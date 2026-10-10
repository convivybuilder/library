import Foundation

public enum PEMError: Error, CustomStringConvertible {
    case noSuchLabel(String)
    case malformedBase64

    public var description: String {
        switch self {
        case .noSuchLabel(let label): return "PEM: no \"\(label)\" block found"
        case .malformedBase64: return "PEM: body did not decode as base64"
        }
    }
}

public enum PEM {
    /// `label` is the bare word between the dashes, e.g. `"CERTIFICATE"` or
    /// `"CERTIFICATE REQUEST"` — this repo's own `generate_csr.py` and
    /// `tests/tls_ca.py` both produce the latter via plain `openssl`, so this
    /// reads whatever PEM `openssl` writes without assuming which label.
    public static func encode(_ der: Data, label: String) -> String {
        let b64 = der.base64EncodedString()
        var lines: [String] = ["-----BEGIN \(label)-----"]
        var idx = b64.startIndex
        while idx < b64.endIndex {
            let end = b64.index(idx, offsetBy: 64, limitedBy: b64.endIndex) ?? b64.endIndex
            lines.append(String(b64[idx..<end]))
            idx = end
        }
        lines.append("-----END \(label)-----")
        return lines.joined(separator: "\n") + "\n"
    }

    public static func decode(_ pem: String, label: String) throws -> Data {
        let begin = "-----BEGIN \(label)-----"
        let end = "-----END \(label)-----"
        guard let beginRange = pem.range(of: begin),
              let endRange = pem.range(of: end, range: beginRange.upperBound..<pem.endIndex)
        else {
            throw PEMError.noSuchLabel(label)
        }
        let body = pem[beginRange.upperBound..<endRange.lowerBound]
            .components(separatedBy: .whitespacesAndNewlines)
            .joined()
        guard let data = Data(base64Encoded: body) else { throw PEMError.malformedBase64 }
        return data
    }
}
