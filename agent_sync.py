"""The reconcile of `projects/<crew>/agents/<name>.md` into the
`roster_agents` table (issue_01M5H8RPFPM6VBVJCPBND1XVBT).

Where a crew's agents used to be named: `ALLOWED_AUDIENCE`, a Python set
literal in agent-platform's `lib/knowledge/knowledge.py`, seeded once by
`load_crew_registry.py` at a fresh install and never touched again. Once
that literal shrinks to Builder's seed roster, a crew's OWN agents reach
`roster_agents` no other way, because `agent-platform` holds no credential
with `roster.write` (`identity.py` grants it only to the orchestrator role).
No crew names its agents at registration either — a name first exists when
its definition lands under `projects/<crew>/agents/<name>.md` — so the
Library is the one side of this that can notice a new definition and add it.

**Beside `profile_sync.py`, not a growth of it.** The two tables are
different shapes of state: a profile is upserted and deleted in place, a
roster name is only ever added, never removed (see `apply()` below). What
they share is the mechanism — `_signature`'s change detection, the served
checkout at `KNOWLEDGE_ROOT`, and the capability-checked write through a
`crew_registry` function attributed to `AUTOMATIC_ACTOR` rather than to a
caller — so this module borrows that mechanism's shape rather than its
table's.

**This reads the served knowledge tree on disk, the same files `profile_
sync.py` globs, not the vector index `reference.py` serves.** Hanging the
reconcile here rather than on `kb merge` is what covers a direct `kb_create`
or `kb_update` write too: both land in this checkout before any PR exists to
merge, so a scan keyed to the tree's own files sees them on the same next
read a merged one would.

**A name is added, never removed.** A document that stops supplying a name
— deleted, or edited to a different `kind` — is flagged instead:
`_flag_missing` logs one signal, idempotently per name, and `crew_profiles.
roster()` computes `source_doc_missing` fresh on every read. Removing the
row outright could strand a document that already names the agent as an
`audience` or in `binds_agents`, which is the harm `profile_sync`'s own
confirmed-delete exists to avoid for a profile and this module avoids
altogether by never deleting at all.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import sqlite3
import threading
import time

import yaml

import crew_registry
import identity
import store

# The served knowledge checkout, read once at import — the same variable
# name `profile_sync.py` and `reference_admission.py` each read independently
# from the same environment variable, rather than importing one another's
# copy.
KNOWLEDGE_ROOT = os.environ.get("LIBRARY_KNOWLEDGE_ROOT") or None

# Recorded as `created_by`/`updated_by` on every row this reconcile inserts,
# and as the signal's `actor`/`ingested_by`, so either reads as the reload
# rather than an operator (matching `profile_sync.AUTOMATIC_ACTOR`).
AUTOMATIC_ACTOR = "library: automatic roster reconcile"

_logger = logging.getLogger(__name__)


class AgentRootError(ValueError):
    """The root cannot be trusted to reconcile against. Nothing is added or
    flagged."""


class AgentFileError(ValueError):
    """One agent definition file cannot be read or parsed. It contributes no
    name this scan, and — matching `profile_sync.ProfileFileError`'s same
    reasoning for a malformed profile — its absence from the loaded set does
    not flag the row it would have supplied as missing, since the file is
    still there, just not readable right now."""

    def __init__(self, crew: str, name: str, message: str):
        super().__init__(message)
        self.crew = crew
        self.name = name


# The YAML frontmatter block a `kind: agent` document opens with: a line of
# exactly `---`, the block, then a closing `---` on its own line (or at
# end of file, for a document with no body below it).
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n|\Z)",
                          re.S)


def agent_paths(root: str) -> list[str]:
    """Every `projects/<crew>/agents/<name>.md` under `root`, sorted."""
    return sorted(glob.glob(
        os.path.join(root, "projects", "*", "agents", "*.md")))


def _check_root(root: str) -> None:
    """Refuse a root the reconcile cannot trust — the same check `profile_
    sync._check_root` runs, for the same reason: an empty or missing
    `projects` directory globs to the same empty list a genuine checkout
    with no agent definitions yet would, and reconciling against it would
    read every already-added row as having lost its source."""
    projects_dir = os.path.join(root, "projects")
    if not os.path.isdir(projects_dir) or not os.listdir(projects_dir):
        raise AgentRootError(
            f"{root!r} does not look like a knowledge checkout — no "
            f"non-empty 'projects' directory under it; refusing to "
            f"reconcile the roster against it")


def _read(path: str) -> dict | None:
    """One `projects/<crew>/agents/<name>.md` file as `{crew, name, doc_id}`,
    or `None` when its frontmatter `kind` is not `agent` — a path the glob
    matched that simply is not an agent definition, not a failure.

    Raises `AgentFileError` for a file this scan cannot read or whose
    frontmatter does not parse, so a file mid-edit never reads as its
    agent's definition having been removed.
    """
    crew = os.path.basename(os.path.dirname(os.path.dirname(path)))
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise AgentFileError(crew, name, f"{path} could not be read: {e}")
    m = _FRONTMATTER.match(text)
    if m is None:
        raise AgentFileError(
            crew, name, f"{path} has no YAML frontmatter block")
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise AgentFileError(
            crew, name, f"{path} frontmatter does not parse: {e}")
    if not isinstance(fm, dict) or fm.get("kind") != "agent":
        return None
    return {"crew": crew, "name": name, "doc_id": f"{crew}/agents/{name}"}


def load(root: str) -> tuple[list[dict], list[AgentFileError]]:
    """Every agent definition under `root`, as `(loaded, failed)`. Raises
    `AgentRootError` before reading any file when the root is not trusted —
    see `_check_root` — or when it holds no agent definition at all, the
    same shape `profile_sync.load` refuses an empty result for, since either
    would otherwise read as every name this reconcile ever added having lost
    its source in one scan.
    """
    _check_root(root)
    paths = agent_paths(root)
    if not paths:
        raise AgentRootError(
            f"found no agent definitions under {root!r} (looked for "
            f"projects/*/agents/*.md); refusing to reconcile, since that "
            f"would flag every name this reconcile has added as missing. "
            f"Check the path.")
    loaded: list[dict] = []
    failed: list[AgentFileError] = []
    for path in paths:
        try:
            doc = _read(path)
        except AgentFileError as e:
            failed.append(e)
            continue
        if doc is not None:
            loaded.append(doc)
    return loaded, failed


def missing_sources(existing: list[tuple[str, str]], loaded: list[dict],
                    failed: list[AgentFileError]) -> list[tuple[str, str]]:
    """The `(name, source_doc)` pairs among `existing` — every `roster_
    agents` row with a `source_doc` set — whose document is gone from the
    tree, sorted by name.

    A file that is present but failed to load is not gone, matching
    `profile_sync.stale`'s same reasoning for a profile: `failed` counts as
    present here too.
    """
    present = {d["doc_id"] for d in loaded}
    present |= {f"{e.crew}/agents/{e.name}" for e in failed}
    return sorted((name, source_doc) for name, source_doc in existing
                  if source_doc not in present)


def _refusal(doc: dict, reason: str) -> dict:
    return {"name": doc["name"], "doc_id": doc["doc_id"], "reason": reason}


def _existing_roster_names() -> set[str]:
    """Every name already on the roster, from one read-only connection."""
    conn = store.connect()
    try:
        return {r["name"] for r in conn.execute("SELECT name FROM roster_agents")}
    finally:
        conn.close()


def apply(ident: identity.Identity, loaded: list[dict]
         ) -> tuple[list[dict], list[dict]]:
    """Add every name `loaded` supplies that is not already on the roster.
    Returns `(added, refused)`.

    **Pre-filtered to new names before any write.** One `SELECT name FROM
    roster_agents` read before the loop skips a document whose name is
    already on the roster, so editing one existing definition opens no
    `connect_rw()` transaction at all; only a name this filter lets through
    reaches `roster_add_from_document`. The filter is a cost saving, not
    the guarantee — `roster_add_from_document`'s own `ON CONFLICT (name) DO
    NOTHING` and its slug/`withheld()` checks still run for every name that
    does reach it, exactly as before.

    **The crew-directory check is this module's own.** `crew_registry.
    roster_add_from_document` runs the same slug rule and `withheld()` check
    `PUT /roster/{name}` does, on the bare name; a name under a private or
    black-box crew's own directory is a fact only the PATH carries, which
    `roster_add_from_document`'s signature never sees. Checked once, against
    the whole set, before any write — not per-document — so an unreadable
    project registry refuses the entire batch rather than half of it
    (`crew_registry.RegistryUnreadable` propagates to the caller).
    """
    withholding = crew_registry.withholding_crews()
    existing = _existing_roster_names()
    added: list[dict] = []
    refused: list[dict] = []
    for doc in loaded:
        # The crew check runs before the pre-filter, so a withheld crew's
        # document is reported even when its name is already on the roster.
        if doc["crew"].casefold() in withholding:
            refused.append(_refusal(
                doc, f"{doc['crew']!r} is a private or black-box crew "
                     f"(platform/decisions/0043); its agents' names are "
                     f"withheld from the roster"))
            continue
        if doc["name"] in existing:
            continue
        try:
            _, inserted = crew_registry.roster_add_from_document(
                ident, doc["name"], doc["doc_id"])
        except (crew_registry.InvalidRosterRow,
               crew_registry.RosterNameWithheld) as e:
            refused.append(_refusal(doc, str(e)))
            continue
        if inserted:
            added.append(doc)
    return added, refused


def _flag_missing(name: str, source_doc: str) -> dict:
    """Log one signal for a roster name whose source document is gone from
    the tree. Idempotent across reconciles by `dedup_key` alone — a stable
    key per name, so a later scan that still finds the same file gone logs
    no second row, with no bookkeeping of "have I already flagged this" kept
    here."""
    crew = source_doc.split("/", 1)[0]
    row = {
        "id": f"sig_{store.new_ulid()}",
        "source": "library",
        "kind": "roster_agent_source_missing",
        "actor": AUTOMATIC_ACTOR,
        "ingested_by": AUTOMATIC_ACTOR,
        "acting_human": None,
        "product": crew,
        "stream": None,
        "body": (f"{name!r} is on the roster from {source_doc}, which is no "
                 f"longer on the served knowledge tree. The name is kept — "
                 f"removing it could strand a document that already names "
                 f"the agent — but its source document is gone."),
        "refs": json.dumps([source_doc]),
        "meta": json.dumps({"name": name, "source_doc": source_doc}),
        "privacy_class": "internal",
        "dedup_key": f"roster-agent-source-missing:{name}",
        "produced_at": None,
    }
    store.insert_signal(row, lambda _existing: True)
    return {"name": name, "source_doc": source_doc}


def source_missing(root: str | None, source_doc: str) -> bool:
    """Whether `source_doc`'s file is absent from `root`'s served knowledge
    tree, for `crew_profiles.roster()`'s `source_doc_missing` field.

    `False` when `root` is unset: "this cannot be told" reads as "not
    flagged" rather than every sourced row reading as missing, the same
    default `profile_sync` and `reference_admission` give an absent
    `KNOWLEDGE_ROOT`.
    """
    if not root:
        return False
    crew, _, rest = source_doc.partition("/")
    path = os.path.join(root, "projects", crew, *rest.split("/")) + ".md"
    return not os.path.isfile(path)


# --- the automatic reload ----------------------------------------------------

_UNATTEMPTED = object()
_lock = threading.Lock()
# The signature of the last attempt that did not fail transiently.
_attempted = _UNATTEMPTED

# After a transient failure, reads serve the roster as it stands until this
# many seconds have passed, rather than each reconciling again — matching
# `profile_sync.RETRY_BACKOFF_SECONDS`'s same reasoning.
RETRY_BACKOFF_SECONDS = 30
_clock = time.monotonic
_retry_at = None


def _signature(root: str) -> tuple:
    """`(path, mtime_ns, size, inode)` per agent definition file, sorted by
    path — the same shape `profile_sync._signature` builds for profiles."""
    sig = []
    for path in agent_paths(root):
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig.append((path, st.st_mtime_ns, st.st_size, st.st_ino))
    return tuple(sig)


def _existing_with_source(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["name"], r["source_doc"]) for r in conn.execute(
        "SELECT name, source_doc FROM roster_agents "
        "WHERE source_doc IS NOT NULL")]


def _reconcile(root: str) -> tuple[list[dict], list[dict], list[dict]]:
    """One reconcile of `root` into the roster. Returns `(added, refused,
    flagged)`."""
    loaded, failed = load(root)
    ident = identity.Identity(actor=AUTOMATIC_ACTOR, kind="orchestrator")
    added, refused = apply(ident, loaded)
    conn = store.connect()
    try:
        existing = _existing_with_source(conn)
    finally:
        conn.close()
    flagged = [_flag_missing(name, source_doc) for name, source_doc in
               missing_sources(existing, loaded, failed)]
    return added, refused, flagged


def _backing_off() -> bool:
    return _retry_at is not None and _clock() < _retry_at


def refresh() -> None:
    """Reconcile the roster against the served checkout if its agent
    definitions changed since the last attempt. Called by the read paths
    that consume the roster, after the caller has authenticated, excluding
    any path that holds a lease or a checkout.

    **What is recorded as attempted decides when the next read retries** —
    matching `profile_sync.refresh`'s own rule. A refusal (the root, the
    write gate) is deterministic, so the signature is recorded and the
    warning logged once per broken state; the next file change retries. A
    `sqlite3.Error`, `store.MisconfiguredDatabase` or `crew_registry.
    RegistryUnreadable` is transient, so the signature is not recorded and
    the next read retries after `RETRY_BACKOFF_SECONDS`.
    """
    global _attempted, _retry_at
    root = KNOWLEDGE_ROOT
    if not root or _backing_off() or _attempted == _signature(root):
        return
    with _lock:
        if _backing_off():
            return
        sig = _signature(root)
        if sig == _attempted:
            return
        try:
            added, refused, flagged = _reconcile(root)
        except AgentRootError as e:
            _logger.warning("agent roster reconcile skipped: %s", e)
        except store.WritesDisabled as e:
            _logger.warning(
                "agent roster reconcile skipped, writes are off: %s", e)
        except crew_registry.RegistryUnreadable as e:
            _retry_at = _clock() + RETRY_BACKOFF_SECONDS
            _logger.warning(
                "agent roster reconcile failed, retrying in %d s: %s",
                RETRY_BACKOFF_SECONDS, e)
            return
        except sqlite3.Error as e:
            _retry_at = _clock() + RETRY_BACKOFF_SECONDS
            _logger.warning(
                "agent roster reconcile failed, retrying in %d s: %s",
                RETRY_BACKOFF_SECONDS, e)
            return
        except store.MisconfiguredDatabase as e:
            _retry_at = _clock() + RETRY_BACKOFF_SECONDS
            _logger.warning(
                "agent roster reconcile failed, LIBRARY_DB is "
                "misconfigured, retrying in %d s: %s",
                RETRY_BACKOFF_SECONDS, e)
            return
        else:
            for ref in refused:
                _logger.warning("agent name not added to the roster: %s", ref)
            for flag in flagged:
                _logger.warning("agent roster name flagged: %s", flag)
            _logger.info(
                "agent roster reconcile: %d added, %d refused, %d flagged",
                len(added), len(refused), len(flagged))
        _attempted = sig
        _retry_at = None


def reset() -> None:
    """Forget the last attempt and any backoff, so the next read reconciles.
    Tests only, matching `profile_sync.reset`."""
    global _attempted, _retry_at
    _attempted = _UNATTEMPTED
    _retry_at = None
