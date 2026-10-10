#!/usr/bin/env python3.11
"""mint_credential — the local command that adds one credential to the
Library's own token files and prints it once, in the shape of
`bootstrap_library.py`.

**Run on the machine that serves the Library**, the same family as
`bootstrap_library.py` and `bind_credential.py`: it writes the file directly
rather than going through a route, because there is no route for this yet and
because a fresh install has no credential to authenticate one with.

    ./mint_credential.py --file agents --name orient --out ~/.orient-control/library-token
    ./mint_credential.py --file clients --name orient --out ~/.orient-control/library-client-token

`--out PATH` writes the token straight to PATH, atomically, at mode 0600 from creation, and stdout
stays empty. Redirecting stdout with `>` instead — still supported, for scripting — creates the
file at the shell's umask (usually 0644, world-readable) until something chmods it afterward.

**The token reaches `--out` before anything is committed to the credential store.** If the store
write then fails, the `--out` file is removed again, so a failure never leaves a live credential in
the store that nobody saw. `--out` refuses a PATH that resolves — symlinks included — to either
credential store file itself, and refuses a dangling symlink rather than writing through it.

`--file agents` writes an entry to `identity.TOKENS`
(`~/.library/api-tokens.json`, or `LIBRARY_API_TOKENS`), which resolves as
`agent` unless `--kind system` is given — `approved_client` is never
nameable there (`identity.NAMEABLE_KINDS`) — and there alone `--grants` adds
capabilities beyond the role's own. `--file clients` writes to
`identity.CLIENT_TOKENS`, whose every entry resolves as `approved_client`
regardless of what it says, and there alone `--holds-reserved` and
`--withholds` narrow the human-reserved capabilities a client credential
carries — all three validated with `identity`'s own load-time validators, so
this command cannot write an entry `identity` would then refuse to serve.

**The token is generated here with `secrets.token_urlsafe(32)`, printed on
stdout, and nothing else is.** Status goes to stderr, so
`TOKEN=$(./mint_credential.py --file agents --name orient)` captures the
credential and only that. `--products` is a comma list, `"*"` (every
product) by default. A `--name` already present in the target file is
refused unless `--replace` is given, and no other entry's token is ever
printed or logged.

**`identity` re-reads both files per request, so no restart is needed** for
a credential minted this way to take effect.

**`--revoke --name NAME` removes that entry instead of minting one** — the
same file, resolved through the same `FILES` map:

    ./mint_credential.py --file agents --revoke --name orient
    ./mint_credential.py --file clients --revoke --name orient

It deletes the entry whose `name` field equals NAME, atomically, and leaves
every other entry byte-for-byte as it was. Refused, writing nothing and
exiting non-zero, when no entry carries that name, or when more than one
does — the refusal says how many, never which. `identity` re-reads the file
per request (see above), so a credential already in use is refused with 401
on its very next request, no restart needed. Stdout stays empty; the name
and the file revoked from go to stderr, never a token. `--revoke` takes only
`--file` and `--name`; it is refused at argument parsing alongside `--out`,
`--replace`, `--products`, `--kind`, `--grants`, `--holds-reserved` or
`--withholds`, none of which apply to removing an entry.

**Importing `identity` is deferred past the one line that settles the
environment it needs, and that line is not optional.** `identity` imports
`store`, whose module top level runs `DB_PATH, DB_SOURCE = resolve_db()`
unconditionally — the same chain that made `init_vectors.py` crash with a
raw `store.MisconfiguredDatabase` traceback on a fresh install instead of
its own message (reviewer, library#155), because that command, like this
one, has no database to open and no business requiring `LIBRARY_DB` or
`ORIENT_CONTROL_DB` to be set at all. An adopter's shell running this
command for the first time is exactly the shell with neither exported. The
placeholder below is provably not a real database — `tests/conftest.py`
sets the same one, for the same reason — so `resolve_db()` succeeds without
ever opening it, and only `setdefault`, so a `LIBRARY_DB` the caller did set
is left alone.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LIBRARY_DB", "/nonexistent/mint-credential-placeholder.db")

import identity  # noqa: E402  (see the module docstring's note above)

FILES = {"agents": lambda: identity.TOKENS, "clients": lambda: identity.CLIENT_TOKENS}


class Refused(RuntimeError):
    """The credential cannot be written and nothing was."""


def _check_mode(path: Path) -> None:
    """Refuse a target file that already exists wider than 0600 — writing a
    credential into it would inherit permissions weaker than the file is
    supposed to have."""
    if not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & ~0o600:
        raise Refused(
            f"{path} exists with mode {oct(mode)}, wider than 0600. Fix its "
            f"permissions (chmod 600 {path}) and run this again — nothing "
            f"was written.")


def _load_entries(path: Path) -> dict:
    """The existing token → entry map, or `{}` if the file does not exist yet.
    Never discards entries it cannot parse: a file that is not valid JSON, or
    not a JSON object, is refused rather than silently overwritten."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        raise Refused(
            f"{path} exists and is not valid JSON ({exc}); refusing to "
            f"overwrite what it holds. Move it aside or fix it, then run "
            f"this again — nothing was written.") from None
    if not isinstance(loaded, dict):
        raise Refused(
            f"{path} exists and is not a JSON object of credentials; "
            f"refusing to overwrite what it holds. Move it aside or fix it, "
            f"then run this again — nothing was written.")
    return loaded


