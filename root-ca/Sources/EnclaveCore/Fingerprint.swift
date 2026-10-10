import CryptoKit
import Foundation

/// The two SHA-256 fingerprints this tool ever computes, kept as two
/// distinctly-named functions rather than one generic `sha256Hex` — they
/// hash different things, for different readers, and confusing them is
/// exactly the mistake platform/decisions/0066's issuance step is built to
/// make impossible for a human to make silently.
public enum Fingerprint {
    /// SHA-256 over a public key's DER `SubjectPublicKeyInfo` — what
    /// `generate_csr.py`'s `_pubkey_sha256` prints on the requesting
    /// machine's own screen, and what `sign` must print identically here so
    /// the operator's visual comparison (platform/decisions/0066, issuance step 3) is
    /// actually comparing the same value on both sides.
    public static func publicKey(spkiDER: Data) -> String {
        sha256Hex(spkiDER)
    }

    /// SHA-256 over a certificate's full DER encoding — what
    /// `machine_identity.sha256_der` computes from the peer certificate on
    /// every mutual-TLS request, and so the exact value `enroll_machine.py
    /// issue` must be given as `fingerprint_sha256` for the registry row this
    /// leaf's traffic will be checked against.
    public static func certificate(der: Data) -> String {
        sha256Hex(der)
    }

    private static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
