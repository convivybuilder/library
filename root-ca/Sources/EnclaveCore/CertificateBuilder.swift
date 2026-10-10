import Foundation

/// Assembles an X.509v3 `Certificate` DER structure from already-encoded
/// pieces. Every field this tool's certificates carry is spelled out
/// explicitly here rather than defaulted by a library, because
/// platform/decisions/0066's Handoff is itself the spec for what belongs in
/// each one — there is no "reasonable default" this module is allowed to
/// pick instead of reading it off that record.
public enum X509 {
    /// `AlgorithmIdentifier ::= SEQUENCE { algorithm OID, parameters ABSENT
    /// }` — ECDSA signature algorithms carry no parameters (unlike RSA's
    /// mandatory NULL), per RFC 5480.
    public static let ecdsaWithSHA256AlgorithmIdentifier = DER.sequence([DER.oid(OIDs.ecdsaWithSHA256)])

    /// `SubjectPublicKeyInfo` for a raw P-256 point (`0x04 || X(32) || Y(32)`,
    /// the uncompressed form `SecKeyCopyExternalRepresentation` returns) —
    /// used only to build the root's own SPKI, since it is the one public
    /// key in this tool's flow that never arrives already wrapped in one
    /// (see `CertificateRequest`, which reuses a CSR's SPKI verbatim instead
    /// of rebuilding it, precisely so the tool's own re-encoding is never a
    /// second thing that could disagree with what the operator's screen
    /// shows).
    public static func spki(rawECPoint: Data) -> Data {
        let algorithm = DER.sequence([DER.oid(OIDs.idEcPublicKey), DER.oid(OIDs.prime256v1)])
        return DER.sequence([algorithm, DER.bitString(rawECPoint)])
    }

    /// A cryptographically random 20-byte serial number, DER-encoded as a
    /// positive INTEGER. 20 bytes (160 bits) matches common CA practice
    /// (CA/Browser Forum baseline requirements ask for at least 64 bits of
    /// output from a CSPRNG); collision odds are not a concern this tool's
    /// issuance volume — "about a dozen times a year across three machines"
    /// (platform/decisions/0066) — ever has to think about.
    public static func randomSerial() -> Data {
        var bytes = [UInt8](repeating: 0, count: 20)
        let result = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        precondition(result == errSecSuccess, "SecRandomCopyBytes failed with status \(result)")
        return DER.integer(unsignedMagnitude: Data(bytes))
    }

    /// `TBSCertificate`, exactly the fields RFC 5280 requires for a v3
    /// certificate carrying extensions: `version [0] EXPLICIT INTEGER { v3
    /// (2) }`, `serialNumber`, `signature` (the algorithm the *outer*
    /// `Certificate.signatureAlgorithm` must repeat verbatim — RFC 5280
    /// section 4.1.1.2), `issuer`, `validity`, `subject`,
    /// `subjectPublicKeyInfo`, `extensions [3] EXPLICIT`.
    public static func buildTBSCertificate(
        serial: Data,
        issuerName: Data,
        subjectName: Data,
        notBefore: Date,
        notAfter: Date,
        spkiDER: Data,
        extensions: [Data]
    ) -> Data {
        let version = DER.tlv(DER.contextExplicit(0), DER.integer(2))
        let validity = DER.sequence([DER.utcTime(notBefore), DER.utcTime(notAfter)])
        let extensionsField = DER.tlv(DER.contextExplicit(3), DER.sequence(extensions))
        return DER.sequence([
            version,
            serial,
            ecdsaWithSHA256AlgorithmIdentifier,
            issuerName,
            validity,
            subjectName,
            spkiDER,
            extensionsField,
        ])
    }

    /// `Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm,
    /// signatureValue BIT STRING }`. `signature` is the raw ECDSA-Sig-Value
    /// DER bytes `SecKeyCreateSignature(..., .ecdsaSignatureMessageX962SHA256,
    /// ...)` returns — that algorithm hashes and signs in one call and
    /// already produces the exact bytes this field needs, so nothing here
    /// re-derives or re-wraps them.
    public static func assembleCertificate(tbsDER: Data, signature: Data) -> Data {
        DER.sequence([tbsDER, ecdsaWithSHA256AlgorithmIdentifier, DER.bitString(signature)])
    }
}
