import Foundation
import XCTest
@testable import EnclaveCore

/// Pins the exact `ssh` arguments and remote commands `root-ca enroll` runs.
/// `EnrollmentTests` stubs the transport, so these are what catch a remote
/// command that `individuator` or `enroll_machine.py` would refuse.
final class EnrollmentCommandsTests: XCTestCase {
    func testSSHArgumentsEndOptionsBeforeTheTarget() {
        XCTAssertEqual(
            EnrollmentCommands.sshArguments(target: "agents@host-example", command: "true"),
            ["--", "agents@host-example", "true"])
    }

    func testTheRequestIsReadFromStdoutWithTheRequiredMachineName() {
        let command = EnrollmentCommands.fetchRequest(host: "host-example")
        XCTAssertEqual(command, "individuator csr --machine-name host-example")
        XCTAssertFalse(command.contains("--out"), "the request must not pass through a file on the host")
    }

    func testTheCertificateIsWrittenUnderAPrivateDirectoryInTheAccountsHome() {
        let command = EnrollmentCommands.pushCertificate()
        XCTAssertEqual(
            command,
            "umask 077 && mkdir -p \"$HOME/.individuator-enroll\" && " +
                "chmod 700 \"$HOME/.individuator-enroll\" && " +
                "cat > \"$HOME/.individuator-enroll/certificate.pem\"")
        XCTAssertFalse(command.contains("/tmp"), "a shared /tmp lets another local user redirect the write")
    }

    func testRegistrationRunsTheInstalledEnrollMachineWithTheComputedValues() {
        let notAfter = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertEqual(
            EnrollmentCommands.register(
                host: "host-example", fingerprintSHA256: String(repeating: "ab", count: 32),
                notAfter: notAfter),
            "LIBRARY_DB=\"$HOME/.library/library.db\" " +
                "\"$HOME/.local/deploy/library/.venv/bin/python\" " +
                "\"$HOME/.local/deploy/library/enroll_machine.py\" " +
                "issue host-example \(String(repeating: "ab", count: 32)) 2027-01-15T08:00:00 " +
                "--custody root-file --note \"enrolled by root-ca enroll\"")
    }

    func testAnOverriddenRegisterCommandReplacesOnlyThePrefix() {
        let command = EnrollmentCommands.register(
            registerCommand: "enroll_machine.py", host: "h", fingerprintSHA256: "f",
            notAfter: Date(timeIntervalSince1970: 0))
        XCTAssertEqual(command, "enroll_machine.py issue h f 1970-01-01T00:00:00 " +
            "--custody root-file --note \"enrolled by root-ca enroll\"")
    }
}
