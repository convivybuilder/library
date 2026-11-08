import Foundation
import XCTest
@testable import EnclaveCore

/// `Enrollment.enroll`'s own test suite (CQ·223): exercises the
/// fingerprint-gate, signing, and ordering entirely against
/// `StubEnrollmentTransport` and `ThrowawaySoftwareSigner` — never a real
/// SSH connection, never the Secure Enclave. `IssuanceIntegrationTests`
/// already covers the certificate bytes `LeafCertificate.issue` produces;
/// these tests cover the orchestration around that call instead.
final class EnrollmentTests: XCTestCase {
    private func makeRootAndCSR(host: String = "host-example") throws
        -> (signer: ThrowawaySoftwareSigner, issuerCommonName: String, csrPEM: String, rawECPoint: Data) {
        let signer = try ThrowawaySoftwareSigner.generate()
        let generated = try TestCSRBuilder.makeP256CSR(commonName: host)
        return (signer, "Test Root", generated.pem, generated.rawECPoint)
    }

    func testConfirmedMatchingFingerprintSignsPushesAndRegistersWithTheComputedValues() throws {
        let (signer, issuerCommonName, csrPEM, rawECPoint) = try makeRootAndCSR()
        let transport = StubEnrollmentTransport()
        transport.csrPEMToReturn = csrPEM

        var printedFingerprints: [String] = []
        var confirmedFingerprints: [String] = []
        let now = Date().addingTimeInterval(-60)

        let result = try Enrollment.enroll(
            host: "host-example", issuerCommonName: issuerCommonName, days: 90, now: now,
            transport: transport, signer: signer,
            printFingerprint: { printedFingerprints.append($0) },
            confirm: { fingerprint in
                confirmedFingerprints.append(fingerprint)
                return true
            })

        let expectedFingerprint = Fingerprint.publicKey(spkiDER: X509.spki(rawECPoint: rawECPoint))
        XCTAssertEqual(printedFingerprints, [expectedFingerprint],
                        "the fingerprint must be printed before confirmation is asked for")
        XCTAssertEqual(confirmedFingerprints, [expectedFingerprint],
                        "confirm must see the same fingerprint that was printed")
        XCTAssertEqual(result.publicKeyFingerprint, expectedFingerprint)

        XCTAssertEqual(transport.fetchedHosts, ["host-example"])
        XCTAssertEqual(transport.pushedCertificates.count, 1)
        XCTAssertEqual(transport.pushedCertificates[0].host, "host-example")
        XCTAssertEqual(transport.pushedCertificates[0].certificatePEM, result.certificatePEM)

        XCTAssertEqual(transport.registeredMachines.count, 1)
        let registered = transport.registeredMachines[0]
        XCTAssertEqual(registered.host, "host-example")
        XCTAssertEqual(registered.fingerprintSHA256, result.certificateFingerprint,
                        "the registry call gets the certificate's own fingerprint, not the public-key one")
        XCTAssertEqual(registered.notAfter, now.addingTimeInterval(90 * 86400))
        XCTAssertEqual(result.notAfter, now.addingTimeInterval(90 * 86400))
    }

    func testAHostNameThatSSHOrAShellWouldReadAsMoreThanANameIsRefusedBeforeAnySSH() throws {
        let (signer, issuerCommonName, csrPEM, _) = try makeRootAndCSR()
        for host in ["", "-oProxyCommand=x", "host example", "host;x", "host$(x)", "agents@host", ".host"] {
            let transport = StubEnrollmentTransport()
            transport.csrPEMToReturn = csrPEM
            XCTAssertThrowsError(try Enrollment.enroll(
                host: host, issuerCommonName: issuerCommonName, days: 90,
                transport: transport, signer: signer,
                printFingerprint: { _ in }, confirm: { _ in true })) { error in
                guard case EnrollmentError.invalidHost(let refused) = error else {
                    return XCTFail("expected .invalidHost for \(host.debugDescription), got \(error)")
                }
                XCTAssertEqual(refused, host)
            }
            XCTAssertTrue(transport.fetchedHosts.isEmpty, "\(host.debugDescription) must reach no SSH")
        }
    }

    func testOrdinaryHostNamesAreAccepted() {
        for host in ["macbook", "host-example", "keel.host.example", "Mac-Studio2"] {
            XCTAssertTrue(Enrollment.isValidHostName(host), host)
        }
    }

    func testDeclinedConfirmationSignsPushesAndRegistersNothing() throws {
        let (signer, issuerCommonName, csrPEM, _) = try makeRootAndCSR()
        let transport = StubEnrollmentTransport()
        transport.csrPEMToReturn = csrPEM

        XCTAssertThrowsError(try Enrollment.enroll(
            host: "host-example", issuerCommonName: issuerCommonName, days: 90,
            transport: transport, signer: signer,
            printFingerprint: { _ in },
            confirm: { _ in false })) { error in
            guard case EnrollmentError.declined(let host) = error else {
                return XCTFail("expected .declined, got \(error)")
            }
            XCTAssertEqual(host, "host-example")
        }

        XCTAssertEqual(transport.fetchedHosts, ["host-example"], "the request is still pulled before the decline")
        XCTAssertTrue(transport.pushedCertificates.isEmpty, "a decline must push no certificate")
        XCTAssertTrue(transport.registeredMachines.isEmpty, "a decline must register nothing")
    }

