"""Content-addressed blob storage for the Archives (ADR-0020 §"structured
records that point at blobs").

**Local, on the Library's own machine, deliberately — not the cloud object
store ADR-0020's language (Object Lock, retention locks) more literally
describes.** Everything else this service owns is a local file on the mini,
this Library has no cloud credentials to provision, and content-addressing
gives the one property that matters most for a later migration: a blob's
identity is its hash, never its location, so moving the bytes to S3 or GCS is
a backend swap behind `write_blob`/`read_blob`, not a schema change to every
row that references one.

**Content-addressed, so a write is idempotent by construction.** The same
bytes hash to the same ref whoever submits them, so a resumed or retried
capture that resends a turn it already sent costs a `stat` and nothing more —
no separate dedup bookkeeping, unlike `signals.dedup_key`, which exists
because a signal's IDENTITY is not its bytes.

**Write-gated the same way every other write path in this service is, and
for the same reason.** `store.WRITES_ENABLED` is the single door
`store.connect_rw` refuses behind during the shadow; a blob store with its
own, different gate would be a second place deciding whether this process
may write, which is exactly the class of drift ADR-0020's "only the Library
holds blob-store write credentials" exists to prevent. Reading a blob carries
no such gate — a blob written before a restart must stay readable through
one, the same way `signals` rows do.

**No per-caller quota, and that gap is real rather than papered over
(security-reviewer, PR #40).** Every identity holding `signal.create` — the
whole `agent` role, not a narrow admin set — can still write up to
`MIN_FREE_BYTES` of headroom's worth of distinct 20 MB blobs (`store.
MAX_BLOB_BYTES`), and this codebase's own agents are documented elsewhere
as retrying and resending, so a bug that varies content slightly per retry
needs no malice to add up. What `MIN_FREE_BYTES` below bounds is narrower
and specific: not who may write how much, but that this filesystem never
actually reaches zero free bytes — because it is the SAME filesystem the
Library's own SQLite database lives on, so a full disk here is a
full-service outage, not a feature-scoped failure. A real per-caller quota
is future work if the gap above ever gets exercised; the floor below is
what stands in for it today.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

import store

# Refuse a write that would leave less than this much free on the blob
# store's filesystem. See the module docstring for what this does and does
# not bound.
MIN_FREE_BYTES = 1_000_000_000


class MisconfiguredBlobStore(RuntimeError):
    """`LIBRARY_BLOB_DIR` is unset, or names a path this process cannot serve
    blobs from.

    **Mirrors `store.MisconfiguredDatabase`** (memory-retention epic clause
    8, issue_01M35R5KXQJGWPRFC9BGEZAYQE): the blob store's location is
    configuration, not a constant, and a missing NAS mount must not look
    like a store with no blobs in it. `resolve_blob_dir` raises this at
    import — before the app binds a port — for the same reason
    `store.resolve_db` raises at import rather than letting the service
    start and fail requests one at a time: a service that starts on a
    misconfigured store reports healthy while every blob is unreachable.
    """


def resolve_blob_dir(env=None) -> Path:
    """The directory this process reads and writes blobs under.

    **No default.** A fallback to `~/.library/blobs` here is indistinguishable
    from the real store at that path once the two happen to share a name — an
    unmounted NAS at the configured location left the service silently
    writing a fresh, empty directory tree at the fallback instead of refusing,
    which is the failure clause 8 exists to close.

    **Existence, directory-ness and writability are all checked here, at
    import, rather than left for the first write to discover.** A write-time
    failure would be `write_blob`'s problem alone; a service that has already
    bound its port and answered `/health` by the time it hits that is a
    service reporting healthy while blobs are unreachable, which is exactly
    what this function exists to prevent.

    **The directory is never created here.** `write_blob` still creates the
    two-character shard directory under an existing root (unchanged), but the
    root itself is configuration a human or a mount point provides — creating
    it silently is how an absent NAS mount comes to look like an empty,
    otherwise-healthy store.
    """
    env = os.environ if env is None else env
    raw = env.get("LIBRARY_BLOB_DIR")
    if not raw:
        raise MisconfiguredBlobStore(
            "LIBRARY_BLOB_DIR is not set. The blob store's location is "
            "configuration, not a constant, and there is no default this "
            "can fall back to without risking a missing NAS mount looking "
            "like a store with no blobs in it. Set LIBRARY_BLOB_DIR to the "
            "blob store's root directory.")
    path = Path(raw).expanduser()
    if not path.exists():
        raise MisconfiguredBlobStore(
            f"LIBRARY_BLOB_DIR names {path}, which does not exist. Refusing "
            f"to start rather than create it: the Library does not create "
            f"its own blob store root, because doing so is how a missing "
            f"mount comes to look like an empty, otherwise-healthy store. "
            f"Create the directory (or mount it) before starting.")
    if not path.is_dir():
        raise MisconfiguredBlobStore(
            f"LIBRARY_BLOB_DIR names {path}, which exists but is not a "
            f"directory — refusing to start.")
    if not os.access(path, os.W_OK):
        raise MisconfiguredBlobStore(
            f"LIBRARY_BLOB_DIR names {path}, which is not writable by this "
            f"process — refusing to start.")
    return path


BLOB_DIR = resolve_blob_dir()

# A SHA-256 hex digest, and nothing else — checked before ANY path is built
# from a caller-influenced `ref`, on both the write return value and every
# read. A ref that reaches `_path` unchecked is a path-traversal opportunity:
# `../../etc/passwd` is a syntactically fine string and a catastrophic path
# component (security-reviewer doctrine already stated for `db_uri` in
# `store.py` and `migrate.py` — the same class of bug, a different field).
#
# **`\Z`, NOT `$`.** `$` matches immediately before a trailing `\n`, so
# `"0"*64 + "\n"` would pass a `$`-anchored check with a newline riding
# along into the final path component — not reachable by any ref this
# module produces or reads today (security-reviewer, PR #40: traced every
# path a ref can take), but this function's whole job is being an airtight
# guard for whichever caller reaches it next.
_REF = re.compile(r"^[0-9a-f]{64}\Z")


class InvalidRef(ValueError):
    """A blob ref that is not a 64-character lowercase hex SHA-256 digest."""


class StorageFull(RuntimeError):
    """Refused a write that would leave less than `MIN_FREE_BYTES` free on
    the blob store's filesystem — the same one the Library's own database
    lives on."""


def _path(ref: str) -> Path:
    if not _REF.match(ref):
        raise InvalidRef(f"{ref!r} is not a 64-character lowercase hex digest")
    return BLOB_DIR / ref[:2] / ref


def write_blob(content: bytes) -> str:
    """Store `content`, keyed by its own SHA-256. Returns the hex digest.

    **Written to a temp file in the same directory, then renamed, never
    written in place.** `os.rename` is atomic on the same filesystem, so a
    reader can never observe a partially-written blob — it sees either the
    old state (nothing at this path) or the new one (the complete content),
    with no window where the file exists but is still being written. Two
    concurrent writers of the SAME content race harmlessly: both compute the
    same ref, both rename to the same final path, and the second rename
    either succeeds over an identical file or the first one already did —
    either way the bytes at `ref` are correct, because they can only ever be
    the bytes that hashed to it.

    **Chmod read-only immediately after the rename, a partial answer to
    ADR-0020's append-only requirement** (reviewer, PR #40, citing
    knowledge#159: "append-only enforced only by the Library's write path
    bounds the damage to what the Library chooses to do, which is no bound
    at all once the Library is the thing that was compromised"). This is
    that bound stated in the filesystem rather than only in code — a
    compromised or buggy process still has to go out of its way (reopen for
    write, or `chmod` back) to overwrite an existing blob, rather than doing
    it by the same code path an ordinary write already uses. It is not
    Object Lock or a retention lock, and does not claim to be; full
    enforcement is deferred to whichever backend a later migration puts
    behind this same function, the same way ADR-0020 already defers the
    credential boundary to the write cutover.

    **A crash between the write and the rename leaves an orphaned temp
    file, accepted rather than guarded** (reviewer, PR #40). This runs on
    one controlled machine; a `.{ref}.{pid}.tmp` surviving a disk-full or a
    `kill -9` is cheap to find and remove by hand, and adding a startup
    sweep for a failure mode this rare is not worth the code it would cost.
    """
    if not store.WRITES_ENABLED:
        raise store.WritesDisabled(
            "the Library is in its read-only shadow: blob writes are gated "
            "the same way every other write is. Set LIBRARY_WRITES_ENABLED=1 "
            "once the vault is this service's own file and Orient has "
            "stopped writing to it.")
    ref = hashlib.sha256(content).hexdigest()
    dest = _path(ref)
    # **A TIMING SIDE CHANNEL, ACCEPTED FOR TODAY'S CONTENT SHAPE** (reviewer,
    # PR #40). This short-circuit is faster (a `stat`) than the write+rename
    # below, so a caller holding no more than `signal.create` could in
    # principle time whether specific byte-exact content already exists
    # anywhere in the store, including under a product or privacy_class it
    # cannot read — the classic content-addressed-dedup existence oracle.
    # Exploiting it needs the attacker to already hold the exact candidate
    # bytes, which is a real bound: today's content is free-form transcript
    # and archive text, not a fixed-format secret or token an attacker could
    # plausibly guess byte-for-byte. Equalizing timing (always write+rename,
    # discarding the redundant copy) would also defeat the reason this
    # short-circuit exists — a resumable capture is EXPECTED to resend
    # content it already sent, routinely, and a full write on every resend
    # is the cost that dedup exists to avoid. Revisit if a future caller
    # ever stores fixed-format secrets through this path.
    if dest.is_file():
        return ref
    # **THE ONE BOUND THIS MODULE DOES ENFORCE**, checked against the
    # database's own directory rather than `BLOB_DIR`'s — they are the same
    # filesystem in every real deployment, and the database's directory is
    # guaranteed to exist where a fresh `BLOB_DIR` might not be yet. Not a
    # per-caller quota (see the module docstring); a floor under everyone.
    free = shutil.disk_usage(store.DB_PATH.parent).free
    if free - len(content) < MIN_FREE_BYTES:
        raise StorageFull(
            f"writing {len(content)} more byte(s) would leave under "
            f"{MIN_FREE_BYTES} free on {store.DB_PATH.parent} — refusing "
            f"rather than risk the database sharing that filesystem "
            f"running out of room")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{ref}.{os.getpid()}.tmp"
    try:
        tmp.write_bytes(content)
        os.rename(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(dest, 0o444)
    return ref


def read_blob(ref: str) -> bytes:
    """The bytes stored under `ref`. Raises `InvalidRef` for a malformed ref
    and `FileNotFoundError` for a well-formed one nothing was ever written
    under — two different failures, on purpose: the first is a caller error
    before any disk access, the second is "no such blob" after one."""
    return _path(ref).read_bytes()


# --- quarantine: the two-phase delete `discard_sweep.py` drives -------------
#
# **THE RACE THIS CLOSES** (security-reviewer, PR #180): `app.py`'s
# `create_signal` calls `write_blob` BEFORE inserting the row that names it
# (see that call site's own comment for why that order is deliberate), and
# `write_blob`'s content-addressed short-circuit never touches an existing
# file's mtime, so an outright delete run between a concurrent submission's
# `write_blob` and its `INSERT` would remove the very bytes that INSERT is
# about to name — a plain grace-period check on the file's mtime
# cannot see that gap, because the short-circuit gives the file no fresh
# mtime to check. Quarantining instead of deleting closes it without
# depending on mtime at all: `write_blob` only ever finds a file at `ref`'s
# OWN path, and quarantining moves the file OFF that path, so a write that
# lands after the quarantine rename finds nothing there and writes a fresh
# copy — never observing, racing against, or colliding with the quarantined
# one.
#
# Longest a request can plausibly spend between `write_blob` returning and
# its `INSERT` committing: that `INSERT` can itself block for the whole of
# `store.BUSY_TIMEOUT_MS` (5 s) waiting for the write lock, on top of the
# ordinary scheduling delay of the thread-pool hop `write_blob` already
# crosses in `create_signal`. `QUARANTINE_GRACE_SECONDS` is set with a wide,
# unmeasured margin above that combined worst case — the same margin
# `discard_sweep.SWEEP_BATCH_SIZE` takes against its own unmeasured
# per-batch cost, for the same reason: nothing here has been timed against
# a loaded instance of this specific request path.
QUARANTINE_GRACE_SECONDS = 60

# Dot-prefixed and suffixed with a literal word rather than a pid, so it can
# never collide with `write_blob`'s own `.{ref}.{pid}.tmp` temp-file pattern
# in the same directory, and so a second quarantine attempt for the SAME ref
# (harmless — content-addressing guarantees identical bytes under one ref)
# lands on the one name rather than accumulating pid-numbered duplicates.
_QUARANTINE_SUFFIX = ".quarantine"


def _quarantine_path(ref: str) -> Path:
    dest = _path(ref)
    return dest.parent / f".{ref}{_QUARANTINE_SUFFIX}"


def quarantine_blob(ref: str) -> bool:
    """Move the blob stored under `ref` out of its content-addressed path
    into a quarantine name in the SAME directory, atomically. Returns
    whether a file was actually moved: `False` reads as "nothing there to
    quarantine", not an exception, the same idempotent-report convention
    every mutator in this module uses.

    `os.rename` within one directory is atomic, so a concurrent `write_blob`
    for the same content — a resend `write_blob`'s own docstring calls
    EXPECTED — never observes a half-moved state: it either still finds the
    file at `ref` (and short-circuits, same as always) or finds nothing
    there (because this already moved it) and writes a fresh copy. Either
    way it never touches the quarantined file.

    The quarantine file's mtime is set to the moment of THIS call (`rename`
    does not touch a file's own mtime), so `quarantined_refs` can read back
    how long it has sat there.
    """
    if not store.WRITES_ENABLED:
        raise store.WritesDisabled(
            "the Library is in its read-only shadow: blob writes are gated "
            "the same way every other write is. Set LIBRARY_WRITES_ENABLED=1 "
            "once the vault is this service's own file and Orient has "
            "stopped writing to it.")
    src = _path(ref)
    dest = _quarantine_path(ref)
    try:
        os.rename(src, dest)
    except FileNotFoundError:
        return False
    os.utime(dest, None)
    return True


def restore_blob(ref: str) -> bool:
    """Move a quarantined blob back to its content-addressed path — ONLY if
    nothing is there already. A concurrent `write_blob` may have written a
    fresh copy at `ref` while the quarantined one waited out the grace
    period; that fresh copy wins, and the quarantined duplicate (guaranteed
    byte-identical, since both share the one hash `ref` names) is dropped
    rather than restored over it. Returns whether a file was actually moved
    back into place — `False` covers both "a fresh copy already there" and
    "nothing was quarantined at all".
    """
    if not store.WRITES_ENABLED:
        raise store.WritesDisabled(
            "the Library is in its read-only shadow: blob writes are gated "
            "the same way every other write is. Set LIBRARY_WRITES_ENABLED=1 "
            "once the vault is this service's own file and Orient has "
            "stopped writing to it.")
    src = _quarantine_path(ref)
    dest = _path(ref)
    if dest.is_file():
        src.unlink(missing_ok=True)
        return False
    try:
        os.rename(src, dest)
    except FileNotFoundError:
        return False
    os.chmod(dest, 0o444)
    return True


def discard_quarantined(ref: str) -> bool:
    """Unlink a quarantined blob outright — nothing came to reference `ref`
    again inside the grace period. Returns whether a file was actually
    removed, the same idempotent-report convention `quarantine_blob` and
    `restore_blob` use."""
    if not store.WRITES_ENABLED:
        raise store.WritesDisabled(
            "the Library is in its read-only shadow: blob writes are gated "
            "the same way every other write is. Set LIBRARY_WRITES_ENABLED=1 "
            "once the vault is this service's own file and Orient has "
            "stopped writing to it.")
    try:
        _quarantine_path(ref).unlink()
        return True
    except FileNotFoundError:
        return False


def quarantined_refs() -> list[tuple[str, float]]:
    """Every ref currently sitting in quarantine, each with the time it was
    quarantined (its file's mtime, set explicitly by `quarantine_blob`).
    Discovered by scanning the blob store's own shard directories — nothing
    else records which refs are quarantined, so a pass interrupted between
    quarantining a blob and re-checking it leaves a file this same scan
    finds again on the next pass, the same way it finds one quarantined by
    the current pass. Read-only; carries no write gate."""
    if not BLOB_DIR.is_dir():
        return []
    out = []
    for shard in BLOB_DIR.iterdir():
        if not shard.is_dir():
            continue
        for f in shard.glob(f".*{_QUARANTINE_SUFFIX}"):
            ref = f.name[1:-len(_QUARANTINE_SUFFIX)]
            if _REF.match(ref):
                out.append((ref, f.stat().st_mtime))
    return out
