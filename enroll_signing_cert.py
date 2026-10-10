#!/usr/bin/env python3.14
"""enroll_signing_cert — the local admin command that writes a
`signing_certificates` row (platform/contracts/library-protocol §2.2), the
same shape `enroll_machine.py` is for `machine_certificates`.

**Takes the fingerprint and expiry the signing tool already printed, not a
certificate file** — see `signing_registry.py`'s module docstring for why,
and `machine_registry.py`'s for the same reasoning on the sibling registry.

    enroll_signing_cert.py issue operator@convivy.example macbook \\
        4f3c...<64 hex chars> 2026-12-01T00:00:00 \\
        --note "root ceremony, 2026-09-27"
    enroll_signing_cert.py revoke 4f3c...<64 hex chars> --note "lost the key"
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys

import signing_registry
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
        row = signing_registry.issue(
            args.fingerprint_sha256, args.person, args.machine,
            args.not_after, note=args.note)
    except signing_registry.InvalidCertificateRow as e:
        print(f"enroll_signing_cert: {e}", file=sys.stderr)
        return 2
    except store.WritesDisabled as e:
        print(f"enroll_signing_cert: {e}", file=sys.stderr)
        return 1
    print(f"issued: person {row['person']!r}, machine {row['machine']!r}, "
          f"fingerprint {row['fingerprint_sha256'][:12]}…, "
          f"not_after {row['not_after']}")
    return 0


def _revoke(args) -> int:
    try:
        row = signing_registry.revoke(args.fingerprint_sha256,
                                       note=args.note)
    except signing_registry.InvalidCertificateRow as e:
        print(f"enroll_signing_cert: {e}", file=sys.stderr)
        return 2
    except store.WritesDisabled as e:
        print(f"enroll_signing_cert: {e}", file=sys.stderr)
        return 1
    if row is None:
        print(f"enroll_signing_cert: {args.fingerprint_sha256!r} has no "
              f"active certificate to revoke", file=sys.stderr)
        return 2
    print(f"revoked: person {row['person']!r}, fingerprint "
          f"{row['fingerprint_sha256'][:12]}…")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    issue_p = sub.add_parser(
        "issue", help="register a newly-signed signing certificate")
    issue_p.add_argument("person", help="the certificate's subject, an "
                                         "email address")
    issue_p.add_argument("machine", help="the Mac holding the key, as the "
                                          "certificate's convivy://signer/ "
                                          "URI names it")
    issue_p.add_argument("fingerprint_sha256",
                          help="SHA-256 over the certificate's DER encoding, "
                               "as printed by the signing tool")
    issue_p.add_argument("not_after", type=_parse_not_after)
    issue_p.add_argument("--note", default=None)
    issue_p.set_defaults(func=_issue)

    revoke_p = sub.add_parser(
        "revoke", help="revoke the signing certificate named by this "
                        "fingerprint")
    revoke_p.add_argument("fingerprint_sha256")
    revoke_p.add_argument("--note", default=None)
    revoke_p.set_defaults(func=_revoke)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
