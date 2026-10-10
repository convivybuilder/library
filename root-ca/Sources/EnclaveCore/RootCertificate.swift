import Foundation

/// Builds the root's own self-signed certificate — the one document this
/// tool's `generate-root` command hands to the Library, and the only thing
/// about the root that the Library ever holds: "The Library holds only the
/// root certificate, which is public. A process that reads it gains
/// nothing" (platform/decisions/0066).
public enum RootCertificate {
    public static func generateSelfSigned(
        signer: RootSigner,
        commonName: String,
        notBefore: Date,
        notAfter: Date
    ) throws -> Data {
        let name = X509Name.commonNameOnly(commonName)
        let spki = X509.spki(rawECPoint: signer.publicKeyRawPoint)
        let extensions = [
            X509Extension.basicConstraints(isCA: true),
            X509Extension.keyUsage(isCA: true),
        ]
        let tbs = X509.buildTBSCertificate(
            serial: X509.randomSerial(),
            issuerName: name, subjectName: name,
            notBefore: notBefore, notAfter: notAfter,
            spkiDER: spki, extensions: extensions)
        let signature = try signer.sign(tbs)
        return X509.assembleCertificate(tbsDER: tbs, signature: signature)
    }
}
