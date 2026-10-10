"""`generate_csr.py`: the file-based fallback for issuance step 1
(platform/decisions/0066), and the constant it deliberately does not import
from `machine_identity` (see that module's docstring for why).

**Not evidence about the Secure Enclave.** Everything here proves is that
the file-based CSR this tool can actually build is structurally correct —
the right SAN, the right EKU, a public key whose printed fingerprint matches
what a reader would independently compute. None of it touches a Secure
Enclave or Touch ID, which this machine does not have
(platform/decisions/0066).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import generate_csr
import machine_identity

if shutil.which("openssl") is None:
    pytest.skip("no openssl on PATH — this module shells out to it for "
                "every key and CSR it builds", allow_module_level=True)


def _csr_text(csr_path) -> str:
    result = subprocess.run(
        ["openssl", "req", "-in", str(csr_path), "-noout", "-text"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_uri_prefix_matches_machine_identity():
    """Kept as a literal copy rather than an import (see the module
    docstring) — this is what stops the copy drifting silently."""
    assert generate_csr.MACHINE_URI_PREFIX == machine_identity.MACHINE_URI_PREFIX


def test_generate_writes_a_key_and_a_csr(tmp_path):
    key_path, csr_path = generate_csr.generate("macbook", tmp_path)
    assert key_path.is_file()
    assert csr_path.is_file()


def test_the_key_is_owner_only(tmp_path):
    """The key is the Library's TLS server (or a machine's client) private
    key — it must be readable by nobody but its owner, whichever openssl
    build made it (comment 2752 on issue_01M2K1NDCV4J92GE665KAST12N)."""
    key_path, _ = generate_csr.generate("macbook", tmp_path)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_the_key_ends_0600_even_if_openssl_left_it_permissive(
        tmp_path, monkeypatch):
    """Regression for the LibreSSL build, which leaves the key at 0o644
    under this repo's umask. Simulates that by chmod-ing the key file
    permissive right after the real ecparam call writes it — this must
    fail against code that trusts whatever mode openssl left."""
    real_run = generate_csr._run

    def _run_then_leave_key_permissive(*args, **kwargs):
        real_run(*args, **kwargs)
        if "ecparam" in args:
            key_path = Path(args[args.index("-out") + 1])
            key_path.chmod(0o644)

    monkeypatch.setattr(generate_csr, "_run", _run_then_leave_key_permissive)
    key_path, _ = generate_csr.generate("macbook", tmp_path)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_a_preexisting_permissive_key_file_is_replaced_and_ends_0600(
        tmp_path):
    key_path = tmp_path / "macbook.key"
    key_path.write_text("stale key material from a previous run")
    key_path.chmod(0o644)

    generate_csr.generate("macbook", tmp_path)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_generate_raises_and_main_returns_1_if_the_mode_cannot_be_forced(
        tmp_path, monkeypatch):
    """If chmod can't make the key 0o600 — modeled here by making chmod a
    no-op after openssl leaves the key permissive — `generate()` must not
    silently hand back a key whose mode it could not fix."""
    real_run = generate_csr._run
    real_chmod = os.chmod

    def _run_then_leave_key_permissive(*args, **kwargs):
        real_run(*args, **kwargs)
        if "ecparam" in args:
            key_path = args[args.index("-out") + 1]
            real_chmod(key_path, 0o644)

    monkeypatch.setattr(generate_csr, "_run", _run_then_leave_key_permissive)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="0o600"):
        generate_csr.generate("macbook", tmp_path)

    rc = generate_csr.main(["macbook", "--out-dir", str(tmp_path)])
    assert rc == 1


def test_the_key_is_p256(tmp_path):
    key_path, _ = generate_csr.generate("macbook", tmp_path)
    result = subprocess.run(
        ["openssl", "pkey", "-in", str(key_path), "-noout", "-text"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "prime256v1" in result.stdout or "P-256" in result.stdout


def test_the_csr_carries_exactly_one_uri_san_naming_the_machine(tmp_path):
    _, csr_path = generate_csr.generate("macbook", tmp_path)
    text = _csr_text(csr_path)
    assert "URI:convivy://machine/macbook" in text
    assert text.count("URI:") == 1


def test_the_common_name_carries_the_same_string(tmp_path):
    _, csr_path = generate_csr.generate("macbook", tmp_path)
    assert "CN = macbook" in _csr_text(csr_path) or "CN=macbook" in _csr_text(
        csr_path)


def test_a_client_csr_requests_client_auth_only(tmp_path):
    _, csr_path = generate_csr.generate("macbook", tmp_path)
    text = _csr_text(csr_path)
    assert "TLS Web Client Authentication" in text
    assert "TLS Web Server Authentication" not in text
    assert "DNS:" not in text
    assert "IP Address:" not in text


def test_a_server_csr_requests_server_auth_and_loopback_names(tmp_path):
    _, csr_path = generate_csr.generate("library-mini", tmp_path, server=True)
    text = _csr_text(csr_path)
    assert "TLS Web Server Authentication" in text
    assert "TLS Web Client Authentication" not in text
    assert "DNS:localhost" in text
    assert "IP Address:127.0.0.1" in text
    assert "URI:convivy://machine/library-mini" in text


def test_the_printed_fingerprint_matches_an_independent_computation(tmp_path):
    key_path, _ = generate_csr.generate("macbook", tmp_path)
    printed = generate_csr._pubkey_sha256(key_path)

    result = subprocess.run(
        ["openssl", "pkey", "-in", str(key_path), "-pubout", "-outform",
         "DER"], capture_output=True)
    assert result.returncode == 0
    independent = hashlib.sha256(result.stdout).hexdigest()

    assert printed == independent
    assert len(printed) == 64


def test_main_prints_the_fingerprint_and_the_custody_warning(tmp_path, capsys):
    rc = generate_csr.main(["macbook", "--out-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "custody=file" in out
    assert "Secure Enclave" in out


def test_main_reports_a_failure_from_openssl_rather_than_raising(
        tmp_path, monkeypatch):
    """A caller-facing tool exits with a message, not a traceback, when the
    system openssl itself fails."""
    def _boom(*args, **kwargs):
        raise RuntimeError("openssl exploded")
    monkeypatch.setattr(generate_csr, "generate", _boom)
    rc = generate_csr.main(["macbook", "--out-dir", str(tmp_path)])
    assert rc == 1