def _tokens_named(entries: dict, name: str) -> list[str]:
    """Every token in `entries` whose `name` field equals `name`, in no
    particular order. Shared by the duplicate-name check `_build_entry` makes
    before minting and the lookup `_revoke_entry` makes before removing."""
    return [tok for tok, meta in entries.items()
            if isinstance(meta, dict) and meta.get("name") == name]


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace `path` with `data`, mode 0600, never leaving a half-written
    file where `identity._tokens()`/`identity._client_tokens()` would read
    it. `mkstemp` creates its file 0600, and the rename is atomic within the
    directory — the same shape `bootstrap_library.py`'s own `_atomic_write`
    uses. `os.replace` never follows a symlink at `path`: rename(2) replaces
    the directory entry itself, not whatever it points to."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _ensure_private_dir(dir_path: Path) -> None:
    """Create `dir_path` and every missing ancestor at 0700, one level at a
    time. `Path.mkdir(parents=True, mode=0o700)` applies `mode` only to the
    leaf directory it creates — an ancestor `parents=True` also creates
    along the way is left at the umask's default, usually 0755. `mkdir`'s
    own `mode` argument is masked by the umask too, so each level gets an
    explicit `chmod` after creation to land on 0700 regardless."""
    missing = []
    cur = dir_path
    while not cur.exists():
        missing.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)


def _check_out_not_store(path: Path) -> None:
    """Refuse a `--out` PATH that resolves — following symlinks, via
    `os.path.realpath` — to either credential store, `identity.TOKENS` or
    `identity.CLIENT_TOKENS`, regardless of which one `--file` targets:
    `--out` writes a bare token, not JSON, and landing it on either store
    file destroys every credential that file holds with no warning beyond
    this one."""
    out_real = os.path.realpath(str(path))
    for store_path in (identity.TOKENS, identity.CLIENT_TOKENS):
        if out_real == os.path.realpath(str(store_path)):
            raise Refused(
                f"--out {path} resolves to {store_path}, a credential "
                f"store; refusing to overwrite it with a bare token. "
                f"Nothing was written.")


def _check_out_dangling(path: Path) -> None:
    """Refuse a `--out` PATH that is a symlink with no target.
    `path.exists()` follows symlinks and reports a dangling one as absent —
    exactly the signal an ordinary new PATH gives — so a dangling symlink
    needs its own check rather than being written through as though it were
    new."""
    if path.is_symlink() and not path.exists():
        raise Refused(
            f"{path} is a symlink with no target; refusing to write "
            f"through it. Nothing was written.")


