import Foundation

/// Every OID this tool reads or writes, as pre-encoded DER content bytes
/// (`DER.oid` wraps these in the tag and length). Computed by hand from the
/// dotted form once, and cross-checked rather than derived independently
/// where the Python side of this design already encodes the same arc:
/// `id-ecPublicKey` bytes match `test_no_early_data_interface.py`'s sibling
/// modules nowhere, but `idKpClientAuth` below is byte-for-byte
/// `machine_identity.CLIENT_AUTH_OID`, and `idCeExtKeyUsage` is byte-for-byte
/// `machine_identity._EXT_KEY_USAGE_OID` — the two places this tool's output
/// and that module's input must agree on what an OID means.
public enum OIDs {
    /// 1.2.840.10045.2.1 — id-ecPublicKey, `SubjectPublicKeyInfo.algorithm`
    /// for every key in this tool's certificates.
    public static let idEcPublicKey: [UInt8] = [0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x02, 0x01]

    /// 1.2.840.10045.3.1.7 — prime256v1 (secp256r1, "P-256"), the only curve
    /// the Secure Enclave supports and so the only one this tool ever emits.
    public static let prime256v1: [UInt8] = [0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x03, 0x01, 0x07]

    /// 1.2.840.10045.4.3.2 — ecdsa-with-SHA256, the signature algorithm on
    /// every certificate this tool issues and the only one it accepts on an
    /// incoming CSR.
    public static let ecdsaWithSHA256: [UInt8] = [0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x04, 0x03, 0x02]

    /// 2.5.4.3 — commonName.
    public static let commonName: [UInt8] = [0x55, 0x04, 0x03]

    /// 2.5.29.17 — id-ce-subjectAltName.
    public static let idCeSubjectAltName: [UInt8] = [0x55, 0x1D, 0x11]

    /// 2.5.29.37 — id-ce-extKeyUsage. Byte-for-byte
    /// `machine_identity._EXT_KEY_USAGE_OID`.
    public static let idCeExtKeyUsage: [UInt8] = [0x55, 0x1D, 0x25]

    /// 2.5.29.19 — id-ce-basicConstraints.
    public static let idCeBasicConstraints: [UInt8] = [0x55, 0x1D, 0x13]

    /// 2.5.29.15 — id-ce-keyUsage.
    public static let idCeKeyUsage: [UInt8] = [0x55, 0x1D, 0x0F]

    /// 1.3.6.1.5.5.7.3.2 — id-kp-clientAuth. Byte-for-byte
    /// `machine_identity.CLIENT_AUTH_OID`.
    public static let idKpClientAuth: [UInt8] = [0x2B, 0x06, 0x01, 0x05, 0x05, 0x07, 0x03, 0x02]

    /// 1.3.6.1.5.5.7.3.1 — id-kp-serverAuth.
    public static let idKpServerAuth: [UInt8] = [0x2B, 0x06, 0x01, 0x05, 0x05, 0x07, 0x03, 0x01]

    /// 2.25.332679538808142797592760871621889796493 — the signing purpose a
    /// person's signing certificate carries in `extendedKeyUsage`
    /// (platform/contracts/library-protocol §2.2). The arc is ITU-T X.667's
    /// UUID arc, which needs no registration; the UUID is
    /// `fa47bfa2-1f8a-4d3d-8e88-d3cab3caa98d`.
    public static let idKpSigning: [UInt8] = [
        0x69, 0x83, 0xf4, 0xc7, 0xdf, 0xe8, 0xc3, 0xf8, 0xd2, 0xb4,
        0xfb, 0x8e, 0xc4, 0xb4, 0xf9, 0xab, 0x9e, 0xaa, 0xd3, 0x0d,
    ]
}
