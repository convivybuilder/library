import EnclaveCore
import Foundation

/// root-ca — the Secure Enclave root-generation and signing tool for the
/// Library's certificate authority (platform/decisions/0066). Runs on
/// `macbook` only: it is the one machine with a Touch ID sensor, and the ADR
/// is explicit that the root belongs there because of that fact, not on the
/// mini that serves the Library.
///
/// Three subcommands:
///
///   root-ca generate-root --out root.crt
///       One-time. Generates the root's P-256 key inside the Secure Enclave,
///       non-exportable, released only by Touch ID on the current enrolled
///       fingerprint set. Writes the self-signed root certificate — public,
///       and the only thing about the root the Library ever holds.
///
///   root-ca sign --machine <name> --kind client|server --csr <path> \
///       --custody hardware|file|root-file --root-cert <path> --out leaf.crt
///       Verifies the CSR's self-signature, prints the SHA-256 of the
///       requesting public key for the operator to compare against the
///       requesting machine's own screen, then asks the Secure Enclave to
///       sign — which is the moment Touch ID is requested. Prints the exact
///       `enroll_machine.py issue` command to run next, on the machine that
///       serves the Library, since this tool never writes to that registry
///       itself (`machine_registry.py` owns that write path).
///
///   root-ca sign-signer --email <person's email> --machine <name> \
///       --csr <path> --root-cert <path> --out signer.crt
///       Issues a person's signing certificate (platform/contracts/
///       library-protocol §2.2) — the same ceremony as `sign`, sharing its
///       flow: self-signature verification, the fingerprint print before
///       Touch ID, and the certificate-fingerprint-and-expiry print after,
///       in the shape a registry enrolment command can take. Registry
///       enrolment itself is a separate tool this command does not call.
///
/// **The fingerprint print and the Touch ID prompt are two different
/// authorizations, not one.** The prompt proves a human touched the sensor;
/// it says nothing about what was signed, because the Secure Enclave never
/// shows the requester the certificate request it is about to authorize.
/// The fingerprint comparison is what binds the signature to *this*
/// request — see `RootSigner.sign`'s docstring in EnclaveCore. Do not
/// collapse the two into a single "press finger to sign" step; the order in
/// `issueLeaf` below (print, then sign) is load-bearing.
let defaultLabel = "Convivy Library Root CA"
let defaultRootCommonName = "Convivy Library Root"

struct CLIError: Error, CustomStringConvertible {
    let message: String
    let exitCode: Int32
    init(_ message: String, exitCode: Int32 = 1) {
        self.message = message
        self.exitCode = exitCode
    }
    var description: String { message }
}

func fail(_ message: String, exitCode: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data("root-ca: \(message)\n".utf8))
    exit(exitCode)
}

func run() {
    var args = Array(CommandLine.arguments.dropFirst())
    guard !args.isEmpty else {
        fail("usage: root-ca <generate-root|sign|sign-signer> [options]", exitCode: 2)
    }
    let command = args.removeFirst()
    do {
        switch command {
        case "generate-root":
            try runGenerateRoot(args)
        case "sign":
            try runSign(args)
        case "sign-signer":
            try runSignSigner(args)
        default:
            fail("unknown command \(command.debugDescription); expected generate-root, sign, or sign-signer", exitCode: 2)
        }
    } catch let e as CLIError {
        fail(e.message, exitCode: e.exitCode)
    } catch {
        fail("\(error)")
    }
}

// MARK: - generate-root

