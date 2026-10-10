#!/usr/bin/env python3.14
"""generate_csr — step 1 of platform/decisions/0066's issuance path, run on
the machine being enrolled, including the Library's own listener machine
for its `serverAuth` certificate.

**Produces a FILE-BASED key, not a Secure-Enclave-backed one, and that is a
named gap rather than an oversight.** ADR-0066 requires the enrolling
machine's key be generated in its own Secure Enclave, non-exportable
(`kSecAttrAccessibleWhenUnlockedThisDeviceOnly`). Creating a Secure Enclave
key is a Security.framework call (`SecKeyCreateRandomKey` with
`kSecAttrTokenIDSecureEnclave`) with no Python binding in this repo's
dependency set: `cryptography` is not a dependency here (see `tests/
tls_ca.py`'s own docstring), adding one for this alone would be a dependency
change out of scope for this PR, and CPython's `ssl`/`hashlib` modules do
not expose Security.framework at all. `platform/decisions/0065` names the
Individuator, written in Swift, as the client that reaches the enclave
directly — it does not exist yet. This tool is what makes the CSR step of
the issuance path runnable in the meantime.

**Whatever key this produces must be registered with `custody=file`, never
`hardware`.** `machine_registry.issue` takes `custody` as a required
argument rather than inferring it, precisely so a caller cannot forget
which this is — and platform/decisions/0066 already accepts file-based
client keys through stages B and C for the identical reason (the
Individuator is not built), so this is the same, already-named deviation
rather than a new one.

**This has never run against a real Secure Enclave or Touch ID, and cannot
on this machine** — `ioreg -c AppleBiometricSensor` on the mini this
service runs on returns zero nodes (platform/decisions/0066's own
measurement). Nothing here is evidence about the Secure Enclave property;
it is evidence only that the file-based fallback this repo can build
produces a structurally correct CSR.

Built on the system `openssl` CLI, the same choice `tests/tls_ca.py` makes
and for the same reason: adding a crypto library dependency for this alone
is out of scope for the PR that needs it.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

# **NOT IMPORTED FROM `machine_identity`, DELIBERATELY.** That module pulls
# in `mtls`, which pulls in `store`, which resolves the Library's own
# database at import time and raises if neither `LIBRARY_DB` nor
# `ORIENT_CONTROL_DB` is set. This tool runs on an enrolling machine, which
# has no reason to have either configured — the Library's own database is
# not this tool's business, and `bin/install-library` never runs before
# enrollment does. Kept a literal copy instead, and
# `test_generate_csr.py::test_the_uri_prefix_matches_machine_identity` pins
# the two constants equal so they cannot drift silently.
MACHINE_URI_PREFIX = "convivy://machine/"


def _run(*args: str, umask: int | None = None) -> None:
    kwargs = {} if umask is None else {"umask": umask}
    result = subprocess.run(
        list(args), capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


def _pubkey_sha256(key_path: Path) -> str:
    """SHA-256 over the DER encoding of `key_path`'s public key — what the
    comparison in issuance step 3 checks: the operator reads this off the requesting
    machine's own screen and matches it against what the signing tool
    prints, and that comparison is what authorizes issuance
    (platform/decisions/0066)."""
    result = subprocess.run(
        ["openssl", "pkey", "-in", str(key_path), "-pubout", "-outform",
         "DER"], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"could not read the public key back out of {key_path}: "
            f"{result.stderr.decode(errors='replace')}")
    return hashlib.sha256(result.stdout).hexdigest()


def generate(machine: str, out_dir: Path, *,
             server: bool = False) -> tuple[Path, Path]:
    """A P-256 keypair and a CSR for `machine`, written into `out_dir` as
    `<machine>.key` and `<machine>.csr`. Returns `(key_path, csr_path)`.

    **Exactly one URI SAN, `convivy://machine/<machine>`, and the common
    name carries the same string** (ADR-0066's Handoff). CN alone would not
    be enough: `machine_identity.uri_names` is the only field the Library
    ever reads as identity, and it never reads the common name.

    `server=True` requests `serverAuth` instead of `clientAuth` and adds
    `DNS:localhost` and `IP:127.0.0.1` — "a tunnelled client dials its own
    loopback" (ADR-0066) — for the Library's own listener certificate.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    key_path = out_dir / f"{machine}.key"
    csr_path = out_dir / f"{machine}.csr"

    # This is the Library's TLS server key (or a machine's client key); its
    # mode must be 0o600 regardless of which openssl build made it — the
    # LibreSSL on macOS's system PATH (`/usr/bin/openssl`) leaves a key file
    # at the umask's default 0o644, unlike Homebrew's OpenSSL, which chmods
    # it 0o600 itself. Delete any old key first: opening an existing file
    # for writing keeps that file's mode, umask or not, so a rerun over a
    # stale 0o644 key would otherwise stay 0o644. The umask passed to this
    # one call (not a process-wide `os.umask`, which would leak into
    # anything else this interpreter does) keeps the key from ever landing
    # world- or group-readable, even for the instant before the chmod below.
    key_path.unlink(missing_ok=True)
    _run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
         "-out", str(key_path), umask=0o077)
    os.chmod(key_path, 0o600)
    actual_mode = stat.S_IMODE(os.stat(key_path).st_mode)
    if actual_mode != 0o600:
        raise RuntimeError(
            f"{key_path} ended at mode {oct(actual_mode)}, not 0o600 — "
            "refusing to leave a private key with a permissive mode")

    uri = f"{MACHINE_URI_PREFIX}{machine}"
    sans = [f"URI:{uri}"]
    if server:
        sans += ["DNS:localhost", "IP:127.0.0.1"]
    eku = "serverAuth" if server else "clientAuth"

    _run("openssl", "req", "-new", "-key", str(key_path), "-subj",
         f"/CN={machine}",
         "-addext", f"subjectAltName={','.join(sans)}",
         "-addext", f"extendedKeyUsage={eku}",
         "-out", str(csr_path))
    return key_path, csr_path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("machine")
    p.add_argument("--out-dir", type=Path, default=Path.cwd())
    p.add_argument(
        "--server", action="store_true",
        help="request serverAuth with DNS:localhost/IP:127.0.0.1, for the "
             "Library's own listener certificate rather than a machine "
             "(client) certificate")
    args = p.parse_args(argv)

    try:
        key_path, csr_path = generate(args.machine, args.out_dir,
                                       server=args.server)
        fingerprint = _pubkey_sha256(key_path)
    except RuntimeError as e:
        print(f"generate_csr: {e}", file=sys.stderr)
        return 1

    print(f"key: {key_path}")
    print(f"csr: {csr_path}")
    print("custody: file — this key was generated in software, not a "
          "Secure Enclave; register it with custody=file")
    print("public key SHA-256 — compare this on the requesting machine's "
          "own screen against what the signing tool prints:")
    print(f"  {fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
