"""The reconcile of `projects/<project>/profiles/<variant>.yaml` into the
`profiles` table, shared by the two callers that must not drift apart:

- `load_crew_registry.py profiles`, the operator-run load, and
- `refresh()` below, which a profile read runs when the served knowledge
  checkout's profile files have changed since the last attempt.

Before `refresh()`, a profile merged to knowledge `main` took effect only
after someone ran the operator load by hand. On 2026-10-05 `coder` could not
be dispatched for about a day because its stored row predated knowledge
c30f1b99, and nothing in `kb merge`, `kb sync` or the Library ran the load
(issue_01M45AGVTEH7EZMS45M6PGJ0Y7).

**The change detection is `projects.py`'s (library#206).** The last attempt is
keyed by a signature of the files it read, `(path, mtime_ns, size, inode)` per
file. A read re-stats the files and reconciles only when the signature moved,
under a lock, so the common read costs one glob and one stat per file.

**A file that fails to load keeps its stored row, and so does every row
whose file is still on disk.** `stale()` decides "gone" from the paths on
disk, not from the rows that parsed, so a malformed file cannot delete its own
row by failing.

**The automatic path deletes a row only once its absence is confirmed.** The
first scan that finds the file absent holds the row. A scan at least
`CONFIRM_ABSENT_SECONDS` later that still finds it absent deletes it. The
operator command deletes on the first absence, because it runs on a settled
tree.

**Both callers refuse a root they cannot trust.** A missing or empty checkout,
or one whose `projects/` holds no profile file, would make every stored row
look stale and delete the table (load_crew_registry.py, PR #131 rounds 1-2).
The automatic path logs the refusal and keeps serving the stored rows.

**The automatic path respects the write gate.** Its writes go through
`crew_registry`, which refuses with `store.WritesDisabled` while
`LIBRARY_WRITES_ENABLED` is off. `refresh()` logs that once per signature and
serves the stored rows, as the read routes do today.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
import threading
import time

import yaml

import crew_registry
import identity
import store

# The served knowledge checkout, read once at import from the variable
# `reference_admission` reads too. Unset means no reload, so the stored rows
# are served as they are, which is what every test fixture expects.
KNOWLEDGE_ROOT = os.environ.get("LIBRARY_KNOWLEDGE_ROOT") or None

# Recorded as `updated_by` on every row a reload writes, so a row's author
# says the change came from the reload rather than an operator.
AUTOMATIC_ACTOR = "library: automatic profile reload"

_logger = logging.getLogger(__name__)


class ProfileRootError(ValueError):
    """The root cannot be trusted to reconcile against. Nothing is written."""


class ProfileFileError(ValueError):
    """One profile file cannot be loaded. Its stored row is left as it is.

    `project` and `variant` come from the file's path, so the error still
    names the row the file would have written."""

    def __init__(self, project: str, variant: str, message: str):
        super().__init__(message)
        self.project = project
        self.variant = variant


def profile_paths(root: str) -> list[str]:
    """Every `projects/<project>/profiles/<variant>.yaml` under `root`,
    sorted."""
    return sorted(glob.glob(
        os.path.join(root, "projects", "*", "profiles", "*.yaml")))


def _check_root(root: str) -> None:
    """Refuse a root the reconcile cannot trust.

    **An empty directory, a missing one, and a genuine checkout with no
    profiles all glob to the same empty list**, and the wrong read deletes
    every row rather than none. This checks the structural marker every
    profile path depends on, so the glob result alone is never the whole
    test.
    """
    projects_dir = os.path.join(root, "projects")
    if not os.path.isdir(projects_dir) or not os.listdir(projects_dir):
        raise ProfileRootError(
            f"{root!r} does not look like a knowledge checkout — no non-empty "
            f"'projects' directory under it; refusing to reconcile profiles "
            f"against it")


def _read(path: str) -> dict:
    """One profile file as `{project, variant, include, claude_md_extras}`.

    **The path segments are the source of truth for `project` and `variant`,
    checked against the YAML's own `profile:` key.** A mismatch is a fact
    about the file worth stopping on, not something to resolve silently.
    """
    project = os.path.basename(os.path.dirname(os.path.dirname(path)))
    variant = os.path.splitext(os.path.basename(path))[0]
    # ValueError covers UnicodeDecodeError (a file that is not UTF-8) and the
    # ValueError PyYAML raises for a value such as an impossible date.
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError, ValueError) as e:
        raise ProfileFileError(project, variant,
                               f"{path} does not parse: {e}")
    declared = doc.get("profile") if isinstance(doc, dict) else None
    if declared != f"{project}/{variant}":
        raise ProfileFileError(
            project, variant,
            f"{path} declares profile {declared!r}, which does not match its "
            f"own path {project}/{variant}")
    row = {
        "project": project,
        "variant": variant,
        "include": doc.get("include", []),
        "claude_md_extras": doc.get("claude_md_extras", []),
    }
    # TypeError: `_validate_include_block` sorts the unknown keys of a block,
    # and a block with both a non-string key and a string key cannot be sorted.
    try:
        crew_registry.validate_profile(
            project, variant, row["include"], row["claude_md_extras"])
    except (crew_registry.InvalidRosterRow, TypeError) as e:
        raise ProfileFileError(project, variant, f"{path}: {e}")
    return row


def load(root: str) -> tuple[list[dict], list[ProfileFileError]]:
    """Every profile file under `root`, as `(loaded, failed)`.

    `loaded` holds the rows of the files that parse and validate. `failed`
    holds one `ProfileFileError` per file that does not. Raises
    `ProfileRootError` before reading any file when the root is not trusted,
    and when it holds no profile file at all.
    """
    _check_root(root)
    paths = profile_paths(root)
    if not paths:
        raise ProfileRootError(
            f"found no profiles under {root!r} (looked for "
            f"projects/*/profiles/*.yaml); refusing to reconcile, since that "
            f"would delete every row in the table. Check the path.")
    loaded: list[dict] = []
    failed: list[ProfileFileError] = []
    for path in paths:
        try:
            loaded.append(_read(path))
        except ProfileFileError as e:
            failed.append(e)
    return loaded, failed


def stale(existing: list[dict], loaded: list[dict],
          failed: list[ProfileFileError]) -> list[tuple[str, str]]:
    """The stored `(project, variant)` keys whose file is gone, sorted.

    A file that is present but failed to load is not gone, so its row is
    not in this list."""
    present = {(r["project"], r["variant"]) for r in loaded}
    present |= {(e.project, e.variant) for e in failed}
    return sorted(key for key in
                  ((r["project"], r["variant"]) for r in existing)
                  if key not in present)


def apply(ident: identity.Identity, loaded: list[dict],
          gone: list[tuple[str, str]]) -> None:
    """Upsert every loaded row, then delete the `gone` rows. Each write goes
    through `crew_registry`, which checks the capability and the write gate."""
    for row in loaded:
        crew_registry.profile_put(
            ident, row["project"], row["variant"], row["include"],
            row["claude_md_extras"])
    for project, variant in gone:
        crew_registry.profile_delete(ident, project, variant)


# --- the automatic reload ----------------------------------------------------

_UNATTEMPTED = object()
_lock = threading.Lock()
# The signature of the last attempt that did not fail transiently. See
# `refresh()` for what is recorded.
_attempted = _UNATTEMPTED

# After a transient failure, reads serve the stored rows until this many seconds
# have passed, rather than each reconciling again. A failing writer otherwise
# costs every read a full parse of the checkout and a wait on the busy timeout,
# all under `_lock`.
RETRY_BACKOFF_SECONDS = 30
# A row whose file is absent is deleted only by a scan at least this many
# seconds after the first scan that found it absent. The checkout's fast-forward
# rewrites a changed file within milliseconds, so a scan inside that window
# never confirms a removal.
CONFIRM_ABSENT_SECONDS = 10
_clock = time.monotonic
_retry_at = None
# `(project, variant)` -> the clock reading of the first scan that found the
# row's file absent, for rows held and not yet confirmed.
_absent_since: dict[tuple[str, str], float] = {}
_recheck_at = None


def _signature(root: str) -> tuple:
    """`(path, mtime_ns, size, inode)` per profile file, sorted by path. A
    file that vanishes between the glob and its stat is left out, which moves
    the signature on the next read."""
    sig = []
    for path in profile_paths(root):
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig.append((path, st.st_mtime_ns, st.st_size, st.st_ino))
    return tuple(sig)


def _unchanged(row: dict, stored: dict) -> bool:
    """Whether the stored row already holds `row`'s `include` and
    `claude_md_extras`. Writing it again would only move its `updated_at` and
    `updated_by`, which name the change that last altered the row."""
    current = stored.get((row["project"], row["variant"]))
    return (current is not None
            and json.loads(current["include"]) == row["include"]
            and json.loads(current["claude_md_extras"]) == row["claude_md_extras"])


def _reconcile(root: str):
    """One reconcile of `root` into the table. Returns `(loaded, failed,
    confirmed, held)`: `confirmed` are the rows deleted, and `held` maps each
    absent row this scan did not delete to when it was first seen absent."""
    loaded, failed = load(root)
    conn = store.connect()
    try:
        stored = {(r["project"], r["variant"]): dict(r) for r in conn.execute(
            "SELECT project, variant, include, claude_md_extras FROM profiles")}
    finally:
        conn.close()
    gone = stale(list(stored.values()), loaded, failed)
    now = _clock()
    first_seen = {key: _absent_since.get(key, now) for key in gone}
    confirmed = [key for key in gone
                 if now - first_seen[key] >= CONFIRM_ABSENT_SECONDS]
    held = {key: at for key, at in first_seen.items() if key not in confirmed}
    changed = [row for row in loaded if not _unchanged(row, stored)]
    apply(identity.Identity(actor=AUTOMATIC_ACTOR, kind="orchestrator"),
          changed, confirmed)
    return loaded, failed, confirmed, held


def _wanted(sig) -> bool:
    """Whether a read reconciles for `sig`: the checkout moved since the last
    attempt, or a held row's confirming scan is due."""
    if sig != _attempted:
        return True
    return _recheck_at is not None and _clock() >= _recheck_at