func runGenerateRoot(_ args: [String]) throws {
    var p = ArgParser(args)
    let label = p.value(for: "--label") ?? defaultLabel
    let commonName = p.value(for: "--common-name") ?? defaultRootCommonName
    let days = try parseDays(p.value(for: "--days"), default: 3650) // 10 years — unspecified by ADR-0066, see the PR body
    guard let outPath = p.value(for: "--out") else {
        throw CLIError("generate-root requires --out <path to write the root certificate PEM to>", exitCode: 2)
    }
    try p.requireEmpty()

    print("Generating the root key in the Secure Enclave, biometry-bound to the")
    print("current enrolled fingerprint set. This may prompt for Touch ID.")

    let signer: SecureEnclaveRootSigner
    do {
        signer = try SecureEnclaveRootSigner.generate(label: label)
    } catch let e as SecureEnclaveError {
        throw CLIError(e.description)
    }

    let notBefore = Date()
    let notAfter = notBefore.addingTimeInterval(TimeInterval(days) * 86400)
    let certDER: Data
    do {
        certDER = try RootCertificate.generateSelfSigned(
            signer: signer, commonName: commonName, notBefore: notBefore, notAfter: notAfter)
    } catch let e as SecureEnclaveError {
        throw CLIError(e.description)
    }

    let pem = PEM.encode(certDER, label: "CERTIFICATE")
    try pem.write(toFile: outPath, atomically: true, encoding: .utf8)

    print("")
    print("root certificate written to \(outPath)")
    print("subject: CN=\(commonName)")
    print("valid until: \(isoDate(notAfter))")
    print("")
    print("This file is public. Install it as the Library's mutual-TLS trust")
    print("anchor (ssl_ca_certs). The private key never left the Secure Enclave")
    print("and this tool has no command that exports it.")
}

// MARK: - sign, sign-signer (shared flow)

/// What `issueLeaf` hands back to a subcommand: the issued certificate and
/// the `notAfter` it was computed with, since `LeafCertificate.Result` does
/// not carry the expiry back out.
struct IssuedLeaf {
    let result: LeafCertificate.Result
    let notAfter: Date
}

/// The flow `sign` and `sign-signer` share: read and self-verify the CSR,
/// read the root's own commonName back off `--root-cert`, print the
/// requesting public key's fingerprint for the operator to compare *before*
/// Touch ID releases the signature, then sign. `machine` names the Mac
/// requesting the key in every case; `signingEmail` is required only when
/// `kind` is `.signing` and is `LeafCertificate.issue`'s to validate.
func issueLeaf(
    csrPath: String,
    rootCertPath: String,
    machine: String,
    kind: MachineCertificateKind,
    signingEmail: String?,
    days: Int
) throws -> IssuedLeaf {
    let csrPEM = try readFile(csrPath, what: "CSR")
    let csr: CertificateRequest
    do {
        csr = try CertificateRequest.parse(pem: csrPEM)
    } catch {
        throw CLIError("could not parse or verify the CSR at \(csrPath): \(error)")
    }

    let rootCertPEM = try readFile(rootCertPath, what: "root certificate")
    let rootCertDER: Data
    do {
        rootCertDER = try PEM.decode(rootCertPEM, label: "CERTIFICATE")
    } catch {
        throw CLIError("could not read the root certificate at \(rootCertPath): \(error)")
    }
    guard let issuerCommonName = try? CertificateReader.subjectCommonName(certificateDER: rootCertDER)
    else {
        throw CLIError("the root certificate at \(rootCertPath) carries no readable commonName")
    }

    // **Print the fingerprint before signing. This ordering is the control.**
    // See this file's top-level docstring and RootSigner.sign's docstring in
    // EnclaveCore: the fingerprint comparison is what authorizes WHAT gets
    // signed, and it has to happen, and be seen, before the signature that
    // Touch ID is about to release.
    print("requesting machine: \(machine)")
    // **THE CSR'S OWN commonName IS DELIBERATELY NOT PRINTED HERE**
    // (security-reviewer, convivy/library#92). It is chosen freely by the
    // requesting machine, and printing it put attacker-controlled text on the
    // operator's screen immediately above the fingerprint that ADR-0066 calls
    // "the authorization" for what gets signed. Newlines and carriage returns
    // are ordinary valid UTF-8 and need no exotic escape: a commonName of
    // blank lines followed by a fabricated "compare this" block and a
    // fingerprint from a prior legitimate enrollment pushes the real block out
    // of view, and the operator authorizes a key they never compared.
    //
    // **Escaping control characters would not be enough, which is why this is
    // a deletion rather than a sanitiser.** A commonName of pure printable
    // ASCII reading "verified — matches prior enrollment" is still a sentence
    // the attacker wrote next to the value the whole ceremony exists to
    // protect. Nothing that a requester controls belongs on this screen.
    //
    // Nothing is lost: the issued certificate's subject takes its commonName
    // from `machine` or from `signingEmail` (`LeafCertificate.issue`), never
    // from the CSR's own commonName, so the CSR's commonName has no effect on
    // what is signed and never had.
    //
    // **The attack this closes is cheaper than the residual ADR-0066
    // accepts.** That record accepts a co-resident process on `macbook`
    // fabricating a prompt, which needs code execution on the signing machine.
    // This needed only a CSR file placed in front of the operator — which the
    // record's own issuance step 2 says travels with no confidentiality.
    print("")
    print("public key SHA-256 — compare this against what the requesting machine's")
    print("own screen shows before continuing. This comparison is the")
    print("authorization; Touch ID only confirms a human is present.")
    print("")
    print("  \(Fingerprint.publicKey(spkiDER: csr.spkiDER))")
    print("")

    let signer: SecureEnclaveRootSigner
    do {
        signer = try SecureEnclaveRootSigner.load()
    } catch let e as SecureEnclaveError {
        throw CLIError(e.description)
    }

    let notBefore = Date()
    let notAfter = notBefore.addingTimeInterval(TimeInterval(days) * 86400)
    let result: LeafCertificate.Result
    do {
        result = try LeafCertificate.issue(
            csr: csr, machine: machine, kind: kind, signingEmail: signingEmail,
            issuerCommonName: issuerCommonName,
            notBefore: notBefore, notAfter: notAfter, signer: signer)
    } catch let e as SecureEnclaveError {
        throw CLIError(e.description)
    }
    return IssuedLeaf(result: result, notAfter: notAfter)
}

