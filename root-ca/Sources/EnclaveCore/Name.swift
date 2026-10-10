import Foundation

/// `Name` construction and reading, scoped to exactly what this tool's
/// certificates ever carry: one RDN, one AttributeTypeAndValue, commonName.
/// platform/decisions/0066 is explicit that the common name is never read as
/// identity by the Library — `machine_identity.uri_names` never touches
/// it — so `read(cn:)` below exists only for the tool's own informational
/// printouts, never for a trust decision.
public enum X509Name {
    /// `Name ::= RDNSequence ::= SEQUENCE OF RelativeDistinguishedName`,
    /// here always exactly one RDN holding exactly one
    /// `AttributeTypeAndValue { commonName, UTF8String(cn) }`.
    public static func commonNameOnly(_ cn: String) -> Data {
        let atv = DER.sequence([DER.oid(OIDs.commonName), DER.utf8String(cn)])
        return DER.sequence([DER.set([atv])])
    }

    /// The first commonName found while walking a `Name`'s RDNs, or `nil` if
    /// none is present. Read for display only — see the type's docstring.
    public static func readCommonName(_ nameDER: Data) -> String? {
        guard let outer = try? DERReader.readTLV(nameDER, at: 0),
              outer.tag == DER.tagSequence,
              let rdns = try? DERReader.children(nameDER, in: outer.content)
        else { return nil }
        for rdn in rdns {
            guard rdn.tag == DER.tagSet,
                  let atvs = try? DERReader.children(nameDER, in: rdn.content)
            else { continue }
            for atv in atvs {
                guard atv.tag == DER.tagSequence,
                      let fields = try? DERReader.children(nameDER, in: atv.content),
                      fields.count == 2,
                      fields[0].tag == DER.tagOID,
                      Array(nameDER[fields[0].content]) == OIDs.commonName
                else { continue }
                let valueNode = fields[1]
                return String(data: nameDER[valueNode.content], encoding: .utf8)
            }
        }
        return nil
    }
}
