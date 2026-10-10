import EnclaveCore
import Foundation
import Security

/// Builds a self-signed PKCS#10 `CertificationRequest` from a throwaway
/// software P-256 key, entirely in-process.
///
/// **Why not shell out to `openssl` the way `tests/tls_ca.py` does on the
/// Python side.** It was tried first, matching that convention exactly, and
/// chasing an intermittent-looking crash under it is what surfaced a real
/// bug in `DERReader` (now fixed — see that file's docstring): the crash
/// turned out to have nothing to do with subprocesses, and reproduced from
/// pure in-process key generation too, once the CSR's SPKI was read back as
/// a `Data` slice. This builder stayed in-process anyway, since it needs no
/// subprocess at all — `CertificateRequest.parse` still needs a real CSR to
/// prove it verifies a real one, and building one from a software `SecKey`
/// exercises exactly the same DER this tool signs in production.
///
/// This is not a second implementation to trust instead of a first: the
/// certificate-verification side of these tests (`IssuanceIntegrationTests`)
/// cross-checks the *output* against Security.framework's own `SecTrust`
/// and `SecCertificateCopyValues` — a parser Apple ships, independent of
/// everything in `EnclaveCore` — which is the real independent check this
/// suite relies on.
enum TestCSRBuilder {
    struct Generated {
        let pem: String
        let der: Data
        let privateKey: SecKey
        let rawECPoint: Data
    }

    static func makeP256CSR(commonName: String) throws -> Generated {
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

        let subject = X509Name.commonNameOnly(commonName)
        let spki = X509.spki(rawECPoint: rawPoint)
        // `attributes [0] IMPLICIT SET OF Attribute`, empty: this tool never
        // reads a CSR's requested extensions (see `CertificateRequest`'s
        // docstring for why — the machine name and purpose come from the
        // operator's `--machine`/`--kind` flags, never from the CSR's own
        // self-asserted content), so nothing here ever needs to populate it.
        let emptyAttributes = DER.tlv(DER.contextPrimitive(0), Data())
        let certificationRequestInfo = DER.sequence([DER.integer(0), subject, spki, emptyAttributes])

        var sigError: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(
            privateKey, .ecdsaSignatureMessageX962SHA256, certificationRequestInfo as CFData, &sigError
        ) as Data? else {
            throw sigError!.takeRetainedValue()
        }

        let der = DER.sequence([
            certificationRequestInfo,
            X509.ecdsaWithSHA256AlgorithmIdentifier,
            DER.bitString(signature),
        ])
        return Generated(pem: PEM.encode(der, label: "CERTIFICATE REQUEST"), der: der,
                          privateKey: privateKey, rawECPoint: rawPoint)
    }

    /// A CSR whose SPKI claims an algorithm OID that is not `id-ecPublicKey`
    /// — structurally well-formed, but naming a key type the Secure Enclave
    /// root can never sign for. Stands in for "someone submitted a non-EC
    /// (e.g. RSA) CSR" without needing an RSA key generator: what
    /// `CertificateRequest.extractRawECPoint` actually checks is the
    /// algorithm OID, not the key material, so this exercises the same
    /// refusal path.
    static func makeCSRWithWrongKeyAlgorithm(commonName: String) throws -> Data {
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

        let subject = X509Name.commonNameOnly(commonName)
        // A SubjectPublicKeyInfo whose algorithm OID is deliberately wrong
        // (ecdsa-with-SHA256's OID, which is a signature algorithm, not a
        // key algorithm — never a value `extractRawECPoint` accepts).
        let wrongAlgorithm = DER.sequence([DER.oid(OIDs.ecdsaWithSHA256)])
        let bogusSPKI = DER.sequence([wrongAlgorithm, DER.bitString(rawPoint)])
        let emptyAttributes = DER.tlv(DER.contextPrimitive(0), Data())
        let certificationRequestInfo = DER.sequence([DER.integer(0), subject, bogusSPKI, emptyAttributes])

        var sigError: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(
            privateKey, .ecdsaSignatureMessageX962SHA256, certificationRequestInfo as CFData, &sigError
        ) as Data? else {
            throw sigError!.takeRetainedValue()
        }
        return DER.sequence([
            certificationRequestInfo,
            X509.ecdsaWithSHA256AlgorithmIdentifier,
            DER.bitString(signature),
        ])
    }
}
