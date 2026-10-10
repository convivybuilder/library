#!/usr/bin/env python3.14
"""enroll_machine — the local admin command that writes a
`machine_certificates` row (platform/decisions/0066, Handoff: "an
administrative command run locally on the mini, needing no network and no
certificate").

**Run on the machine that serves the Library, against its own database
directly** — the same shape `bind_credential.py` uses, and for the same
reason: a certificate registered wrong, or not registered at all, is refused
by `machine_identity.MachineIdentityGate` on the mutual-TLS listener with no
way to fix it *through* that listener, since fixing it is the thing the
listener is refusing. This command is the door that stays open when that one
is closed.

**Takes the fingerprint and expiry the signing tool already printed, not a
certificate file** — see `machine_registry.py`'s module docstring for why.

    enroll_machine.py issue macbook \\
        4f3c...<64 hex chars> 2026-12-01T00:00:00 \\
        --custody hardware --note "renewal, 2026-09-04"
    enroll_machine.py revoke macbook --note "decommissioned"
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys

import machine_registry
import store


def _parse_not_after(raw: str) -> _dt.datetime:
    try:
        return _dt.datetime.fromisoformat(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"not_after must be an ISO-8601 date or datetime, not {raw!r}"
        ) from e


def _issue(args) -> int:
    try:
        row = machine_registry.issue(
            args.machine, args.fingerprint_sha256, args.not_after,
            args.custody, note=args.note)
    except machine_registry.InvalidCertificateRow as e:
        print(f"enroll_machine: {e}", file=sys.stderr)
        return 2
    except store.WritesDisabled as e:
        print(f"enroll_machine: {e}", file=sys.stderr)
        return 1
    print(f"issued: machine {row['machine']!r}, fingerprint "
          f"{row['fingerprint_sha256'][:12]}…, custody {row['custody']}, "
          f"not_after {row['not_after']}")
    return 0


def _revoke(args) -> int:
    try:
        row = machine_registry.revoke(args.machine, note=args.note)
    except store.WritesDisabled as e:
        print(f"enroll_machine: {e}", file=sys.stderr)
        return 1
    if row is None:
        print(f"enroll_machine: {args.machine!r} has no active certificate "
              f"to revoke", file=sys.stderr)
        return 2
    print(f"revoked: machine {row['machine']!r}, fingerprint "
          f"{row['fingerprint_sha256'][:12]}…")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    issue_p = sub.add_parser(
        "issue", help="register a newly-signed certificate, revoking "
                       "whatever was active for this machine")
    issue_p.add_argument("machine")
    issue_p.add_argument("fingerprint_sha256",
                          help="SHA-256 over the certificate's DER encoding, "
                               "as printed by the signing tool")
    issue_p.add_argument("not_after", type=_parse_not_after)
    issue_p.add_argument("--custody", required=True,
                          choices=sorted(machine_registry.CUSTODY_VALUES))
    issue_p.add_argument("--note", default=None)
    issue_p.set_defaults(func=_issue)

    revoke_p = sub.add_parser(
        "revoke", help="revoke the currently active certificate for a "
                        "machine")
    revoke_p.add_argument("machine")
    revoke_p.add_argument("--note", default=None)
    revoke_p.set_defaults(func=_revoke)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