// MARK: - sign

/// The leaf custodies `sign` accepts, spelled exactly as
/// `machine_registry.CUSTODY_VALUES` spells them, because the value is passed
/// through verbatim to the `enroll_machine.py issue --custody` command printed
/// below. It is not written into the certificate. `root-file` is the
/// Individuator's root-held software key (platform/decisions/0092);
/// library#290 added it to the registry and missed this list.
/// `tests/test_root_ca_custody.py` pins the two sets equal.
let leafCustodyValues = ["hardware", "file", "root-file"]

func runSign(_ args: [String]) throws {
    var p = ArgParser(args)
    let days = try parseDays(p.value(for: "--days"), default: 90) // platform/decisions/0066 Handoff: 90-day machine certificates
    guard let machine = p.value(for: "--machine") else {
        throw CLIError("sign requires --machine <name>", exitCode: 2)
    }
    guard let kindRaw = p.value(for: "--kind") else {
        throw CLIError("sign requires --kind client|server", exitCode: 2)
    }
    let kind: MachineCertificateKind
    switch kindRaw {
    case "client": kind = .client
    case "server": kind = .server
    default: throw CLIError("--kind must be client or server, not \(kindRaw.debugDescription)", exitCode: 2)
    }
    guard let custody = p.value(for: "--custody"), leafCustodyValues.contains(custody) else {
        throw CLIError("sign requires --custody \(leafCustodyValues.joined(separator: "|")) — this is the LEAF key's " +
            "custody, not the root's, and enroll_machine.py takes it as a required " +
            "argument for the same reason: it must be typed by a human on this attended " +
            "path, never inferred or defaulted (platform/decisions/0066)", exitCode: 2)
    }
    guard let csrPath = p.value(for: "--csr") else {
        throw CLIError("sign requires --csr <path to the requester's certificate signing request>", exitCode: 2)
    }
    guard let rootCertPath = p.value(for: "--root-cert") else {
        throw CLIError("sign requires --root-cert <path to the root certificate generate-root wrote>", exitCode: 2)
    }
    guard let outPath = p.value(for: "--out") else {
        throw CLIError("sign requires --out <path to write the leaf certificate PEM to>", exitCode: 2)
    }
    try p.requireEmpty()

    let issued = try issueLeaf(
        csrPath: csrPath, rootCertPath: rootCertPath, machine: machine,
        kind: kind, signingEmail: nil, days: days)
    let result = issued.result

    try result.certificatePEM.write(toFile: outPath, atomically: true, encoding: .utf8)

    print("signed: \(outPath)")
    print("  machine:            \(machine)")
    print("  kind:               \(kind == .client ? "clientAuth" : "serverAuth")")
    print("  not_after:          \(isoDate(issued.notAfter))")
    print("  certificate SHA-256: \(result.certificateFingerprint)")
    print("")
    print("This tool does not write to machine_certificates itself. Run the")
    print("following on the machine that serves the Library, against its own")
    print("database (platform/decisions/0066, issuance step 5):")
    print("")
    print("  enroll_machine.py issue \(machine) \(result.certificateFingerprint) " +
        "\(isoDate(issued.notAfter)) --custody \(custody) --note \"signed by root-ca, \(isoDate(Date()))\"")
}

