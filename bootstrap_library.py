#!/usr/bin/env python3.11
"""bootstrap_library — the local command that makes the Library's first
administrator and mints its first approved-client credential, once.

**Run on the machine that serves the Library, against its own database
directly**, the same family as `bind_credential.py` and `enroll_machine.py` and
for the same reason: at an adopter's first install there is no administrator to
authenticate and no credential to present, so nothing that goes through the
Library's own listener can be the way in. The mutual-TLS route is unavailable
too, since platform/decisions/0066 keeps the one root key in `macbook`'s Secure
Enclave, and GitHub is ruled out by company/decisions/0009. Local access to the
database is the authority, needing no network, no credential and no certificate.

    ./bootstrap_library.py "Ada Lovelace"

In one transaction it records that the bootstrap happened, creates the named
person, makes them an administrator, and adds a credential to the file
`identity.CLIENT_TOKENS` names (`~/.library/client-tokens.json`, or
`LIBRARY_CLIENT_TOKENS`). Run `python -m alembic upgrade head` first; the
tables it writes come from that migration.

**The credential is generated here and printed on stdout, and nothing else is.**
`bind_credential.py` reads its token from stdin because the operator already has
one and an argument would put it in shell history and in every process's view of
`ps`. This command has no secret to be handed: it takes a display name, which is
not one, and mints the token itself. Status goes to stderr, so
`TOKEN=$(./bootstrap_library.py "Ada Lovelace")` captures the credential and
only that. The file it lands in is keyed by the raw token (`identity.resolve`
looks the bearer token up as the dict key), which is why the file is written
mode 0600 and only the token's SHA-256 is recorded in the database.

**The credential's scope is every product, `["*"]`.** A fresh Library has no
products to narrow it to, and the credential exists to administer the Library,
so a narrower scope would only lock its holder out of what they are there to
set up. It is an entry in the approved-client file and so resolves to
`kind = "approved_client"` whatever it says (`identity.NAMEABLE_KINDS`
deliberately omits that kind); the `name` recorded for it is the person's id,
which is what the audit columns will show against its writes.

**THE GUARD IS A RECORDED EVENT, NEVER A COUNT.** A bootstrap gated on "no
administrator exists" re-fires whenever that table is empty, which is true of a
person record someone has emptied and of a database that was restored from a
copy holding nobody. This command instead writes one row to `bootstrap_record`
and a second run is refused against it. That row is independent of `people`
and `administrators`: this command never reads either to decide, and nothing
that deletes from them touches it, so emptying the person records or revoking
the only administrator does not re-arm it. The table's primary key admits one
row and a `CHECK (id = 1)` refuses any other, so the refusal is SQLite's own
rather than a check-then-insert this code could get wrong or lose a race in.

**THE BOUNDARY.** The record lives in the database it guards, so anyone with
local write access to that database can `DELETE FROM bootstrap_record` and run
this again, and so can anyone who restores a copy of the database from before
the bootstrap. That is accepted: local write access already owns everything,
including writing the credential file by hand, which is how an approved-client
credential was made before this command existed. It is a different property
from the one the guard provides, which is that this command does not re-fire
because a table it never consults was emptied. Defending against the first is
outside what a row in the same file could ever do, and nothing here implies
otherwise.

**A failed bootstrap does not use up the one-time record.** The credential file
is written before the transaction commits, and a failure rolls the record back
and puts the file back, so a full disk or an unwritable directory leaves the
command runnable rather than refusing forever with no credential to show for
it. A credential file that exists but is not a JSON object is refused for the
same reason: overwriting it would destroy whatever credentials it held.

**The handler decides from the record rather than from a flag**, so an
interruption arriving after the commit became durable keeps the credential
instead of erasing it. The comment in `bootstrap`'s own handler says why a flag
cannot carry that, and `_record_is_durable` says which way it errs when it
cannot tell. **The token is still lost in that case** — it prints only on the
way out, which the interruption prevented. Recover it by reading the credential
file: the surviving entry's `name` is the `person_id` that run created, and
`bootstrap_record.person_id` names the same one.

**THAT COVERS AN EXCEPTION AND NOT A HARD KILL**, so do not read it as a promise
that the file is always put back. SIGKILL, an OOM kill or a crash between the
file write and the commit runs no handler: SQLite discards the uncommitted
transaction on the next open, leaving the guard unconsumed and the command
runnable, and the new entry stays in the credential file. It is a live `["*"]`
credential keyed by a token nobody was shown, since the token prints only after
the commit, and its `name` matches no row in `people`. Whoever can read that
0600 file can already write any credential into it, so it grants no authority
they lacked; it is untidy rather than a way in, and a retry adds a second entry
beside it rather than replacing it.

**The opposite ordering is worse, which is why this one was chosen.** Committing
first and writing the file second, the same kill leaves the guard consumed, a
person and an administrator row on disk, and no credential anywhere — every
retry refuses, and the only way out is deleting the record by hand. An
administrator who cannot authenticate is the harmful artifact; a stray token in
a private file is not.

**Two neighbouring residues a hard kill can leave, so the map is complete.**
Neither changes the conclusion above and neither has a guard, because nobody has
seen either occur; they are written down so a reader weighing the ordering is
not surprised by one.

- **A kill INSIDE `_atomic_write`, between `mkstemp` and `os.replace`**, leaves
  the real credential file untouched and an orphan `.client-tokens.<random>`
  beside it at 0600, holding as much of the new contents as had been written —
  empty if the kill beat the write, a prefix of the JSON if it landed mid-write,
  and otherwise every token the file already had plus the new one, in plaintext.
  The cleanup that unlinks it runs only for an exception. It grants nobody
  anything the file's owner did not already have, but it is a second copy that
  outlives an operator later removing an entry from the real file, which is the
  part worth knowing (security-reviewer, PR #134 rounds 2 and 3).
- **An interruption after the commit and before the token is printed** consumes
  the guard with the credential intact in the file. This one is not only a hard
  kill: an ordinary Ctrl-C lands here too, since the handler now keeps the file
  over a durable record and `KeyboardInterrupt` then passes `main()`, which
  catches none of it. Milder than it sounds and not the rejected ordering — the
  operator recovers by reading the file, where the entry's `name` is the person
  id the same run created.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import json
import os
import secrets
import sqlite3
import sys
import tempfile
from pathlib import Path

import credential_binding
import identity
import store

ACTOR = "bootstrap_library"

# `people.display_name` is `String(300)`. SQLite does not enforce the length,
# so this is the only place that does.
DISPLAY_NAME_MAX = 300


class AlreadyBootstrapped(RuntimeError):
    """The one-time record exists; nothing was written."""


class Refused(RuntimeError):
    """The bootstrap cannot proceed and nothing was written."""


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace `path` with `data`, mode 0600, never leaving a half-written file
    where `identity._client_tokens()` would read it. `mkstemp` creates its file
    0600, and the rename is atomic within the directory."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".client-tokens.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _add_client_credential(path: Path, token: str, entry: dict) -> bytes | None:
    """Add `token` to the approved-client file, keeping every entry already in
    it. Returns the file's previous bytes, `None` if it did not exist, so the
    caller can put it back."""
    try:
        prior = path.read_bytes()
    except FileNotFoundError:
        prior = None
    entries: object = {}
    if prior is not None:
        try:
            entries = json.loads(prior)
        except ValueError:
            entries = None
        if not isinstance(entries, dict):
            raise Refused(
                f"{path} exists and is not a JSON object of credentials; "
                f"refusing to overwrite what it holds. Move it aside or fix it, "
                f"then run this again — nothing was recorded.")
    entries[token] = entry
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, json.dumps(entries, indent=2).encode("utf-8"))
    return prior


def _record_is_durable(conn: sqlite3.Connection) -> bool:
    """Whether the one-time record survived the rollback, meaning the commit
    landed and the credential in the file is the one it names.

    **UNCERTAINTY ANSWERS `True`, AND THAT DIRECTION IS THE POINT.** The caller
    uses this to decide whether to undo the credential file, and the two ways of
    being wrong are not equal. Leaving a credential whose record did not commit
    is an entry in a 0600 file that nobody holds and nothing authorises against
    a missing person — the module docstring's hard-kill residue, which the
    security review judged inert. Erasing a credential whose record DID commit
    locks the Library out permanently, since the guard refuses every retry. So a
    connection too broken to answer keeps the file.
    """
    try:
        return conn.execute(
            "SELECT 1 FROM bootstrap_record WHERE id = 1").fetchone() is not None
    except sqlite3.Error:
        return True


def _claim_bootstrap(conn: sqlite3.Connection, person_id: str, digest: str,
                     now_iso: str, performed_by: str) -> None:
    """Write the one-time record, or raise `AlreadyBootstrapped` if it exists.

    The INSERT is the guard. `bootstrap_record` admits one row (`id` is its
    primary key and `CHECK (id = 1)`), so SQLite refuses the second. Nothing here
    reads `people` or `administrators`, which is what keeps the guard
    independent of them.
    """
    try:
        conn.execute(
            "INSERT INTO bootstrap_record (id, person_id, credential_sha256, "
            "performed_at, performed_by) VALUES (1, ?, ?, ?, ?)",
            (person_id, digest, now_iso, performed_by))
    except sqlite3.IntegrityError:
        # `BEGIN IMMEDIATE` holds the write lock, so this row is the one that
        # beat us and it is still there to read.
        prior = conn.execute(
            "SELECT person_id, performed_at, performed_by FROM bootstrap_record "
            "WHERE id = 1").fetchone()
        raise AlreadyBootstrapped(
            f"the bootstrap already ran: {prior['person_id']} was made the "
            f"first administrator at {prior['performed_at']} by "
            f"{prior['performed_by']}. It runs once, so nothing was written; "
            f"a further administrator or credential is not this command's job."
        ) from None


def bootstrap(display_name: str, *, now=None) -> tuple[str, str]:
    """Do the bootstrap; return `(person_id, token)`.

    Goes through `store.connect_rw()`, so it refuses exactly like every other
    write while the Library is in its read-only shadow (`store.WritesDisabled`).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat(" ")
    person_id = f"person_{store.new_ulid()}"
    token = secrets.token_urlsafe(32)
    digest = credential_binding.sha256_token(token)
    performed_by = getpass.getuser()

    conn = store.connect_rw()
    written = False
    prior: bytes | None = None
    try:
        # Takes the write lock now, so two bootstraps cannot both pass the guard.
        conn.execute("BEGIN IMMEDIATE")
        _claim_bootstrap(conn, person_id, digest, now_iso, performed_by)
        conn.execute(
            "INSERT INTO people (id, display_name, created_at, created_by, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, display_name, now_iso, ACTOR, now_iso, ACTOR))
        conn.execute(
            "INSERT INTO administrators (person_id, granted_at, granted_by) "
            "VALUES (?, ?, ?)", (person_id, now_iso, ACTOR))
        prior = _add_client_credential(
            identity.CLIENT_TOKENS, token,
            {"name": person_id, "products": ["*"]})
        written = True
        conn.commit()
    except BaseException:
        conn.rollback()
        # **THE HANDLER ASKS THE DATABASE, NOT A FLAG.** Undoing the credential
        # file is right only while the record is still undone too; over a
        # durable record it produces the one state this design exists to avoid —
        # guard consumed, credential erased, token never printed, and no way to
        # run the command again.
        #
        # **A FLAG CANNOT CARRY THIS AND AN EARLIER VERSION OF THIS CODE TRIED.**
        # It set `written = False` right after `conn.commit()` returned. That
        # line never runs in the case it was written for: `commit()` blocks in
        # fsync (`synchronous = FULL`), CPython delivers a pending SIGINT at the
        # call's own interrupt check the instant it returns, and the handler is
        # entered before the next store. Probed on 3.11.15 and 3.14.5 — the
        # handler saw the flag still set — and the disassembly shows nothing but
        # `POP_TOP`, `LOAD_CONST`, `STORE_FAST` between the call and the end of
        # the body, none of them a delivery point. The line was dead and its
        # comment claimed a window it did not close (reviewer, PR #134 round 2).
        #
        # The row is the only thing that knows, so read it. This also covers a
        # `commit()` that raises after the write became durable, which no flag
        # set by the calling code could ever see.
        if written and not _record_is_durable(conn):
            if prior is None:
                identity.CLIENT_TOKENS.unlink(missing_ok=True)
            else:
                _atomic_write(identity.CLIENT_TOKENS, prior)
        raise
    finally:
        conn.close()
    return person_id, token


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("display_name",
                   help="the administrator's display name, e.g. 'Ada Lovelace'")
    args = p.parse_args(argv)

    name = args.display_name.strip()
    if not name or len(name) > DISPLAY_NAME_MAX:
        print(f"bootstrap_library: the display name must be 1 to "
              f"{DISPLAY_NAME_MAX} characters", file=sys.stderr)
        return 2

    try:
        person_id, token = bootstrap(name)
    except (AlreadyBootstrapped, Refused, store.WritesDisabled,
            store.MisconfiguredDatabase, OSError) as e:
        print(f"bootstrap_library: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        hint = (" Run `python -m alembic upgrade head` against this database "
                "first." if "no such table" in str(e) else "")
        print(f"bootstrap_library: {e}.{hint}", file=sys.stderr)
        return 1

    print(f"bootstrapped {name!r} as {person_id}, the Library's first "
          f"administrator", file=sys.stderr)
    print(f"approved-client credential added to {identity.CLIENT_TOKENS}; "
          f"present it as `Authorization: Bearer <token>`", file=sys.stderr)
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
