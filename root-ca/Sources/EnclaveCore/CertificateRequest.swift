import Foundation
import Security

public enum CSRError: Error, CustomStringConvertible {
    case unsupportedKeyAlgorithm
    case unsupportedSignatureAlgorithm
    case malformedPublicKeyPoint
    case invalidSelfSignature
    case der(DERError)

    public var description: String {
        switch self {
        case .unsupportedKeyAlgorithm:
            return "CSR: subject public key is not EC P-256 — the Secure Enclave root " +
                "only ever signs P-256 keys (platform/decisions/0066)"
        case .unsupportedSignatureAlgorithm:
            return "CSR: signed with something other than ecdsa-with-SHA256"
        case .malformedPublicKeyPoint:
            return "CSR: subjectPublicKey is not a 65-byte uncompressed P-256 point"
        case .invalidSelfSignature:
            return "CSR: signature does not verify against its own claimed public key " +
                "— this is not proof of possession of the private key, refusing to sign it"
        case .der(let e): return e.description
        }
    }
}

/// A parsed, **self-signature-verified** PKCS#10 `CertificationRequest`
/// (RFC 2986). Verifying the self-signature is what turns "a public key and
/// a name someone typed" into "a public key someone has demonstrated holding
/// the matching private key for" — a CSR that fails this check proves
/// nothing about possession, and `CertificateRequest.parse` refuses to
/// return one.
///
/// **This is not the authorization platform/decisions/0066 requires.**
/// Self-signature verification is a structural sanity check on the request;
/// it says nothing about whether the machine *should* get a certificate.
/// That authorization is the fingerprint comparison the operator performs by eye
/// against `Fingerprint.publicKey(spkiDER:)`'s output, plus the Touch ID
/// gesture that releases the root key to sign — both external to this type,
/// and both required (see this tool's `sign` command).
public struct CertificateRequest {
    /// The exact `SubjectPublicKeyInfo` DER bytes as they appeared in the
    /// CSR — reused verbatim in the issued leaf certificate and hashed
    /// verbatim for `Fingerprint.publicKey`, so the fingerprint this tool
    /// prints is byte-for-byte what `generate_csr.py`'s
    /// `openssl pkey -pubout -outform DER` would print on the requesting
    /// machine for the same key.
    public let spkiDER: Data

    /// The raw 65-byte uncompressed EC point (`0x04 || X || Y`) extracted
    /// from `spkiDER`, in the form `SecKeyCreateWithData` accepts.
    public let rawECPoint: Data

    /// The CSR's own `subject` commonName, for display only — see
    /// `X509Name`'s docstring for why this is never trusted for identity.
    public let commonName: String?

    public static func parse(pem: String) throws -> CertificateRequest {
        let der = try PEM.decode(pem, label: "CERTIFICATE REQUEST")
        return try parse(der: der)
    }

