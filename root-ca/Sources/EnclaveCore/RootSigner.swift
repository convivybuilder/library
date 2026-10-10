import Foundation

/// Anything that can produce ECDSA-P256-SHA256 signatures on behalf of the
/// root, without exposing the private key material itself.
///
/// **The only production conformer is `SecureEnclaveRootSigner`**, in the
/// `root-ca` executable target, not here. This protocol exists so
/// `RootCertificate.generateSelfSigned` and `LeafCertificate.issue` — the
/// code that builds the actual certificate bytes — can be exercised by tests
/// against a software P-256 key, the same "throwaway key clearly named as
/// such" `tests/tls_ca.py` uses on the Python side, without ever touching a
/// keychain or a Secure Enclave. A fake conformer belongs in test code only:
/// nothing here or in the CLI is allowed to construct one.
public protocol RootSigner {
    /// The root's own public key, as a raw uncompressed P-256 point
    /// (`0x04 || X(32) || Y(32)`) — `SecKeyCopyExternalRepresentation`'s
    /// external representation for an EC public key.
    var publicKeyRawPoint: Data { get }

    /// Signs `message` (the exact bytes to hash-and-sign — a TBSCertificate,
    /// never anything pre-hashed), returning the raw `ECDSA-Sig-Value` DER
    /// bytes ready to embed in a certificate's `signatureValue`.
    ///
    /// **For the Secure Enclave conformer, calling this is the moment Touch
    /// ID is asked for** (platform/decisions/0066, issuance step 4: "Touch ID
    /// releases the root key; the certificate is signed"). Nothing about
    /// this protocol's shape enforces that — it is a property of the one
    /// production conformer's key's access control, not of this interface —
    /// which is exactly why every caller of `sign` must have already printed
    /// the public-key fingerprint for the operator to compare *before*
    /// calling it: the signature this method returns is not itself evidence
    /// that the request was inspected, only that a human's finger authorized
    /// *a* signature (see this type's docstring, and `CertificateRequest`'s).
    func sign(_ message: Data) throws -> Data
}
