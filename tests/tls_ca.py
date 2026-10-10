"""A throwaway certificate authority for the mutual-TLS tests.

Built with the system `openssl` CLI rather than a Python crypto library:
`cryptography` is not a dependency of this service — `ssl.getpeercert()`
parses a certificate's SAN without one, and so does everything else this
repo ships — and adding one just to generate test fixtures would be a
dependency change, which is out of scope for the PR that needs this file.
`openssl` is already a build requirement of everything on this machine that
terminates TLS.

**Never the real root.** `platform/decisions/0066`'s root lives in
`macbook`'s Secure Enclave behind Touch ID; no test can reach it, and every
certificate this module produces is signed by a CA generated fresh, in a
pytest tmp_path, and thrown away at the end of the test.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

if shutil.which("openssl") is None:
    pytest.skip("no openssl on PATH — this module shells out to it for "
                "every certificate it builds", allow_module_level=True)


def _run(*args: str) -> None:
    result = subprocess.run(list(args), capture_output=True, text=True)
    assert result.returncode == 0, (
        f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


def make_ca(dir_: Path, cn: str = "convivy-test CA") -> tuple[Path, Path]:
    """A self-signed P-256 CA: (cert path, key path)."""
    key = dir_ / "ca.key"
    cert = dir_ / "ca.crt"
    _run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
         "-out", str(key))
    _run("openssl", "req", "-x509", "-new", "-key", str(key), "-days", "3650",
         "-out", str(cert), "-subj", f"/CN={cn}")
    return cert, key


def make_cert(dir_: Path, name: str, ca_cert: Path, ca_key: Path, *,
              uri_names: tuple[str, ...] = (),
              dns_names: tuple[str, ...] = (),
              ip_names: tuple[str, ...] = (),
              email_names: tuple[str, ...] = (),
              ext_key_usage: str = "clientAuth",
              key_usage: str | None = None,
              subject_cn: str | None = None,
              days: int = 90) -> tuple[Path, Path]:
    """A P-256 leaf certificate signed by `ca_cert`/`ca_key`: (cert, key).

    `uri_names` takes zero, one, or more entries — what lets the mutual-TLS
    tests build the three shapes `platform/decisions/0066` names a refusal
    for: no name, one name, and more than one.

    `ext_key_usage` and `key_usage` are passed to openssl verbatim, so a
    caller can set `critical,` or a numeric OID. `subject_cn` defaults to
    `name`; a signing certificate's subject is the person's email.
    """
    key = dir_ / f"{name}.key"
    csr = dir_ / f"{name}.csr"
    cert = dir_ / f"{name}.crt"
    _run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
         "-out", str(key))
    _run("openssl", "req", "-new", "-key", str(key),
         "-subj", f"/CN={subject_cn or name}", "-out", str(csr))

    ext_lines = [f"extendedKeyUsage={ext_key_usage}"]
    if key_usage is not None:
        ext_lines.append(f"keyUsage={key_usage}")
    sans = ([f"URI:{u}" for u in uri_names] + [f"DNS:{d}" for d in dns_names]
            + [f"IP:{ip}" for ip in ip_names]
            + [f"email:{e}" for e in email_names])
    if sans:
        ext_lines.insert(0, f"subjectAltName={','.join(sans)}")
    ext_file = dir_ / f"{name}.ext"
    ext_file.write_text("\n".join(ext_lines) + "\n")

    _run("openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
         "-CAkey", str(ca_key), "-CAcreateserial", "-days", str(days),
         "-out", str(cert), "-extfile", str(ext_file))
    return cert, key
