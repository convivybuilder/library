import Foundation

/// The exact `ssh` arguments and remote commands `root-ca enroll` runs,
/// kept here as pure functions so the tests can pin them. The executable's
/// `SSHEnrollmentTransport` only runs what these return. Every `host` that
/// reaches them has passed `Enrollment.isValidHostName`, so it is one shell
/// word with no metacharacters.
public enum EnrollmentCommands {
    /// The certificate's directory on the host, under the `agents` account's
    /// own home. The remote shell expands `$HOME`.
    public static let defaultCertificateDirectory = "$HOME/.individuator-enroll"

    /// The remote command that runs `enroll_machine.py` on the Library's
    /// host, before its `issue` arguments. `enroll_machine.py` is on no
    /// non-interactive PATH, and `LIBRARY_DB` has no default, so this names
    /// the installed copy and the vault under the remote `$HOME`.
    public static let defaultRegisterCommand =
        "LIBRARY_DB=\"$HOME/.library/library.db\" " +
        "\"$HOME/.local/deploy/library/.venv/bin/python\" " +
        "\"$HOME/.local/deploy/library/enroll_machine.py\""

    /// `ssh`'s argv: `--` ends its options, so the target is never read as one.
    public static func sshArguments(target: String, command: String) -> [String] {
        ["--", target, command]
    }

    /// The account the pull and push steps log in as.
    public static func hostTarget(_ host: String) -> String {
        "agents@\(host)"
    }

    /// `individuator csr` prints the request to stdout when given no `--out`,
    /// so the request never touches the host's filesystem. `--machine-name`
    /// is required by `individuator`.
    public static func fetchRequest(host: String) -> String {
        "individuator csr --machine-name \(host)"
    }

    /// Writes stdin to `<directory>/certificate.pem`, creating the directory
    /// mode 0700 first, so only the `agents` account could plant a symlink
    /// in it. A fixed path in the host's shared `/tmp` let any local user
    /// redirect the write (library#303 round 1).
    public static func pushCertificate(directory: String = defaultCertificateDirectory) -> String {
        "umask 077 && mkdir -p \"\(directory)\" && chmod 700 \"\(directory)\" && " +
            "cat > \"\(directory)/certificate.pem\""
    }

    public static func register(
        registerCommand: String = defaultRegisterCommand,
        host: String, fingerprintSHA256: String, notAfter: Date
    ) -> String {
        "\(registerCommand) issue \(host) \(fingerprintSHA256) \(isoDate(notAfter)) " +
            "--custody root-file --note \"enrolled by root-ca enroll\""
    }

    /// `enroll_machine.py`'s `not_after` form: UTC, seconds, no zone suffix.
    public static func isoDate(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter.string(from: date)
    }
}