    func testSSHFailureWhilePullingTheRequestStopsWithThatStepNamedAndRegistersNothing() throws {
        let (signer, issuerCommonName, _, _) = try makeRootAndCSR()
        let transport = StubEnrollmentTransport()
        transport.fetchError = StubSSHFailure(message: "connection refused")

        XCTAssertThrowsError(try Enrollment.enroll(
            host: "host-example", issuerCommonName: issuerCommonName, days: 90,
            transport: transport, signer: signer,
            printFingerprint: { _ in XCTFail("the fingerprint is never read if the pull failed") },
            confirm: { _ in XCTFail("confirm is never asked if the pull failed"); return true })) { error in
            guard case EnrollmentError.transportFailed(let step, _) = error else {
                return XCTFail("expected .transportFailed, got \(error)")
            }
            XCTAssertEqual(step, .pullingTheCertificateRequest)
            XCTAssertTrue("\(error)".contains(step.description),
                           "the error message must name the step that failed")
        }

        XCTAssertTrue(transport.pushedCertificates.isEmpty)
        XCTAssertTrue(transport.registeredMachines.isEmpty)
    }

    func testSSHFailureWhilePushingTheCertificateStopsWithThatStepNamedAndRegistersNothing() throws {
        let (signer, issuerCommonName, csrPEM, _) = try makeRootAndCSR()
        let transport = StubEnrollmentTransport()
        transport.csrPEMToReturn = csrPEM
        transport.pushError = StubSSHFailure(message: "broken pipe")

        XCTAssertThrowsError(try Enrollment.enroll(
            host: "host-example", issuerCommonName: issuerCommonName, days: 90,
            transport: transport, signer: signer,
            printFingerprint: { _ in },
            confirm: { _ in true })) { error in
            guard case EnrollmentError.transportFailed(let step, _) = error else {
                return XCTFail("expected .transportFailed, got \(error)")
            }
            XCTAssertEqual(step, .pushingTheCertificate)
            XCTAssertTrue("\(error)".contains(step.description),
                           "the error message must name the step that failed")
        }

        XCTAssertEqual(transport.fetchedHosts, ["host-example"], "the request was pulled and signed before the push failed")
        XCTAssertTrue(transport.registeredMachines.isEmpty, "a failed push must not be followed by registration")
    }

    func testSSHFailureWhileRegisteringTheMachineStopsWithThatStepNamedButTheCertificateWasAlreadyPushed() throws {
        let (signer, issuerCommonName, csrPEM, _) = try makeRootAndCSR()
        let transport = StubEnrollmentTransport()
        transport.csrPEMToReturn = csrPEM
        transport.registerError = StubSSHFailure(message: "host unreachable")

        XCTAssertThrowsError(try Enrollment.enroll(
            host: "host-example", issuerCommonName: issuerCommonName, days: 90,
            transport: transport, signer: signer,
            printFingerprint: { _ in },
            confirm: { _ in true })) { error in
            guard case EnrollmentError.transportFailed(let step, _) = error else {
                return XCTFail("expected .transportFailed, got \(error)")
            }
            XCTAssertEqual(step, .registeringTheMachine)
            XCTAssertTrue("\(error)".contains(step.description),
                           "the error message must name the step that failed")
        }

        // The push already happened — the registration step is the one
        // that failed, and this test exists to pin that it is reported as
        // such rather than read back as "nothing happened at all".
        XCTAssertEqual(transport.pushedCertificates.count, 1)
        XCTAssertTrue(transport.registeredMachines.isEmpty)
    }
}

private struct StubSSHFailure: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

/// A stub `EnrollmentTransport`: never opens a connection, records every
/// call it receives, and fails exactly the step a test asks it to.
private final class StubEnrollmentTransport: EnrollmentTransport {
    var csrPEMToReturn: String?
    var fetchError: Error?
    var pushError: Error?
    var registerError: Error?

    private(set) var fetchedHosts: [String] = []
    private(set) var pushedCertificates: [(host: String, certificatePEM: String)] = []
    private(set) var registeredMachines: [(host: String, fingerprintSHA256: String, notAfter: Date)] = []

    func fetchCertificateRequest(host: String) throws -> String {
        fetchedHosts.append(host)
        if let fetchError { throw fetchError }
        guard let csrPEMToReturn else {
            XCTFail("fetchCertificateRequest called with no csrPEMToReturn set")
            throw StubSSHFailure(message: "no CSR configured")
        }
        return csrPEMToReturn
    }

    func pushCertificate(host: String, certificatePEM: String) throws {
        if let pushError { throw pushError }
        pushedCertificates.append((host, certificatePEM))
    }

    func registerMachine(host: String, fingerprintSHA256: String, notAfter: Date) throws {
        if let registerError { throw registerError }
        registeredMachines.append((host, fingerprintSHA256, notAfter))
    }
}