    public static func parse(der: Data) throws -> CertificateRequest {
        do {
            let outer = try DERReader.readTLV(der, at: 0)
            try DERReader.expect(outer, tag: DER.tagSequence)
            let outerFields = try DERReader.children(der, in: outer.content)
            guard outerFields.count == 3 else {
                throw DERError.malformed("CertificationRequest expects 3 fields, found \(outerFields.count)")
            }
            let certificationRequestInfo = outerFields[0]
            let signatureAlgorithm = outerFields[1]
            let signatureBitString = outerFields[2]

            try DERReader.expect(certificationRequestInfo, tag: DER.tagSequence)
            let infoFields = try DERReader.children(der, in: certificationRequestInfo.content)
            guard infoFields.count >= 3 else {
                throw DERError.malformed("CertificationRequestInfo expects at least 3 fields, found \(infoFields.count)")
            }
            let subject = infoFields[1]
            let spkiNode = infoFields[2]
            let spkiDER = der[spkiNode.whole]

            try DERReader.expect(signatureAlgorithm, tag: DER.tagSequence)
            let algFields = try DERReader.children(der, in: signatureAlgorithm.content)
            guard let algOIDField = algFields.first, algOIDField.tag == DER.tagOID,
                  Array(der[algOIDField.content]) == OIDs.ecdsaWithSHA256
            else {
                throw CSRError.unsupportedSignatureAlgorithm
            }

            try DERReader.expect(signatureBitString, tag: DER.tagBitString)
            // BIT STRING content: one leading "unused bits" octet (always
            // 0x00 for the byte-aligned DER signatures this tool ever reads
            // or writes), then the ECDSA-Sig-Value DER bytes.
            guard let unusedBitsOctet = der[signatureBitString.content].first, unusedBitsOctet == 0 else {
                throw DERError.malformed("signature BIT STRING has non-zero unused-bit count")
            }
            let signatureDER = der[der.index(after: signatureBitString.content.lowerBound)..<signatureBitString.content.upperBound]

            let rawECPoint = try Self.extractRawECPoint(spkiDER: spkiDER)

            let attrs: [String: Any] = [
                kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
                kSecAttrKeyClass as String: kSecAttrKeyClassPublic,
            ]
            var createError: Unmanaged<CFError>?
            guard let publicKey = SecKeyCreateWithData(rawECPoint as CFData, attrs as CFDictionary, &createError) else {
                throw CSRError.malformedPublicKeyPoint
            }

            var verifyError: Unmanaged<CFError>?
            let verified = SecKeyVerifySignature(
                publicKey, .ecdsaSignatureMessageX962SHA256,
                der[certificationRequestInfo.whole] as CFData,
                Data(signatureDER) as CFData, &verifyError)
            guard verified else { throw CSRError.invalidSelfSignature }

            return CertificateRequest(
                spkiDER: Data(spkiDER),
                rawECPoint: rawECPoint,
                commonName: X509Name.readCommonName(Data(der[subject.whole])))
        } catch let e as DERError {
            throw CSRError.der(e)
        }
    }

    /// `SubjectPublicKeyInfo ::= SEQUENCE { algorithm AlgorithmIdentifier,
    /// subjectPublicKey BIT STRING }`. Checks the algorithm is
    /// `id-ecPublicKey` over `prime256v1` before returning the point, so a
    /// non-EC or non-P-256 key is refused here rather than surfacing as an
    /// opaque `SecKeyCreateWithData` failure.
    static func extractRawECPoint(spkiDER: Data) throws -> Data {
        let outer = try DERReader.readTLV(spkiDER, at: 0)
        try DERReader.expect(outer, tag: DER.tagSequence)
        let fields = try DERReader.children(spkiDER, in: outer.content)
        guard fields.count == 2 else { throw DERError.malformed("SubjectPublicKeyInfo expects 2 fields") }
        let algorithm = fields[0]
        let publicKeyBitString = fields[1]

        try DERReader.expect(algorithm, tag: DER.tagSequence)
        let algFields = try DERReader.children(spkiDER, in: algorithm.content)
        guard algFields.count == 2,
              algFields[0].tag == DER.tagOID, Array(spkiDER[algFields[0].content]) == OIDs.idEcPublicKey,
              algFields[1].tag == DER.tagOID, Array(spkiDER[algFields[1].content]) == OIDs.prime256v1
        else {
            throw CSRError.unsupportedKeyAlgorithm
        }

        try DERReader.expect(publicKeyBitString, tag: DER.tagBitString)
        guard let unusedBitsOctet = spkiDER[publicKeyBitString.content].first, unusedBitsOctet == 0 else {
            throw DERError.malformed("subjectPublicKey BIT STRING has non-zero unused-bit count")
        }
        let point = spkiDER[spkiDER.index(after: publicKeyBitString.content.lowerBound)..<publicKeyBitString.content.upperBound]
        guard point.count == 65, point.first == 0x04 else {
            throw CSRError.malformedPublicKeyPoint
        }
        return Data(point)
    }
}