// MARK: - sign-signer

func runSignSigner(_ args: [String]) throws {
    var p = ArgParser(args)
    let days = try parseDays(p.value(for: "--days"), default: 90)
    guard let email = p.value(for: "--email") else {
        throw CLIError("sign-signer requires --email <the person's email>", exitCode: 2)
    }
    guard let machine = p.value(for: "--machine") else {
        throw CLIError("sign-signer requires --machine <name of the Mac holding the key>", exitCode: 2)
    }
    guard let csrPath = p.value(for: "--csr") else {
        throw CLIError("sign-signer requires --csr <path to the requester's certificate signing request>", exitCode: 2)
    }
    guard let rootCertPath = p.value(for: "--root-cert") else {
        throw CLIError("sign-signer requires --root-cert <path to the root certificate generate-root wrote>", exitCode: 2)
    }
    guard let outPath = p.value(for: "--out") else {
        throw CLIError("sign-signer requires --out <path to write the signing certificate PEM to>", exitCode: 2)
    }
    try p.requireEmpty()

    let issued = try issueLeaf(
        csrPath: csrPath, rootCertPath: rootCertPath, machine: machine,
        kind: .signing, signingEmail: email, days: days)
    let result = issued.result

    try result.certificatePEM.write(toFile: outPath, atomically: true, encoding: .utf8)

    print("signed: \(outPath)")
    print("  email:               \(email.lowercased())")
    print("  machine:             \(machine)")
    print("  not_after:           \(isoDate(issued.notAfter))")
    print("  certificate SHA-256: \(result.certificateFingerprint)")
    print("")
    print("This tool does not write to signing_certificates itself. Run the")
    print("following on the machine that serves the Library, against its own")
    print("database (platform/contracts/library-protocol §2.2):")
    print("")
    print("  enroll_signing_cert.py issue \(email.lowercased()) \(machine) " +
        "\(result.certificateFingerprint) \(isoDate(issued.notAfter)) " +
        "--note \"signed by root-ca, \(isoDate(Date()))\"")
}

// MARK: - helpers

func readFile(_ path: String, what: String) throws -> String {
    do {
        return try String(contentsOfFile: path, encoding: .utf8)
    } catch {
        throw CLIError("could not read the \(what) at \(path): \(error)")
    }
}

func parseDays(_ raw: String?, default def: Int) throws -> Int {
    guard let raw else { return def }
    guard let n = Int(raw), n > 0 else {
        throw CLIError("--days must be a positive integer, not \(raw.debugDescription)", exitCode: 2)
    }
    return n
}

func isoDate(_ date: Date) -> String {
    let formatter = DateFormatter()
    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    formatter.timeZone = TimeZone(identifier: "UTC")
    return formatter.string(from: date)
}

/// A minimal `--flag value` parser — two subcommands and a handful of
/// options each do not need swift-argument-parser, and adding an external
/// SwiftPM dependency for this would be exactly the kind of dependency
/// change this repo's Python side avoids for the same reason
/// (`generate_csr.py`'s and `tests/tls_ca.py`'s own docstrings, on why they
/// shell out to `openssl` instead of adding `cryptography`).
struct ArgParser {
    private var remaining: [String]
    init(_ args: [String]) { remaining = args }

    mutating func value(for flag: String) -> String? {
        guard let idx = remaining.firstIndex(of: flag), idx + 1 < remaining.count else { return nil }
        let v = remaining[idx + 1]
        remaining.removeSubrange(idx...(idx + 1))
        return v
    }

    func requireEmpty() throws {
        guard remaining.isEmpty else {
            throw CLIError("unrecognized argument(s): \(remaining.joined(separator: " "))", exitCode: 2)
        }
    }
}

run()
