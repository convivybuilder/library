import Foundation

public enum DERError: Error, CustomStringConvertible {
    case truncated
    case unexpectedTag(expected: UInt8, found: UInt8)
    case malformed(String)

    public var description: String {
        switch self {
        case .truncated: return "DER: truncated input"
        case .unexpectedTag(let expected, let found):
            return "DER: expected tag 0x\(String(expected, radix: 16)), found 0x\(String(found, radix: 16))"
        case .malformed(let reason): return "DER: malformed (\(reason))"
        }
    }
}

/// One definite-length TLV, read at a given offset.
public struct DERNode {
    public let tag: UInt8
    /// The content bytes only (excludes the tag and length octets), as an
    /// ABSOLUTE range into whatever `Data` produced this node — always safe
    /// to subscript directly (`data[node.content]`), whether `data` is a
    /// fresh zero-based buffer or a slice of a larger one.
    public let content: Range<Int>
    /// The full TLV, tag through the last content byte, also absolute —
    /// what a signature over this node covers, and what `CertificateBuilder`
    /// re-embeds verbatim when it reuses a CSR's SubjectPublicKeyInfo.
    public let whole: Range<Int>
    /// The offset immediately after this TLV, RELATIVE to the `Data` this
    /// node was read from (i.e. what to pass as `at:` to read a sibling) —
    /// this one field is not absolute, deliberately: it is only ever handed
    /// back into `readTLV`/`children`, which both expect a relative offset.
    public let next: Int
}

/// A minimal definite-length DER reader — mirrors `machine_identity.py`'s
/// `_der_tlv`/`_der_children` on the Python side of this design, deliberately
/// kept to the same low-tag-number, definite-length subset: every structure
/// this tool ever reads (a CSR this repo's own `generate_csr.py` produced) is
/// built by `openssl`, which never emits indefinite-length or high-tag-number
/// (tag >= 31) encodings.
///
/// **`at offset:` is always relative to `data.startIndex`, never an absolute
/// index** — so a top-level parse always starts `at: 0`, whether `data` is a
/// freshly-built buffer or a `Data` slice sharing storage with a larger one
/// (as `CertificateRequest` produces via `der[spkiNode.whole]`, to reuse a
/// CSR's SubjectPublicKeyInfo verbatim). Getting this arithmetic wrong for
/// slices is not hypothetical: an earlier version of this function computed
/// `contentStart` from `offset` alone, without `data.startIndex`, which is
/// invisible against a zero-based `Data` (offset and absolute index
/// coincide) and traps immediately — `Data`'s subscript is
/// precondition-checked — the first time this function is handed a slice
/// with a non-zero `startIndex`. Every one of `EnclaveCoreTests`'s
/// synthetic fixtures happens to be zero-based, which is exactly why that
/// bug shipped past them; the crash first surfaced against a real CSR's
/// embedded SPKI, which is a slice. This docstring is here so the next
/// change to this function keeps the base-offset arithmetic in one place
/// rather than rediscovering why it matters.
public enum DERReader {
    public static func readTLV(_ data: Data, at offset: Int) throws -> DERNode {
        let base = data.startIndex
        let tagIndex = base + offset
        guard tagIndex < data.endIndex else { throw DERError.truncated }
        let tag = data[tagIndex]
        let lengthIndex = tagIndex + 1
        guard lengthIndex < data.endIndex else { throw DERError.truncated }
        let lengthOctet = data[lengthIndex]
        var contentStart = lengthIndex + 1
        var length: Int
        if lengthOctet & 0x80 == 0 {
            length = Int(lengthOctet)
        } else {
            let n = Int(lengthOctet & 0x7F)
            guard n > 0, n <= 8, contentStart + n <= data.endIndex else {
                throw DERError.malformed("length octet count")
            }
            var value = 0
            for i in 0..<n {
                value = (value << 8) | Int(data[contentStart + i])
            }
            length = value
            contentStart += n
        }
        let contentEnd = contentStart + length
        guard contentEnd <= data.endIndex else { throw DERError.truncated }
        return DERNode(
            tag: tag,
            content: contentStart..<contentEnd,
            whole: tagIndex..<contentEnd,
            next: contentEnd - base)
    }

    /// Every top-level TLV inside `range` — an ABSOLUTE range, as produced by
    /// a previous node's `.content` or `.whole` (a SEQUENCE or SET the
    /// caller has already unwrapped).
    public static func children(_ data: Data, in range: Range<Int>) throws -> [DERNode] {
        let base = data.startIndex
        var out: [DERNode] = []
        var offset = range.lowerBound - base
        while base + offset < range.upperBound {
            let node = try readTLV(data, at: offset)
            out.append(node)
            offset = node.next
        }
        guard base + offset == range.upperBound else {
            throw DERError.malformed("children overran their parent's length")
        }
        return out
    }

    public static func expect(_ node: DERNode, tag: UInt8) throws {
        guard node.tag == tag else { throw DERError.unexpectedTag(expected: tag, found: node.tag) }
    }
}
