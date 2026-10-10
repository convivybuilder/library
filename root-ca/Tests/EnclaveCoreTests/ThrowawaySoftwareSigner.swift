import EnclaveCore
import Foundation
import Security

/// **A throwaway, software-only stand-in for the Secure Enclave root,
/// exactly the role `tests/tls_ca.py`'s self-signed CA plays on the Python
/// side of this design** — never the real root, never exportable to
/// production code, and never given a way to become one: this type lives in
/// the test target only, `SecureEnclaveRootSigner` is a different type in a
/// different target, and nothing in `EnclaveCore` or the `root-ca`
/// executable can construct or accept a `ThrowawaySoftwareSigner`. It exists
/// so `CertificateBuilder`, `LeafCertificate`, and `RootCertificate` — the
/// DER-assembly logic this PR can actually verify — are exercised against a
/// real ECDSA-P256-SHA256 signer without a Secure Enclave or Touch ID, which
/// this machine does not have (platform/decisions/0066; `ioreg -c
/// AppleBiometricSensor` returns zero nodes here too).
struct ThrowawaySoftwareSigner: RootSigner {
    private let privateKey: SecKey
    let publicKeyRawPoint: Data

    static func generate() throws -> ThrowawaySoftwareSigner {
        let attributes: [String: Any] = [
            kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeySizeInBits as String: 256,
        ]
        var genError: Unmanaged<CFError>?
        guard let privateKey = SecKeyCreateRandomKey(attributes as CFDictionary, &genError) else {
            throw genError!.takeRetainedValue()
        }
        guard let publicKey = SecKeyCopyPublicKey(privateKey) else {
            fatalError("SecKeyCopyPublicKey returned nil for a key this test just created")
        }
        var expError: Unmanaged<CFError>?
        guard let rawPoint = SecKeyCopyExternalRepresentation(publicKey, &expError) as Data? else {
            throw expError!.takeRetainedValue()
        }
        return ThrowawaySoftwareSigner(privateKey: privateKey, publicKeyRawPoint: rawPoint)
    }

    func sign(_ message: Data) throws -> Data {
        var error: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(
            privateKey, .ecdsaSignatureMessageX962SHA256, message as CFData, &error
        ) as Data? else {
            throw error!.takeRetainedValue()
        }
        return signature
    }
}
