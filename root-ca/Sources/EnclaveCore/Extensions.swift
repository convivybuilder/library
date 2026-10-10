import Foundation

/// The four X.509v3 extensions this tool ever writes. Every certificate
/// shape platform/decisions/0066's Handoff specifies is assembled from these:
/// exactly one URI name (plus loopback names on the Library's own
/// certificate), the EKU split between machine and server certificates, and
/// CA-vs-leaf marked structurally rather than left to a reader's assumption.
public enum X509Extension {
    /// `Extension ::= SEQUENCE { extnID OID, critical BOOLEAN DEFAULT FALSE
    /// (omitted here since every extension this tool writes is non-critical
    /// except the two named below), extnValue OCTET STRING }`.
    static func build(oid: [UInt8], critical: Bool = false, value: Data) -> Data {
        var fields: [Data] = [DER.oid(oid)]
        if critical { fields.append(DER.boolean(true)) }
        fields.append(DER.octetString(value))
        return DER.sequence(fields)
    }

    /// `subjectAltName`: exactly one `uniformResourceIdentifier` GeneralName
    /// naming the machine, plus (for the Library's own listener certificate)
    /// `dNSName:localhost` and `iPAddress:127.0.0.1` — "a tunnelled client
    /// dials its own loopback" (platform/decisions/0066). Never more than one
    /// URI: `machine_identity.uri_names`'s caller refuses a certificate
    /// carrying two, so this tool must never produce one.
    public static func subjectAltName(machineURI: String, includeLoopback: Bool) -> Data {
        var names: [Data] = [
            DER.tlv(DER.contextPrimitive(6), Data(machineURI.utf8)), // uniformResourceIdentifier [6]
        ]
        if includeLoopback {
            names.append(DER.tlv(DER.contextPrimitive(2), Data("localhost".utf8))) // dNSName [2]
            names.append(DER.tlv(DER.contextPrimitive(7), Data([127, 0, 0, 1])))    // iPAddress [7]
        }
        let generalNames = DER.sequence(names)
        return build(oid: OIDs.idCeSubjectAltName, value: generalNames)
    }

    /// `subjectAltName` for a signing certificate: exactly one `rfc822Name`
    /// (the person's email, already lower-cased by the caller) and one
    /// `uniformResourceIdentifier`, `convivy://signer/<machine>`, naming the
    /// Mac that holds the key (platform/contracts/library-protocol §2.2).
    /// Never a `convivy://machine/` name and never loopback names — a
    /// signing certificate authenticates no TLS connection.
    public static func signerSubjectAltName(email: String, signerURI: String) -> Data {
        let names: [Data] = [
            DER.tlv(DER.contextPrimitive(1), Data(email.utf8)), // rfc822Name [1]
            DER.tlv(DER.contextPrimitive(6), Data(signerURI.utf8)), // uniformResourceIdentifier [6]
        ]
        return build(oid: OIDs.idCeSubjectAltName, value: DER.sequence(names))
    }

    /// `extendedKeyUsage`, one purpose only: `clientAuth` for a machine
    /// certificate, `serverAuth` for the Library's own — "clientAuth only on
    /// machine certificates and serverAuth only on the Library's own"
    /// (platform/decisions/0066), enforced here by the caller never being
    /// offered a way to request both.
    public static func extendedKeyUsage(clientAuth: Bool) -> Data {
        let oid = clientAuth ? OIDs.idKpClientAuth : OIDs.idKpServerAuth
        let purposes = DER.sequence([DER.oid(oid)])
        return build(oid: OIDs.idCeExtKeyUsage, value: purposes)
    }

    /// `extendedKeyUsage` for a signing certificate: CRITICAL, listing
    /// exactly `OIDs.idKpSigning` (platform/contracts/library-protocol §2.2).
    /// Critical, unlike the machine/server EKU above, so a reader that does
    /// not understand this purpose refuses the certificate rather than
    /// accepting it for something it never authorized.
    public static func extendedKeyUsageSigning() -> Data {
        let purposes = DER.sequence([DER.oid(OIDs.idKpSigning)])
        return build(oid: OIDs.idCeExtKeyUsage, critical: true, value: purposes)
    }

    /// `basicConstraints`. Critical on the root (`cA: TRUE`, RFC 5280's
    /// recommendation for a CA certificate) and non-critical on a leaf, where
    /// `cA` is omitted rather than written as `FALSE`: X.690 (11.5) requires a
    /// DER encoder to omit a field carrying its `DEFAULT` value, and `cA`
    /// defaults to `FALSE`. A leaf's `BasicConstraints` is therefore the empty
    /// `SEQUENCE` (`30 00`). OpenSSL and `SecTrust` tolerate the explicit
    /// `FALSE` this used to write, but Python's `cryptography` (Rust `asn1`
    /// backend) refuses to parse it (`ParseError { kind: EncodedDefault,
    /// location: ["BasicConstraints::ca"] }`), and platform/contracts/library-
    /// protocol §2.2 has the Bosun's `kb apply` read a signing certificate
    /// through exactly that library.
    public static func basicConstraints(isCA: Bool) -> Data {
        let content = DER.sequence(isCA ? [DER.boolean(true)] : [])
        return build(oid: OIDs.idCeBasicConstraints, critical: isCA, value: content)
    }

    /// `keyUsage`, critical (RFC 5280 section 4.2.1.3 "SHOULD... be marked as
    /// critical"). The root gets `keyCertSign` (bit 5) and `cRLSign` (bit 6)
    /// — signing certificates is the entire reason this key exists, and
    /// nothing else. A leaf gets `digitalSignature` (bit 0) only: this
    /// tool's leaves are ECDSA, so TLS 1.3 uses the key to sign the
    /// handshake transcript, never for key agreement (that's the ephemeral
    /// ECDHE key, a different key entirely).
    public static func keyUsage(isCA: Bool) -> Data {
        let bits: Set<Int> = isCA ? [5, 6] : [0]
        return build(oid: OIDs.idCeKeyUsage, critical: true, value: DER.namedBitString(bits))
    }
}
