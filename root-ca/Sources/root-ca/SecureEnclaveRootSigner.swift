import CryptoKit
import EnclaveCore
import Foundation
import Security

/// **NOTHING IN THIS FILE HAS EVER RUN AGAINST REAL HARDWARE.**
/// `ioreg -c AppleBiometricSensor` on this machine returns zero nodes
/// (confirmed 2026-09-05 — the same measurement platform/decisions/0066
/// itself records for the Library's mini, and the reason the root does not
/// live there). `generate(label:)` and `sign(_:)` below both compile and
/// both call real CryptoKit Secure Enclave APIs, but neither has been exercised by
/// anything — not this PR's test suite, not a manual run — because there is
/// no Secure Enclave on this machine to exercise them against. Everything
/// that does not require actual hardware (every byte of DER this tool
/// produces, CSR parsing and self-signature verification, the CLI's argument
/// handling) is isolated in `EnclaveCore` and in `RootSigner`-conforming test
/// doubles instead, and is tested. This file is the one boundary that isn't.
enum SecureEnclaveError: Error, CustomStringConvertible {
    case blobStorageError(String)
    case keyGenerationFailed(String)
    case keyAlreadyExists
    case keyNotFound
    case publicKeyExportFailed(String)
    case signingFailed(String)

    var description: String {
        switch self {
        case .blobStorageError(let reason):
            return "could not read or write the root key blob: \(reason)"
        case .keyGenerationFailed(let reason):
            return "could not generate the Secure Enclave key: \(reason)"
        case .keyAlreadyExists:
            return "a root key blob already exists — generate-root does not overwrite one, " +
                "since that would strand every certificate it already signed"
        case .keyNotFound:
            return "no Secure Enclave root key blob found — run generate-root first"
        case .publicKeyExportFailed(let reason):
            return "could not read the public key back out of the Secure Enclave: \(reason)"
        case .signingFailed(let reason):
            return "signing failed (Touch ID declined, cancelled, or the sensor timed out): \(reason)"
        }
    }
}

/// A `RootSigner` backed by a non-exportable P-256 key generated inside
/// `macbook`'s Secure Enclave, biometry-bound to the currently enrolled
/// fingerprint set — platform/decisions/0066's Handoff: a Secure Enclave key
/// with a biometry-bound access control "on the current enrolled set, so
/// re-enrolling a fingerprint invalidates the key rather than extending it to
/// a new finger". That is `.biometryCurrentSet`, never `.biometryAny`, and it
/// is the one line in this whole tool where getting the flag wrong silently
/// weakens the control this entire ADR exists to build.
///
/// **The key is not a keychain item.** A permanent Secure Enclave key in
/// Security.framework lands in the data-protection keychain, which needs a
/// `keychain-access-groups` entitlement that an unsigned `swift run` build
/// cannot carry (`SecKeyCreateRandomKey` fails with -34018,
/// errSecMissingEntitlement). CryptoKit's `SecureEnclave.P256.Signing.PrivateKey`
/// needs no entitlement; its `dataRepresentation` is a blob wrapped by this
/// device's Secure Enclave. The blob holds no key material and is useless on
/// any other machine, so persisting it as a file keeps the key non-exportable
/// and Touch-ID-gated. It is stored at `blobURL`.
final class SecureEnclaveRootSigner: RootSigner {
    /// The wrapped-key blob's fixed location, so `load()` finds the same key
    /// across invocations. The directory is 0700 and the file 0600.
    static var blobURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".library-root-ca", isDirectory: true)
            .appendingPathComponent("root-key.se-blob")
    }

    private let privateKey: SecureEnclave.P256.Signing.PrivateKey
    let publicKeyRawPoint: Data

    private init(privateKey: SecureEnclave.P256.Signing.PrivateKey) {
        self.privateKey = privateKey
        // x963Representation is the raw uncompressed point
        // (0x04 || X || Y), the same bytes SecKeyCopyExternalRepresentation
        // gave for an EC public key.
        self.publicKeyRawPoint = privateKey.publicKey.x963Representation
    }

    /// Generates a brand-new root key. Refuses if a blob already exists —
    /// `generate-root` is a one-time act per root; overwriting silently would
    /// stop every certificate the old root signed from chaining to anything
    /// the Library still trusts.
    ///
    /// `label` is accepted so `--label` keeps parsing, but nothing stores it:
    /// the key has no keychain item to label.
    static func generate(label: String) throws -> SecureEnclaveRootSigner {
        let url = blobURL
        if FileManager.default.fileExists(atPath: url.path) {
            throw SecureEnclaveError.keyAlreadyExists
        }

        var acError: Unmanaged<CFError>?
        guard let accessControl = SecAccessControlCreateWithFlags(
            kCFAllocatorDefault,
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            [.privateKeyUsage, .biometryCurrentSet],
            &acError
        ) else {
            throw SecureEnclaveError.keyGenerationFailed(describeCFError(acError))
        }

        let privateKey: SecureEnclave.P256.Signing.PrivateKey
        do {
            privateKey = try SecureEnclave.P256.Signing.PrivateKey(accessControl: accessControl)
        } catch {
            throw SecureEnclaveError.keyGenerationFailed(String(describing: error))
        }

        // Persist before returning: a key whose blob was never written can
        // never sign again, so the caller must not go on to issue a root
        // certificate for it.
        do {
            let dir = url.deletingLastPathComponent()
            try FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
            // createDirectory leaves an already-present directory's mode alone.
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir.path)
            try privateKey.dataRepresentation.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            throw SecureEnclaveError.blobStorageError(String(describing: error))
        }
        return SecureEnclaveRootSigner(privateKey: privateKey)
    }

    /// Loads the previously-generated root key from its blob. There is no
    /// fallback path from "no blob found" to signing with anything else — a
    /// missing key is a refusal, not a prompt to fall back to another key
    /// source, which is exactly the shape platform/decisions/0066 forbids for
    /// a root.
    static func load() throws -> SecureEnclaveRootSigner {
        let url = blobURL
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw SecureEnclaveError.keyNotFound
        }
        let blob: Data
        do {
            blob = try Data(contentsOf: url)
        } catch {
            throw SecureEnclaveError.blobStorageError(String(describing: error))
        }
        do {
            return SecureEnclaveRootSigner(
                privateKey: try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob))
        } catch {
            throw SecureEnclaveError.keyGenerationFailed(
                "the blob at \(url.path) is not a key this Secure Enclave can load: \(error)")
        }
    }

    /// **This call is what asks Touch ID to release the root** — a signature
    /// by a key whose access control requires `.biometryCurrentSet` blocks
    /// synchronously on the system biometric prompt (platform/decisions/0066,
    /// issuance step 4). By the time a caller reaches this, it must already
    /// have printed the public-key fingerprint for the operator's out-of-band
    /// comparison — see `RootSigner.sign`'s docstring for why the two are
    /// separate steps that do different jobs.
    func sign(_ message: Data) throws -> Data {
        do {
            // Hashes with SHA-256; derRepresentation is the ECDSA-Sig-Value
            // DER the certificate builder embeds.
            return try privateKey.signature(for: message).derRepresentation
        } catch {
            throw SecureEnclaveError.signingFailed(String(describing: error))
        }
    }
}

private func describeCFError(_ error: Unmanaged<CFError>?) -> String {
    guard let error else { return "unknown error" }
    return String(describing: error.takeRetainedValue())
}
