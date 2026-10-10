import Foundation

/// Reads fields back out of an already-assembled `Certificate` DER — used by
/// `sign` to pull the issuer commonName off `--root-cert` rather than have
/// the operator retype it. A retyped string that drifted from the root
/// certificate's actual `subject` would build a leaf whose `issuer` field
/// disagrees with it, which chain-building algorithms — Python's `ssl`
/// module's OpenSSL backend included — use to match a leaf to its issuing
/// CA; reading it back guarantees the two are byte-identical, since both
/// pass through the same `X509Name.commonNameOnly` construction.
public enum CertificateReader {
    public static func subjectCommonName(certificateDER: Data) throws -> String? {
        let outer = try DERReader.readTLV(certificateDER, at: 0)
        try DERReader.expect(outer, tag: DER.tagSequence)
        let fields = try DERReader.children(certificateDER, in: outer.content)
        guard let tbs = fields.first else { throw DERError.malformed("empty Certificate") }
        try DERReader.expect(tbs, tag: DER.tagSequence)
        let tbsFields = try DERReader.children(certificateDER, in: tbs.content)
        // `TBSCertificate`'s fields in order: version [0], serialNumber,
        // signature, issuer, validity, subject, subjectPublicKeyInfo,
        // extensions [3]. Every certificate this tool ever reads is its own
        // v3 output, so `version` is always present and `subject` is always
        // index 5.
        guard tbsFields.count > 5 else { throw DERError.malformed("TBSCertificate missing subject") }
        return X509Name.readCommonName(Data(certificateDER[tbsFields[5].whole]))
    }
}
