// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "root-ca",
    platforms: [.macOS(.v13)],
    targets: [
        // ASN.1/X.509 encoding and decoding, CSR verification, and certificate
        // assembly — every part of this tool that touches no keychain and no
        // Secure Enclave, so it runs and tests the same on any Mac.
        .target(name: "EnclaveCore"),
        // The CLI. Owns the one boundary this package has with hardware: key
        // generation and signing through CryptoKit's Secure Enclave
        // APIs, in SecureEnclaveRootSigner.swift.
        .executableTarget(name: "root-ca", dependencies: ["EnclaveCore"]),
        .testTarget(name: "EnclaveCoreTests", dependencies: ["EnclaveCore"]),
    ]
)