def _check_out_replace_target(path: Path) -> None:
    """Under `--replace`, refuse a PATH that is not already a private
    regular file — the rule `_check_mode` already applies to the credential
    store. `_write_out` renames PATH's previous contents aside before
    writing the new token; if PATH were a symlink, that rename would only
    ever touch the symlink itself (`os.replace`/`rename(2)` never follows
    one), but a *permissions* fix applied afterward to the backup would —
    `os.chmod` does follow symlinks — and land on whatever the symlink
    pointed at. Requiring PATH to already be a private regular file before
    any of this runs removes the need for that chmod entirely: the existing
    file is already 0600, and `os.replace` keeps it private at every
    instant from here on."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise Refused(
            f"--out {path} is a symlink; refusing to replace it — replacing "
            f"through a symlink risks changing the target's permissions "
            f"instead of the link's. Point --out at the target directly, or "
            f"remove the symlink, then run this again. Nothing was written.")
    if not stat.S_ISREG(st.st_mode):
        raise Refused(
            f"--out {path} exists and is not a regular file; refusing to "
            f"replace it. Nothing was written.")
    mode = stat.S_IMODE(st.st_mode)
    if mode & ~0o600:
        raise Refused(
            f"--out {path} exists with mode {oct(mode)}, wider than 0600. "
            f"Fix its permissions (chmod 600 {path}) and run this again — "
            f"nothing was written.")


def _write_out(path: Path, token: str, replace: bool) -> Path | None:
    """Write `token` alone to `path`, mode 0600 from creation. Every failure
    comes back as `Refused` — a directory at PATH, a permissions error, a
    race with another writer — so the caller never sees a bare traceback.

    Without `--replace`, PATH must not already exist. Opened with
    `O_CREAT | O_EXCL`, which POSIX guarantees fails closed on any existing
    directory entry at PATH — symlink included — regardless of what, if
    anything, it points to, so there is no window between a separate
    existence check and this write for a concurrent writer to land in.
    Returns `None`.

    With `--replace`, PATH may or may not exist. By the time this runs,
    `_check_out_replace_target` has already confirmed that if PATH exists it
    is a private (mode 0600) regular file, so no chmod is ever needed here —
    `os.replace` keeps whatever is at PATH private at every instant. If PATH
    exists, its previous contents are renamed aside first, to a predictable
    `<PATH>.mint-backup` name in the same directory, reserved with
    `O_CREAT | O_EXCL` so a stale backup left by an earlier crash is never
    silently overwritten. That backup path is returned so the caller can
    remove it once the store commit that follows also succeeds, or rename it
    back over PATH if that commit fails — a store-commit failure must never
    delete a PATH that held a working credential the store still serves.
    Written through `_atomic_write`, which is atomic within the directory
    and never follows a symlink at PATH; on failure the backup, if any, is
    restored before `Refused` is raised — and if that restore itself fails,
    `Refused` says so and names the backup's path rather than letting the
    exception escape as a bare traceback. Returns `None` when PATH did not
    exist beforehand."""
    try:
        _ensure_private_dir(path.parent)
    except OSError as exc:
        raise Refused(
            f"could not create {path.parent}: {exc}. Nothing was "
            f"written.") from None

    data = f"{token}\n".encode("utf-8")

    if replace:
        backup: Path | None = None
        if path.exists():
            backup = path.parent / f"{path.name}.mint-backup"
            try:
                fd = os.open(str(backup),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise Refused(
                    f"a stale backup already exists at {backup} from an "
                    f"earlier run and may hold a previous token; move it "
                    f"aside, then run this again. Nothing was written."
                ) from None
            os.close(fd)
            try:
                os.replace(path, backup)
            except OSError as exc:
                backup.unlink(missing_ok=True)
                raise Refused(
                    f"could not preserve the existing {path} before "
                    f"replacing it: {exc}. Nothing was written.") from None
        try:
            _atomic_write(path, data)
        except OSError as exc:
            if backup is not None:
                try:
                    os.replace(backup, path)
                except OSError as restore_exc:
                    raise Refused(
                        f"could not write the token to {path}: {exc}. "
                        f"Restoring it from the backup then also failed: "
                        f"{restore_exc}. The previous, still-valid token was "
                        f"left at {backup}; move it back to {path} by "
                        f"hand.") from None
            raise Refused(
                f"could not write the token to {path}: {exc}. Nothing was "
                f"written.") from None
        return backup

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Refused(
            f"{path} already exists; pass --replace to overwrite it with "
            f"the new token. Nothing was written.") from None
    except OSError as exc:
        raise Refused(
            f"could not create {path}: {exc}. Nothing was written.") from None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        Path(path).unlink(missing_ok=True)
        raise Refused(
            f"could not write the token to {path}: {exc}. Nothing was "
            f"written.") from None
    return None


def _split_caps(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


def _client_fields(withholds: list[str], holds_reserved: list[str] | None,
                   label: str) -> dict:
    """`{"withholds": [...]}` and optionally `{"holds_reserved": [...]}`,
    validated with `identity`'s own load-time validators so this command
    cannot write an entry `identity.resolve` would then refuse to serve.
    `_entry_capabilities`/`_entry_holds_reserved` are the exact functions
    `identity._client_tokens()` calls over every entry at read time."""
    meta: dict = {"withholds": withholds}
    try:
        _, withholds_fs = identity._entry_capabilities(
            meta, "approved_client", label)
    except identity._BadEntry as exc:
        raise Refused(str(exc)) from None
    fields: dict = {}
    if withholds_fs:
        fields["withholds"] = sorted(withholds_fs)
    if holds_reserved is not None:
        meta["holds_reserved"] = holds_reserved
        try:
            holds_fs = identity._entry_holds_reserved(
                meta, "approved_client", label, withholds_fs)
        except identity._BadEntry as exc:
            raise Refused(str(exc)) from None
        fields["holds_reserved"] = sorted(holds_fs)
    return fields


def _agent_fields(grants: list[str], kind: str, label: str) -> dict:
    """`{"grants": [...]}`, or `{}` when `grants` is empty — validated with
    `identity`'s own load-time validator so this command cannot write an
    entry `identity.resolve` would then refuse to serve.
    `_entry_capabilities` is the exact function `identity.resolve()` calls
    over every agents-file entry at read time; it refuses an unknown
    capability, a human-reserved one, and `roster.write` on any kind but
    `orchestrator`, so none of those rules are repeated here."""
    if not grants:
        return {}
    try:
        grants_fs, _ = identity._entry_capabilities({"grants": grants}, kind, label)
    except identity._BadEntry as exc:
        raise Refused(str(exc)) from None
    return {"grants": sorted(grants_fs)} if grants_fs else {}


def _build_entry(*, file: str, name: str, products: list[str], kind: str,
                  holds_reserved: list[str] | None, withholds: list[str],
                  grants: list[str], replace: bool) -> tuple[Path, dict, str]:
    """Validate everything `mint()` would write and generate the token, but
    write nothing: returns `(path, entries, token)`, where `entries` is the
    exact dict `path` should end up holding. Splitting build from commit
    lets the caller land `--out` before the store file is touched at all.

    Reads the two paths off `identity` by attribute at call time (never
    copied into a local), so a test that monkeypatches
    `identity.TOKENS`/`identity.CLIENT_TOKENS` after import — the same
    pattern `tests/test_bootstrap_library.py` already uses — reaches this
    function too."""
    path = FILES[file]()
    _check_mode(path)
    entries = _load_entries(path)

    existing = _tokens_named(entries, name)
    if existing and not replace:
        label = identity._entry_label(existing[0], entries[existing[0]])
        raise Refused(
            f"{name!r} already has a credential in {path} ({label}); pass "
            f"--replace to replace it. Nothing was written.")

    entry: dict = {"name": name, "products": products}
    if file == "agents":
        if kind != "agent":
            entry["kind"] = kind
        entry.update(_agent_fields(grants, kind, f"{name!r} (new)"))
    else:
        entry.update(_client_fields(withholds, holds_reserved, f"{name!r} (new)"))

    token = secrets.token_urlsafe(32)
    for tok in existing:
        del entries[tok]
    entries[token] = entry
    return path, entries, token


def _commit(path: Path, entries: dict) -> None:
    """Write `entries` to `path` atomically — the store commit, meant to run
    last, after `--out` (if any) has already fully landed."""
    _ensure_private_dir(path.parent)
    _atomic_write(path, json.dumps(entries, indent=2).encode("utf-8"))


def _revoke_entry(path: Path, name: str) -> None:
    """Remove the entry named `name` from `path`, atomically, leaving every
    other entry byte-for-byte as it was. Refuses, writing nothing, when no
    entry carries that name or more than one does — naming how many, never
    which token, since the file's keys are the bearer tokens themselves.
    Runs the same `_check_mode` guard the mint path runs, so a store file
    already wider than 0600 halts here too rather than being silently
    rewritten at 0600 by `_atomic_write`."""
    _check_mode(path)
    entries = _load_entries(path)
    matches = _tokens_named(entries, name)
    if not matches:
        raise Refused(
            f"no credential named {name!r} in {path}; nothing was written.")
    if len(matches) > 1:
        raise Refused(
            f"{len(matches)} credentials are named {name!r} in {path}; "
            f"refusing to guess which one to remove. Nothing was written.")
    del entries[matches[0]]
    _atomic_write(path, json.dumps(entries, indent=2).encode("utf-8"))


def mint(*, file: str, name: str, products: list[str], kind: str,
         holds_reserved: list[str] | None, withholds: list[str],
         grants: list[str] = (), replace: bool) -> str:
    """Add one credential to `identity.TOKENS` or `identity.CLIENT_TOKENS`;
    return the raw token. `main` does not call this directly — it needs the
    build and the commit as separate steps to sequence `--out` between them
    — but it stays as the one-call shape for anything else that mints a
    credential with no `--out` to land first."""
    path, entries, token = _build_entry(
        file=file, name=name, products=products, kind=kind,
        holds_reserved=holds_reserved, withholds=withholds, grants=list(grants),
        replace=replace)
    _commit(path, entries)
    return token


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--file", required=True, choices=("agents", "clients"),
                   help="which token file to write to")
    p.add_argument("--name", required=True,
                   help="the credential's name, recorded in audit columns; "
                        "under --revoke, the name of the entry to remove")
    p.add_argument("--revoke", action="store_true",
                   help="remove the entry named --name from the file instead "
                        "of minting one; refused, writing nothing, if no "
                        "entry or more than one entry carries that name. "
                        "Incompatible with --out, --replace, --products, "
                        "--kind, --grants, --holds-reserved and --withholds.")
    p.add_argument("--products", default=None,
                   help="comma-separated product scope (default: *, every product)")
    p.add_argument("--kind", choices=("agent", "system"), default=None,
                   help="--file agents only (default: agent); approved_client "
                        "is never nameable in the agents file")
    p.add_argument("--holds-reserved", default=None,
                   help="--file clients only: comma-separated human-reserved "
                        "capabilities this client holds (default: all of them)")
    p.add_argument("--withholds", default=None,
                   help="--file clients only: comma-separated capabilities "
                        "this client's role grants but does not hold")
    p.add_argument("--grants", default=None,
                   help="--file agents only: comma-separated capability "
                        "names to add to the role's capabilities — "
                        "identity.resolve_capabilities computes "
                        "(role_caps | grants) - withholds, so --kind agent "
                        "--grants review_record.create yields the agent "
                        "role plus that one capability")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="write the token to PATH instead of stdout, atomically, "
                        "at mode 0600 from creation, creating its parent "
                        "directory (and every missing ancestor) at 0700 if "
                        "missing; refuses an existing PATH unless --replace "
                        "is given, refuses a dangling symlink at PATH, and "
                        "refuses a PATH that resolves to either credential "
                        "store file. Without --out, the token goes to "
                        "stdout alone, at whatever mode a redirect (`>`) or "
                        "pipe gives the resulting file — typically the "
                        "shell's umask, not 0600.")
    p.add_argument("--replace", action="store_true",
                   help="replace an existing credential of the same --name, "
                        "and overwrite --out if it already exists; --out "
                        "must already be a regular file at mode 0600 or "
                        "narrower (chmod it first otherwise), and while the "
                        "replacement is in flight the previous contents sit "
                        "at <PATH>.mint-backup — if the command crashes, "
                        "that file may still be there holding the old, "
                        "still-valid token")
    args = p.parse_args(argv)

    name = args.name.strip()
    if not name:
        print("mint_credential: --name must not be empty", file=sys.stderr)
        return 2

    if args.revoke:
        conflicts = []
        if args.out is not None:
            conflicts.append("--out")
        if args.replace:
            conflicts.append("--replace")
        if args.products is not None:
            conflicts.append("--products")
        if args.kind is not None:
            conflicts.append("--kind")
        if args.grants is not None:
            conflicts.append("--grants")
        if args.holds_reserved is not None:
            conflicts.append("--holds-reserved")
        if args.withholds is not None:
            conflicts.append("--withholds")
        if conflicts:
            print(f"mint_credential: --revoke is incompatible with "
                  f"{', '.join(conflicts)}", file=sys.stderr)
            return 2
        path = FILES[args.file]()
        try:
            _revoke_entry(path, name)
        except Refused as e:
            print(f"mint_credential: {e}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"mint_credential: could not write {path}: {exc}.",
                  file=sys.stderr)
            return 1
        print(f"revoked {name!r} from {path}", file=sys.stderr)
        return 0

    if args.file == "clients" and args.kind is not None:
        print("mint_credential: --kind applies only to --file agents; every "
              "entry in the approved-client file resolves as approved_client "
              "regardless of what it says", file=sys.stderr)
        return 2
    if args.file == "agents" and (args.holds_reserved is not None
                                   or args.withholds is not None):
        print("mint_credential: --holds-reserved and --withholds apply only "
              "to --file clients", file=sys.stderr)
        return 2
    if args.file == "clients" and args.grants is not None:
        print("mint_credential: --grants applies only to --file agents",
              file=sys.stderr)
        return 2

    products_raw = args.products if args.products is not None else "*"
    products = [x.strip() for x in products_raw.split(",") if x.strip()]
    if not products:
        print("mint_credential: --products must name at least one product, "
              "or '*'", file=sys.stderr)
        return 2

    out_path = Path(args.out).expanduser() if args.out is not None else None
    if out_path is not None:
        try:
            _check_out_not_store(out_path)
            _check_out_dangling(out_path)
            if args.replace:
                _check_out_replace_target(out_path)
        except Refused as e:
            print(f"mint_credential: {e}", file=sys.stderr)
            return 1

    try:
        path, entries, token = _build_entry(
            file=args.file, name=name, products=products,
            kind=args.kind or "agent",
            holds_reserved=(_split_caps(args.holds_reserved)
                            if args.holds_reserved is not None else None),
            withholds=_split_caps(args.withholds),
            grants=_split_caps(args.grants),
            replace=args.replace)
    except Refused as e:
        print(f"mint_credential: {e}", file=sys.stderr)
        return 1

    # The token reaches --out fully written, fsynced and renamed before the
    # store commit below ever runs — so a store-commit failure never leaves
    # a live credential sitting in --out that nobody saw, and a --out
    # failure never leaves a live credential committed to the store either.
    # If --replace overwrote a PATH that already held a working credential,
    # _write_out kept it aside as `backup_path`; the store still serves that
    # credential until the commit below lands, so a commit failure restores
    # it rather than leaving PATH empty.
    backup_path = None
    if out_path is not None:
        try:
            backup_path = _write_out(out_path, token, args.replace)
        except Refused as e:
            print(f"mint_credential: {e}", file=sys.stderr)
            return 1

    try:
        _commit(path, entries)
    except OSError as exc:
        if out_path is not None and backup_path is not None:
            try:
                os.replace(backup_path, out_path)
            except OSError as restore_exc:
                print(f"mint_credential: could not write the credential to "
                      f"{path}: {exc}. Restoring {out_path} from the backup "
                      f"then also failed: {restore_exc}. The previous, "
                      f"still-valid token was left at {backup_path}; move it "
                      f"back to {out_path} by hand.", file=sys.stderr)
                return 1
            note = (f" {out_path} was restored to its previous contents; "
                    "nothing else was written.")
        elif out_path is not None:
            out_path.unlink(missing_ok=True)
            note = (f" The token already written to {out_path} was removed. "
                    "Nothing was left behind.")
        else:
            note = " Nothing was left behind."
        print(f"mint_credential: could not write the credential to {path}: "
              f"{exc}.{note}", file=sys.stderr)
        return 1

    if backup_path is not None:
        backup_path.unlink(missing_ok=True)

    print(f"credential added to {path} as {name!r}; present it as "
          f"`Authorization: Bearer <token>`. identity re-reads this file "
          f"per request, so no restart is needed.", file=sys.stderr)
    if out_path is not None:
        print(f"token written to {out_path}, mode 0600.", file=sys.stderr)
    else:
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