def _backing_off() -> bool:
    return _retry_at is not None and _clock() < _retry_at


def refresh() -> None:
    """Reconcile the table against the served checkout if its profile files
    changed since the last attempt. Called by the read paths that consume
    profiles, after the caller has authenticated.

    **What is recorded as attempted decides when the next read retries.** A
    refusal (the root, the write gate) and a file that fails to load are
    deterministic, so the signature is recorded and the warning logged once
    per broken state; the next file change retries. A `sqlite3.Error` is
    transient (`store.connect_rw` can time out waiting on a writer), so the
    signature is not recorded. Instead reads serve the stored rows for
    `RETRY_BACKOFF_SECONDS`, then the next read retries. The failure is logged
    once, when it happens. A `store.MisconfiguredDatabase` takes the same
    path, so the stored rows are served and the failure is logged once.

    **A reconcile that does not succeed forgets its absence holds.** A hold
    records a scan that found a row's file absent, so one kept across a failure
    would let the next absent scan confirm a removal it did not observe twice.
    """
    global _attempted, _retry_at, _absent_since, _recheck_at
    root = KNOWLEDGE_ROOT
    if not root or _backing_off() or not _wanted(_signature(root)):
        return
    with _lock:
        if _backing_off():
            return
        sig = _signature(root)
        if not _wanted(sig):
            return
        try:
            loaded, failed, confirmed, held = _reconcile(root)
        except ProfileRootError as e:
            _logger.warning("profile reload skipped: %s", e)
            _absent_since = {}
            _recheck_at = None
        except store.WritesDisabled as e:
            _logger.warning("profile reload skipped, writes are off: %s", e)
            _absent_since = {}
            _recheck_at = None
        except sqlite3.Error as e:
            # _recheck_at stays armed so the read after the backoff retries.
            _absent_since = {}
            _retry_at = _clock() + RETRY_BACKOFF_SECONDS
            _logger.warning("profile reload failed, serving the stored rows and "
                            "retrying in %d s: %s", RETRY_BACKOFF_SECONDS, e)
            return
        except store.MisconfiguredDatabase as e:
            # _recheck_at stays armed so the read after the backoff retries a
            # held removal even if the checkout has not changed. The write
            # routes refuse on their own connect_rw, so the stored rows are
            # served here as they are for sqlite.
            _absent_since = {}
            _retry_at = _clock() + RETRY_BACKOFF_SECONDS
            _logger.warning("profile reload failed, LIBRARY_DB is misconfigured, "
                            "serving the stored rows and retrying in %d s: %s",
                            RETRY_BACKOFF_SECONDS, e)
            return
        else:
            for error in failed:
                _logger.warning("profile file not loaded, its stored row is "
                                "kept: %s", error)
            _logger.info("profile reload: %d loaded, %d removed, %d held for a "
                         "confirming scan", len(loaded), len(confirmed),
                         len(held))
            _absent_since = held
            _recheck_at = (min(held.values()) + CONFIRM_ABSENT_SECONDS
                           if held else None)
        _attempted = sig
        _retry_at = None


def reset() -> None:
    """Forget the last attempt and any backoff, so the next read reconciles.
    Tests only, matching `projects.reload()`."""
    global _attempted, _retry_at, _absent_since, _recheck_at
    _attempted = _UNATTEMPTED
    _retry_at = None
    _absent_since = {}
    _recheck_at = None
