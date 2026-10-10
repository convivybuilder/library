import Foundation
import Security

enum SecTrustError: Error, CustomStringConvertible {
    case couldNotParseCertificate
    case couldNotCreateTrust(OSStatus)

    var description: String {
        switch self {
        case .couldNotParseCertificate: return "Security.framework could not parse a certificate this tool produced"
        case .couldNotCreateTrust(let status): return "SecTrustCreateWithCertificates failed: \(status)"
        }
    }
}

/// Chain validation via `Security.framework`'s own X.509 trust engine —
/// entirely in-process, and a genuinely independent implementation from
/// everything in `EnclaveCore`: Apple's, not this tool's. This is the real
/// cross-check `IssuanceIntegrationTests` relies on in place of shelling out
/// to `openssl verify` (see `TestCSRBuilder`'s docstring for why that path
/// is not available in this environment).
enum SecTrustHelper {
    /// `true` if `leafDER` validates against `rootDER` treated as the sole
    /// trusted anchor — signature, chain-building by issuer/subject name,
    /// and the validity window all checked by Security.framework itself.
    static func chainValidates(leafDER: Data, rootDER: Data) throws -> Bool {
        guard let leafCert = SecCertificateCreateWithData(nil, leafDER as CFData),
              let rootCert = SecCertificateCreateWithData(nil, rootDER as CFData)
        else {
            throw SecTrustError.couldNotParseCertificate
        }
        let policy = SecPolicyCreateBasicX509()
        var trust: SecTrust?
        let status = SecTrustCreateWithCertificates(leafCert, policy, &trust)
        guard status == errSecSuccess, let trust else {
            throw SecTrustError.couldNotCreateTrust(status)
        }
        SecTrustSetAnchorCertificates(trust, [rootCert] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        var error: CFError?
        return SecTrustEvaluateWithError(trust, &error)
    }
}
