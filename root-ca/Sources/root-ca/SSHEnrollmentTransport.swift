import EnclaveCore
import Foundation

/// The production `EnrollmentTransport`: shells out to `/usr/bin/ssh`
/// against `agents@<host>` for the pull and push steps, and against the
/// machine serving the Library for the registration step. **Never run against a real host by this
/// package's own test suite** — `EnrollmentTests` exercises
/// `Enrollment.enroll`'s orchestration against a stub conformer instead,
/// the same boundary `SecureEnclaveRootSigner`'s own docstring draws around
/// hardware it cannot test here either.
struct SSHEnrollmentTransport: EnrollmentTransport {
    /// The machine serving the Library, as an SSH alias or hostname from the
    /// operator's own SSH config. Required at the call site, so no machine
    /// name is written into this tool.
    let libraryHost: String
    /// The remote command that runs `enroll_machine.py`, before its `issue`
    /// arguments (`EnrollmentCommands.defaultRegisterCommand` by default).
    let registerCommand: String
    /// Where the certificate lands on the host, for the installer's
    /// `individuator adopt-cert --cert <directory>/certificate.pem`.
    let remoteCertDirectory: String

    init(
        libraryHost: String,
        registerCommand: String = EnrollmentCommands.defaultRegisterCommand,
        remoteCertDirectory: String = EnrollmentCommands.defaultCertificateDirectory
    ) {
        self.libraryHost = libraryHost
        self.registerCommand = registerCommand
        self.remoteCertDirectory = remoteCertDirectory
    }

    func fetchCertificateRequest(host: String) throws -> String {
        try runSSH(target: EnrollmentCommands.hostTarget(host),
                   command: EnrollmentCommands.fetchRequest(host: host))
    }

    func pushCertificate(host: String, certificatePEM: String) throws {
        _ = try runSSH(target: EnrollmentCommands.hostTarget(host),
                       command: EnrollmentCommands.pushCertificate(directory: remoteCertDirectory),
                       stdin: certificatePEM)
    }

    func registerMachine(host: String, fingerprintSHA256: String, notAfter: Date) throws {
        _ = try runSSH(
            target: libraryHost,
            command: EnrollmentCommands.register(
                registerCommand: registerCommand, host: host,
                fingerprintSHA256: fingerprintSHA256, notAfter: notAfter))
    }

    enum SSHError: Error, CustomStringConvertible {
        case nonZeroExit(target: String, command: String, status: Int32, stderr: String)

        var description: String {
            switch self {
            case .nonZeroExit(let target, let command, let status, let stderr):
                return "ssh \(target) \(command.debugDescription) exited \(status): \(stderr)"
            }
        }
    }

    @discardableResult
    private func runSSH(target: String, command: String, stdin: String? = nil) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        process.arguments = EnrollmentCommands.sshArguments(target: target, command: command)

        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        try process.run()
        if let stdin {
            stdinPipe.fileHandleForWriting.write(Data(stdin.utf8))
        }
        try stdinPipe.fileHandleForWriting.close()

        let outData = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        guard process.terminationStatus == 0 else {
            let message = String(data: errData, encoding: .utf8) ?? ""
            throw SSHError.nonZeroExit(
                target: target, command: command, status: process.terminationStatus, stderr: message)
        }
        return String(data: outData, encoding: .utf8) ?? ""
    }
}
