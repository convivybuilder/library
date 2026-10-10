#!/usr/bin/env python3.11
"""bind_credential — the local admin command that writes a
`credential_bindings` row (platform/decisions/0066 stage C, Handoff: "an
administrative command run locally on the mini, needing no network and no
certificate").

**Run on the machine that serves the Library, against its own database
directly** — the same way every other write in this service reaches
`store.connect_rw()`, just without an HTTP request in front of it. This is
also the lockout recovery path: a credential bound to the wrong machine, or
not bound at all, is refused by `credential_binding.CredentialBindingGate`
on the mutual-TLS listener with no way to fix it *through* that listener,
since fixing it is the thing the listener is refusing. This command is the
door that stays open when that one is closed.

**The raw token is read from stdin, never from argv.** A command-line
argument lands in shell history and in every other process's view of `ps`;
stdin does neither. Pipe it in, or paste it and press Ctrl-D:

    echo -n "$TOKEN" | ./bind_credential.py macbook --note "Deck, 2026-09-04"

Only the token's SHA-256 is ever computed from what stdin carries — see
`credential_binding.bind`, the one function that writes this table.
"""
from __future__ import annotations

import argparse
import sys

import credential_binding
import store


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("machine", help="the machine name this credential may "
                                    "be presented from, e.g. 'macbook'")
    p.add_argument("--note", default=None,
                    help="free-text provenance, e.g. who asked and when")
    args = p.parse_args(argv)

    token = sys.stdin.read().strip()
    if not token:
        print("bind_credential: no token on stdin — pipe or paste the raw "
              "bearer token, then EOF", file=sys.stderr)
        return 2

    digest = credential_binding.sha256_token(token)
    try:
        row = credential_binding.bind(digest, args.machine, note=args.note)
    except store.WritesDisabled as e:
        print(f"bind_credential: {e}", file=sys.stderr)
        return 1

    print(f"bound credential {digest[:12]}… to machine "
          f"{row['machine']!r} (created_at {row['created_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
