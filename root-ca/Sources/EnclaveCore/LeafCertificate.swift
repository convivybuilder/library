import Foundation

public enum MachineCertificateKind {
    /// `clientAuth` — every machine calling the Library over mutual TLS.
    case client
    /// `serverAuth` — the Library's own listener certificate, which also
    /// carries `DNS:localhost` and `IP:127.0.0.1` (platform/decisions/0066:
    /// "a tunnelled client dials its own loopback").
    case server
    /// The signing purpose (`OIDs.idKpSigning`,
    /// platform/contracts/library-protocol §2.2) — a person's Secure Enclave
    /// key, used to sign a knowledge save's envelope. Carries no
    /// `clientAuth`/`serverAuth` and no `convivy://machine/` name, so it
    /// authenticates no TLS connection and a machine certificate can never
    /// stand in for it or vice versa.
    case signing
}

public enum MachineNameError: Error, CustomStringConvertible {
    case invalid(String)
    public var description: String {
        switch self {
        case .invalid(let name):
            return "machine name \(name.debugDescription) must be 1-64 lowercase " +
                "letters, digits, or hyphens, matching machine_certificates.machine's " +
                "column width"
        }
    }
}

public enum SigningEmailError: Error, CustomStringConvertible {
    case invalid(String)
    public var description: String {
        switch self {
        case .invalid(let email):
            return "signing certificate email \(email.debugDescription) must be " +
                "non-empty, contain exactly one '@', and contain only printable " +
                "ASCII (0x21-0x7E)"
        }
    }
}

/// Signs a machine (or the Library's own) leaf certificate from an
/// already-verified `CertificateRequest` — platform/decisions/0066's
/// issuance steps 3-4, minus the human parts (the fingerprint comparison and
/// the Touch ID gesture), which live in the `sign` reads the caller before
/// this ever runs.
public enum LeafCertificate {
    public struct Result {
        public let certificateDER: Data
        public let certificatePEM: String
        /// SHA-256 of the requesting public key — print this and let the
        /// operator compare it against the requesting machine's own screen
        /// *before* the signature that produces `certificateDER` happens.
        /// This tool cannot enforce the ordering; see `RootSigner.sign`'s
        /// docstring for why that comparison is the actual authorization and
        /// this call is not.
        public let publicKeyFingerprint: String
        /// SHA-256 of the issued certificate's full DER — what
        /// `enroll_machine.py issue` needs as `fingerprint_sha256`.
        public let certificateFingerprint: String
    }

    private static let machineNamePattern = try! NSRegularExpression(pattern: "^[a-z0-9][a-z0-9-]{0,63}$")

    public static func validateMachineName(_ machine: String) throws {
        let range = NSRange(machine.startIndex..<machine.endIndex, in: machine)
        guard machineNamePattern.firstMatch(in: machine, range: range) != nil else {
            throw MachineNameError.invalid(machine)
        }
    }

    /// Non-empty, exactly one `@`, and every scalar printable ASCII (0x21
    /// through 0x7E: no space, nothing above 0x7E) — the `rfc822Name` SAN
    /// encodes as an `IA5String` (RFC 5280 §4.2.1.6), which is ASCII only, so
    /// a C1 control (e.g. U+009B) or a bidi override (e.g. U+202E) that a
    /// whitespace-and-C0-only filter would pass must be rejected here before
    /// it reaches that encoding. Returns the email lower-cased, which is what
    /// both the subject commonName and the `rfc822Name` SAN store.
    public static func validateSigningEmail(_ email: String) throws -> String {
        guard !email.isEmpty,
              email.filter({ $0 == "@" }).count == 1,
              email.unicodeScalars.allSatisfy({ $0.value >= 0x21 && $0.value <= 0x7E })
        else {
            throw SigningEmailError.invalid(email)
        }
        return email.lowercased()
    }

    /// - Parameter signingEmail: the person's email — required when `kind`
    ///   is `.signing`, and ignored otherwise. `machine` still names the Mac
    ///   holding the key in both cases: as the subject and sole SAN URI for
    ///   `.client`/`.server`, and as the `<machine>` in `convivy://signer/`
    ///   for `.signing`, whose subject and `rfc822Name` SAN instead carry
    ///   `signingEmail`.
    public static func issue(
        csr: CertificateRequest,
        machine: String,
        kind: MachineCertificateKind,
        signingEmail: String? = nil,
        issuerCommonName: String,
        notBefore: Date,
        notAfter: Date,
        signer: RootSigner
    ) throws -> Result {
        try validateMachineName(machine)

        let subjectName: Data
        let extensions: [Data]
        switch kind {
        case .client, .server:
            let uri = "\(MachineURI.prefix)\(machine)"
            subjectName = X509Name.commonNameOnly(machine)
            extensions = [
                X509Extension.subjectAltName(machineURI: uri, includeLoopback: kind == .server),
                X509Extension.extendedKeyUsage(clientAuth: kind == .client),
                X509Extension.basicConstraints(isCA: false),
                X509Extension.keyUsage(isCA: false),
            ]
        case .signing:
            guard let signingEmail else {
                throw SigningEmailError.invalid("")
            }
            let email = try validateSigningEmail(signingEmail)
            let uri = "\(SignerURI.prefix)\(machine)"
            subjectName = X509Name.commonNameOnly(email)
            extensions = [
                X509Extension.signerSubjectAltName(email: email, signerURI: uri),
                X509Extension.extendedKeyUsageSigning(),
                X509Extension.basicConstraints(isCA: false),
                X509Extension.keyUsage(isCA: false),
            ]
        }
        let issuerName = X509Name.commonNameOnly(issuerCommonName)
        let tbs = X509.buildTBSCertificate(
            serial: X509.randomSerial(),
            issuerName: issuerName, subjectName: subjectName,
            notBefore: notBefore, notAfter: notAfter,
            spkiDER: csr.spkiDER, extensions: extensions)

        let publicKeyFingerprint = Fingerprint.publicKey(spkiDER: csr.spkiDER)
        let signature = try signer.sign(tbs)
        let der = X509.assembleCertificate(tbsDER: tbs, signature: signature)

        return Result(
            certificateDER: der,
            certificatePEM: PEM.encode(der, label: "CERTIFICATE"),
            publicKeyFingerprint: publicKeyFingerprint,
            certificateFingerprint: Fingerprint.certificate(der: der))
    }
}

/// Kept as one named constant rather than a literal repeated in three files
/// (`Extensions.swift`'s SAN builder never needs it directly — it takes the
/// already-assembled URI string), matching `MACHINE_URI_PREFIX` on both
/// Python sides of this design (`machine_identity.py`, `generate_csr.py`) —
/// see `generate_csr.py`'s own docstring for why each side keeps its own
/// literal copy instead of a shared import.
public enum MachineURI {
    public static let prefix = "convivy://machine/"
}

/// A signing certificate's SAN URI names the Mac holding the key, under a
/// scheme disjoint from `MachineURI` on purpose
/// (platform/contracts/library-protocol §2.2: "no reader that looks for one
/// can find the other").
public enum SignerURI {
    public static let prefix = "convivy://signer/"
}
