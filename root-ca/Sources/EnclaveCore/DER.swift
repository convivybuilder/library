import Foundation

/// Minimal DER (Distinguished Encoding Rules) building blocks — just the tags
/// this tool's certificates and CSRs ever use. Not a general ASN.1 library:
/// every function here exists because `CertificateBuilder` or
/// `CertificateRequest` calls it, the same scoping `machine_identity.py`'s
/// hand-rolled DER walk uses on the Python side of this same design.
public enum DER {
    // MARK: - Tags

    public static let tagBoolean: UInt8 = 0x01
    public static let tagInteger: UInt8 = 0x02
    public static let tagBitString: UInt8 = 0x03
    public static let tagOctetString: UInt8 = 0x04
    public static let tagNull: UInt8 = 0x05
    public static let tagOID: UInt8 = 0x06
    public static let tagUTF8String: UInt8 = 0x0C
    public static let tagPrintableString: UInt8 = 0x13
    public static let tagUTCTime: UInt8 = 0x17
    public static let tagGeneralizedTime: UInt8 = 0x18
    public static let tagSequence: UInt8 = 0x30
    public static let tagSet: UInt8 = 0x31

    /// `[n]` IMPLICIT, primitive, context-specific — the form every
    /// GeneralName alternative in a subjectAltName uses (dNSName,
    /// uniformResourceIdentifier, iPAddress, ...).
    public static func contextPrimitive(_ n: UInt8) -> UInt8 { 0x80 | n }

    /// `[n]` EXPLICIT, constructed, context-specific — TBSCertificate's
    /// `version` and `extensions` fields, both of which wrap a value that is
    /// itself a distinct ASN.1 type and so cannot be tagged IMPLICIT.
    public static func contextExplicit(_ n: UInt8) -> UInt8 { 0xA0 | n }

    // MARK: - Length and TLV

    /// DER length octets for a value of `count` bytes: short form under 128,
    /// long form (0x80 | number-of-length-bytes, then the length itself
    /// big-endian) otherwise. DER requires the minimal-length encoding of
    /// either form; this always produces it because it derives the byte count
    /// from `count` directly rather than accepting a caller-supplied width.
    public static func length(_ count: Int) -> Data {
        if count < 0x80 {
            return Data([UInt8(count)])
        }
        var bytes: [UInt8] = []
        var n = count
        while n > 0 {
            bytes.insert(UInt8(n & 0xFF), at: 0)
            n >>= 8
        }
        return Data([0x80 | UInt8(bytes.count)] + bytes)
    }

    /// One tag-length-value: `tag`, `length(value.count)`, `value`.
    public static func tlv(_ tag: UInt8, _ value: Data) -> Data {
        var out = Data([tag])
        out.append(length(value.count))
        out.append(value)
        return out
    }

    public static func sequence(_ children: [Data]) -> Data {
        tlv(tagSequence, children.reduce(into: Data()) { $0.append($1) })
    }

    public static func set(_ children: [Data]) -> Data {
        tlv(tagSet, children.reduce(into: Data()) { $0.append($1) })
    }

    /// A DER INTEGER from an unsigned big-endian magnitude. DER integers are
    /// signed two's-complement, so a magnitude whose top bit is set gets a
    /// leading `0x00` to keep it positive — the same padding rule
    /// `machine_registry`'s SQLite `INTEGER` columns never have to think
    /// about, because this is the one place in this tool that produces a
    /// value someone else's ASN.1 parser will read back.
    public static func integer(unsignedMagnitude bytes: Data) -> Data {
        var b = Array(bytes)
        // Strip leading zero bytes down to the minimal magnitude — DER
        // forbids a redundant leading 0x00 that isn't needed for the sign.
        while b.count > 1 && b[0] == 0 && (b[1] & 0x80) == 0 {
            b.removeFirst()
        }
        if b.isEmpty { b = [0] }
        if b[0] & 0x80 != 0 {
            b.insert(0, at: 0)
        }
        return tlv(tagInteger, Data(b))
    }

    public static func integer(_ value: Int) -> Data {
        precondition(value >= 0, "DER.integer(_:) only builds non-negative small integers; every caller in this tool needs one")
        var b: [UInt8] = []
        var n = value
        repeat {
            b.insert(UInt8(n & 0xFF), at: 0)
            n >>= 8
        } while n > 0
        return integer(unsignedMagnitude: Data(b))
    }

    public static func boolean(_ value: Bool) -> Data {
        tlv(tagBoolean, Data([value ? 0xFF : 0x00]))
    }

    /// A BIT STRING with zero unused bits — every use in this tool wraps a
    /// byte-aligned value (a signature, or a raw EC point), never a genuine
    /// bit-level flag set.
    public static func bitString(_ bytes: Data) -> Data {
        var value = Data([0x00])
        value.append(bytes)
        return tlv(tagBitString, value)
    }

    /// A BIT STRING encoding a small named-bit set (`keyUsage`'s flags),
    /// where DER requires trailing zero bits trimmed and the count of
    /// unused trailing bits recorded — RFC 5280 section 4.2.1.3's `KeyUsage`.
    /// `bitPositions` are 0-indexed from the most significant bit of the
    /// first octet (bit 0 = `digitalSignature`, bit 5 = `keyCertSign`, ...).
    public static func namedBitString(_ bitPositions: Set<Int>) -> Data {
        guard let highest = bitPositions.max() else {
            return tlv(tagBitString, Data([0x00]))
        }
        let byteCount = highest / 8 + 1
        var bytes = [UInt8](repeating: 0, count: byteCount)
        for bit in bitPositions {
            bytes[bit / 8] |= 0x80 >> (bit % 8)
        }
        let unusedBits = 7 - (highest % 8)
        var value = Data([UInt8(unusedBits)])
        value.append(Data(bytes))
        return tlv(tagBitString, value)
    }

    public static func octetString(_ bytes: Data) -> Data {
        tlv(tagOctetString, bytes)
    }

    public static func null() -> Data {
        Data([tagNull, 0x00])
    }

    /// An OBJECT IDENTIFIER from its already-encoded content bytes (the
    /// arc-encoded body, not including the tag or length) — every OID this
    /// tool uses is a constant in `OIDs.swift`, computed once by hand and
    /// cross-checked against `machine_identity.py`'s own OID constants
    /// rather than derived at runtime from a dotted string.
    public static func oid(_ contentBytes: [UInt8]) -> Data {
        tlv(tagOID, Data(contentBytes))
    }

    public static func utf8String(_ s: String) -> Data {
        tlv(tagUTF8String, Data(s.utf8))
    }

    public static func printableString(_ s: String) -> Data {
        tlv(tagPrintableString, Data(s.utf8))
    }

    /// A UTCTime (`YYMMDDHHMMSSZ`, UTC, RFC 5280 section 4.1.2.5.1). Valid for any
    /// year 1950-2049 — every certificate this tool issues or ever will
    /// (90-day machine leaves, a root meant to be rotated on the same order
    /// of years `macbook` itself gets replaced) falls inside that window, so
    /// GeneralizedTime is never needed here.
    public static func utcTime(_ date: Date) -> Data {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let c = cal.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
        precondition((1950...2049).contains(c.year ?? 0), "UTCTime covers 1950-2049; this tool never needs GeneralizedTime")
        let yy = (c.year ?? 0) % 100
        let s = String(format: "%02d%02d%02d%02d%02d%02dZ", yy, c.month ?? 0, c.day ?? 0, c.hour ?? 0, c.minute ?? 0, c.second ?? 0)
        return tlv(tagUTCTime, Data(s.utf8))
    }
}
