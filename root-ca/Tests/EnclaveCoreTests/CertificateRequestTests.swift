import CryptoKit
import Foundation
import XCTest
@testable import EnclaveCore

final class CertificateRequestTests: XCTestCase {
    func testParsesAndVerifiesARealCSR() throws {
        let generated = try TestCSRBuilder.makeP256CSR(commonName: "macbook")
        let csr = try CertificateRequest.parse(pem: generated.pem)
        XCTAssertEqual(csr.commonName, "macbook")
        XCTAssertEqual(csr.rawECPoint, generated.rawECPoint)
        XCTAssertEqual(csr.rawECPoint.count, 65)
        XCTAssertEqual(csr.rawECPoint.first, 0x04)

        // The SPKI embedded in the CSR is reused verbatim, never rebuilt —
        // so its fingerprint must exactly match SHA-256 over the SPKI this
        // tool would independently construct from the same raw point.
        let independentSPKI = X509.spki(rawECPoint: generated.rawECPoint)
        let independentFingerprint = SHA256.hash(data: independentSPKI).map { String(format: "%02x", $0) }.joined()
        XCTAssertEqual(Fingerprint.publicKey(spkiDER: csr.spkiDER), independentFingerprint)
    }

    func testRefusesACSRWithATamperedSignature() throws {
        var der = try TestCSRBuilder.makeP256CSR(commonName: "macbook").der
        // Flip the last byte, inside the signature BIT STRING, so the
        // request no longer proves possession of the matching key.
        der[der.count - 1] ^= 0xFF

        XCTAssertThrowsError(try CertificateRequest.parse(der: der)) { error in
            guard case CSRError.invalidSelfSignature = error else {
                return XCTFail("expected .invalidSelfSignature, got \(error)")
            }
        }
    }

    func testRefusesANonECKeyAlgorithm() throws {
        let der = try TestCSRBuilder.makeCSRWithWrongKeyAlgorithm(commonName: "not-p256")
        XCTAssertThrowsError(try CertificateRequest.parse(der: der)) { error in
            guard case CSRError.unsupportedKeyAlgorithm = error else {
                return XCTFail("expected .unsupportedKeyAlgorithm, got \(error)")
            }
        }
    }

    func testMissingPEMLabelThrows() {
        XCTAssertThrowsError(try CertificateRequest.parse(pem: "not a pem file")) { error in
            guard case PEMError.noSuchLabel = error else {
                return XCTFail("expected PEMError.noSuchLabel, got \(error)")
            }
        }
    }
}
