import Foundation

/// SSH, and the registration step on the machine serving the Library, kept behind this protocol so
/// `Enrollment.enroll`'s orchestration — the fingerprint gate, the signing
/// order, and what happens after a step fails — is exercised with no
/// network connection and no Secure Enclave. The one production conformer
/// lives in the `root-ca` executable target, next to
/// `SecureEnclaveRootSigner`: both are boundaries this package has with
/// something outside the process (hardware there, a network and another
/// machine's filesystem here), and neither is exercised by this package's
/// own test suite for that reason.
public protocol EnrollmentTransport {
    /// Over SSH as `agents@host`, runs `individuator csr --out <path>` on
    /// the new host and returns the CSR PEM pulled back.
    func fetchCertificateRequest(host: String) throws -> String

    /// Over the same SSH connection, writes `certificatePEM` to the host
    /// for the installer's `individuator adopt-cert` to pick up. This call
    /// never runs `adopt-cert` itself — that is the installer's step,
    /// routed through the steward.
    func pushCertificate(host: String, certificatePEM: String) throws

    /// Over SSH to the machine serving the Library, runs `enroll_machine.py issue <host>
    /// <fingerprintSHA256> <not_after> --custody root-file` — the
    /// certificate's own fingerprint (not the public-key fingerprint the
    /// operator compared) and the expiry this call itself computed, so
    /// nothing is re-typed. `machine_registry.issue` revokes whatever
    /// certificate was previously active for `host` in the same write.
    func registerMachine(host: String, fingerprintSHA256: String, notAfter: Date) throws
}

/// Which step of `Enrollment.enroll` an `EnrollmentTransport` failure
/// happened in, so a caller can report "the SSH to the host failed" and
/// "the SSH to the Library's machine failed" as two different messages rather than one
/// generic one.
public enum EnrollmentStep: String, Equatable, Sendable, CustomStringConvertible {
    case pullingTheCertificateRequest = "pulling the certificate request from the host"
    case pushingTheCertificate = "pushing the signed certificate back to the host"
    case registeringTheMachine = "registering the machine with the Library"

    public var description: String { rawValue }
}

public enum EnrollmentError: Error, CustomStringConvertible {
    /// An `EnrollmentTransport` call failed. Nothing past that step ran:
    /// a failure while pulling the request signs nothing; a failure while
    /// pushing the certificate still registers nothing.
    case transportFailed(EnrollmentStep, Error)
    /// The operator declined the fingerprint comparison. No certificate is
    /// signed, pushed, or registered.
    case declined(host: String)
    /// The host name is not a plain DNS label sequence. It is passed to
    /// `ssh` as `agents@<host>` and into the remote registration command,
    /// so a leading `-` would be read as an ssh option and a space or a
    /// shell character would split the remote command.
    case invalidHost(String)

    public var description: String {
        switch self {
        case .transportFailed(let step, let underlying):
            return "enroll: \(step) failed: \(underlying)"
        case .invalidHost(let host):
            return "enroll: \(host.debugDescription) is not a host name; " +
                "use letters, digits, '.' and '-', starting with a letter or digit"
        case .declined(let host):
            return "enroll: the fingerprint for \(host) was not confirmed; " +
                "signed, pushed, and registered nothing"
        }
    }
}

/// The `root-ca enroll <host>` flow (CQ·223): pull the host's certificate
/// signing request over SSH, print its public-key fingerprint for the
/// operator to compare against the host's own screen — the same comparison
/// `issueLeaf`'s docstring in the `root-ca` executable describes, and the
/// actual authorization platform/decisions/0066 requires, not the Touch ID
/// gesture that follows it — sign on confirmation, push the certificate
/// back to the host, and register the machine with the Library. A decline, or an
/// `EnrollmentTransport` failure at any step, leaves every later step
/// un-run: nothing is signed, pushed, or registered past the point of
/// failure.
public enum Enrollment {
    public struct Result {
        public let host: String
        public let certificatePEM: String
        public let publicKeyFingerprint: String
        public let certificateFingerprint: String
        public let notAfter: Date
    }

    /// - Parameters:
    ///   - issuerCommonName: the root certificate's own subject commonName,
    ///     read back by the caller the same way `issueLeaf` does for `sign`
    ///     — this function takes no certificate file path, only the already-
    ///     resolved values, so its orchestration is testable with neither a
    ///     filesystem root certificate nor a Secure Enclave.
    ///   - printFingerprint: called once, with the public-key fingerprint,
    ///     before `confirm` — the ordering `RootSigner.sign`'s docstring
    ///     calls load-bearing: the comparison must happen, and be seen,
    ///     before the signature that Touch ID releases.
    ///   - confirm: the operator's out-of-band comparison against the
    ///     host's own screen. Returning `false` signs, pushes, and
    ///     registers nothing.
    /// Whether `host` is letters, digits, `.` and `-`, starting with a
    /// letter or digit: every name `ssh` and a remote shell read literally.
    public static func isValidHostName(_ host: String) -> Bool {
        guard let first = host.unicodeScalars.first,
              CharacterSet.alphanumerics.contains(first), first.isASCII else { return false }
        return host.unicodeScalars.allSatisfy {
            $0.isASCII && (CharacterSet.alphanumerics.contains($0) || $0 == "." || $0 == "-")
        }
    }

    public static func enroll(
        host: String,
        issuerCommonName: String,
        days: Int,
        now: Date = Date(),
        transport: EnrollmentTransport,
        signer: RootSigner,
        printFingerprint: (String) -> Void,
        confirm: (String) -> Bool
    ) throws -> Result {
        guard isValidHostName(host) else {
            throw EnrollmentError.invalidHost(host)
        }
        let csrPEM: String
        do {
            csrPEM = try transport.fetchCertificateRequest(host: host)
        } catch {
            throw EnrollmentError.transportFailed(.pullingTheCertificateRequest, error)
        }

        let csr = try CertificateRequest.parse(pem: csrPEM)
        let publicKeyFingerprint = Fingerprint.publicKey(spkiDER: csr.spkiDER)
        printFingerprint(publicKeyFingerprint)
        guard confirm(publicKeyFingerprint) else {
            throw EnrollmentError.declined(host: host)
        }

        let notBefore = now
        let notAfter = notBefore.addingTimeInterval(TimeInterval(days) * 86400)
        let issued = try LeafCertificate.issue(
            csr: csr, machine: host, kind: .client, issuerCommonName: issuerCommonName,
            notBefore: notBefore, notAfter: notAfter, signer: signer)

        do {
            try transport.pushCertificate(host: host, certificatePEM: issued.certificatePEM)
        } catch {
            throw EnrollmentError.transportFailed(.pushingTheCertificate, error)
        }

        do {
            try transport.registerMachine(
                host: host, fingerprintSHA256: issued.certificateFingerprint, notAfter: notAfter)
        } catch {
            throw EnrollmentError.transportFailed(.registeringTheMachine, error)
        }

        return Result(
            host: host, certificatePEM: issued.certificatePEM,
            publicKeyFingerprint: publicKeyFingerprint,
            certificateFingerprint: issued.certificateFingerprint, notAfter: notAfter)
    }
}
