"""The Library's read of the Keel's canonical project list (ARCH-426).

The registry is the `project_registry` table in the Chart House
(platform/decisions/0098's 2026-10-25 amendment: "an install has one crew
registry, held by the Library in its Chart House"), reconstructed into the
file's own document shape by `project_registry.registry_document`. This
reads it so a work item's `product` is VALIDATED rather than accepted as any
short string — the fault that let `Substrate` sit beside `substrate` in the
live store, one issue invisible to every view asking for `substrate`.

**`PROJECTS_JSON` names a file directly instead, for tests and a
fixture-only install** — the one step agent-platform's own
`lib/agent-platform/projects.py` shares, by the same variable name. There is
no other file fallback: a fallback to a file without the private crews —
agent-platform's old `config/projects.json`, the install repository's old
`<root>/install/projects.json`, or anything else that predates the Chart
House table — would let `private_repos()`, `private_products()` and
`private_crew_names()` read every private crew as public with no error, so
an empty table reads exactly as an unreadable registry does today: the
registry read returns None, and every reader that already fails closed on
None keeps doing so.

**The data is shared and the code is not, deliberately.** agent-platform ships a
loader for the same document shape, and importing it would reuse tested code
including its path-containment guard. Orient declined that import because it
runs in its own venv while agent-platform's library is deployed as a symlink
farm — a farm that has had dead links and once pointed at a feature-branch
worktree — so depending on it trades a data problem for an availability
problem. The Library is a third deployable with the same shape, so it makes
the same trade, and a drift test binds this reader to agent-platform's loader
the way Orient's does.

That the Chart House is now the registry, and Orient still reads its own copy
of the file, is worth noticing rather than settling here. Once Orient is a
client of the Library's storage instead of a second reader, the two cannot
disagree during a transition the way they could while both read a file.
Filed rather than decided (`issue_01KZKR3PDY8K4VA157KZ1GM0F8`).

**FAIL-OPEN, DELIBERATELY.** An unreadable registry makes `resolve()` return its
input unchanged, so the caller keeps the old accept-anything behaviour. That is
the uncomfortable choice and the right one: an unreadable registry is an
operational fault, a service that cannot accept a filing is a work stoppage, and
a wrongly-scoped item is a row someone can fix. The failure is logged, never
silent. `None` therefore means "the registry was read and does not know this",
which is the only case a caller should refuse on.

**IN TABLE MODE, `private_repos()`, `private_products()` AND
`private_crew_names()` SHARE THE SAME THROTTLED FRESHNESS CHECK `resolve()`
DOES.** `POST /registry/crews` calls `reload()` right after its own commit,
so a crew it registers is visible to these three readers on the very next
call, with no wait. A write from another process — `load_project_registry.py`
running against the live vault — has no such signal: these readers see it
within `FRESHNESS_SECONDS` of wall-clock time, the bound
`_throttled_table_signature` enforces, not immediately.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

# CQ·243, platform/decisions/0098's 2026-10-25 amendment: the Chart House is
# the registry and no file is. REGISTRY_PATH now resolves as:
#
# 1. `PROJECTS_JSON`, when set and non-empty, names the file directly (tests
#    and a fixture-only install).
# 2. Else `None`, meaning "read `project_registry` through `store`" — see
#    `_read_table_registry` and `_table_signature` below, which `store` and
#    `project_registry` are imported into lazily rather than at module level:
#    `store.py` imports this module at its own top level (to resolve a
#    product string against the registry), so a module-level import here
#    the other way would make the two modules' load order decide whether
#    `store`'s own names exist yet when this module's lazy readers first
#    reach for them.
_PROJECTS_JSON_ENV = os.environ.get("PROJECTS_JSON", "")
REGISTRY_PATH: Path | None = Path(_PROJECTS_JSON_ENV) if _PROJECTS_JSON_ENV else None

# CQ·243 follow-up: in table mode, `_table_signature()` opens a vault
# connection and runs a query, where file mode's `_signature()` only ever
# cost a cached `stat()`. `store._pr_repo_pattern` calls `projects.projects()`
# and `projects.repo_owner()` for every row `store.item_dict` serves, so a
# 200-row page opened about 400 such connections, most of them on the event
# loop. `_throttled_table_signature()` below bounds that to at most one
# query per `FRESHNESS_SECONDS` of wall-clock time per process; file mode is
# unthrottled and unchanged, since `PROJECTS_JSON` is for tests and a
# fixture-only install, not a code path hit hundreds of times per request.
FRESHNESS_SECONDS = 1.0

# Registry keys starting with this are metadata rather than projects — `_scopes`
# is the one that matters, and `_meta` exists too. Stripping them is what makes
# `available()` mean "there are projects" rather than "the file parsed".
_META_PREFIX = "_"

_logger = logging.getLogger(__name__)

# A sentinel for "no read has been attempted yet", distinct from both `None`
# (the signature of a registry that could not be stat'd or a table that is
# empty or unreadable) and any real signature — so the very first call
# always attempts a read rather than matching a signature by accident.
_UNATTEMPTED = object()


def _signature(path: Path | None):
    """`(st_mtime_ns, st_size, st_ino)` for `path`, or `None` when it cannot
    be stat'd. The inode is load-bearing: agent-platform#757 registers a crew
    by writing a temp file in the registry's directory and renaming it over
    the target, and that rename can leave size identical and, within mtime's
    resolution, mtime identical too — only the inode reliably moves.

    `path is None` means table mode (`REGISTRY_PATH` unset): the signature is
    then `_throttled_table_signature()`, the table-mode analogue keyed on
    `project_registry`'s own `updated_at` rather than a file stat, so a crew
    registered through `POST /registry/crews` is picked up here exactly as
    an edited file is above — throttled to at most one query per
    `FRESHNESS_SECONDS`, see that function's docstring for why."""
    if path is None:
        return _throttled_table_signature()
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _table_signature() -> str | None:
    """The latest `updated_at` in `project_registry`, or `None` when the
    table is empty or the database cannot be read. Opens a vault connection
    and runs one query — call it through `_throttled_table_signature()`
    rather than directly outside a test, so this cost is paid at most once
    per `FRESHNESS_SECONDS` rather than on every `_snapshot()` call; see that
    function's docstring for the per-row call this throttle exists for.

    `store.connect()` can fail with `OSError` (the file is missing),
    `sqlite3.Error` (`sqlite3.connect()` itself can raise, e.g. a
    permission problem or a TOCTOU race after the existence check — it is
    not only the statements run on the handle that can), or
    `store.MisconfiguredDatabase`; all three read as "no signature" here,
    matching `_read_table_registry`'s own catch below."""
    import store
    try:
        conn = store.connect()
    except (OSError, sqlite3.Error, store.MisconfiguredDatabase):
        return None
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) AS latest FROM project_registry"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row["latest"]


# `_UNATTEMPTED` here means "no throttled check has run yet in this
# process", distinct from any real `time.monotonic()` reading — the same
# role `_UNATTEMPTED` plays for `_attempted_signature` above, and the reason
# the very first call always queries rather than matching a default by
# accident.
_last_table_check: float | object = _UNATTEMPTED
_last_table_signature: str | None = None


def _throttled_table_signature() -> str | None:
    """`_table_signature()`, run at most once per `FRESHNESS_SECONDS` of
    wall-clock time per process. Between queries, the last signature this
    process read is returned with no connection opened.

    **Exists because `store._pr_repo_pattern` calls `projects.projects()`
    and `projects.repo_owner()` for every row an item-list route serves**:
    before CQ·243 moved the registry into the Chart House, that per-row call
    cost a cached file `stat()`; after, each one opened a vault connection
    and ran a query, so a 200-row page opened about 400 of them. This
    throttle bounds the per-process cost back down to roughly one query per
    `FRESHNESS_SECONDS`, no matter how many reads land on the process
    meanwhile.

    **Read and written with no lock, matching `_snapshot`'s own no-lock
    common path.** The monotonic timestamp and the cached signature are
    plain module globals: CPython's GIL makes one global's assignment
    atomic, so a reader racing the write below sees either the pair from
    before this call or the pair from after it, never a torn float — the
    worst a race costs is an extra query from a second thread that read the
    old timestamp a moment too soon, which the module accepts as the price
    of taking no lock here (two threads crossing the interval at once may
    both query). Nothing here can skip a check forever: the signature is
    written before the timestamp that gates on it, so a reader that sees
    the new timestamp also sees the signature it belongs with, and a reader
    that still sees the old timestamp simply re-queries.
    """
    global _last_table_check, _last_table_signature
    now = time.monotonic()
    last_check = _last_table_check
    if last_check is not _UNATTEMPTED and now - last_check < FRESHNESS_SECONDS:
        return _last_table_signature
    sig = _table_signature()
    _last_table_signature = sig
    _last_table_check = now
    return sig


class _Snapshot:
    """One read of the registry, plus every view derived from it.

    The views below are `functools.cached_property`, each computed lazily
    from `raw` alone and never from a module global, so one instance can
    never mix data from two registry versions — the fix `reload()` already
    required of itself (see its docstring) now holds by construction instead
    of by every view remembering to clear together.
    """

    def __init__(self, raw: dict):
        self.raw = raw

    @functools.cached_property
    def projects(self) -> dict:
        return {k: v for k, v in self.raw.items() if not k.startswith(_META_PREFIX)}

    @functools.cached_property
    def available(self) -> bool:
        """Whether this snapshot yielded USABLE projects. False means
        validation is disabled and every product string is accepted.

        **Keyed on `projects` rather than the raw read, and that is the
        difference between fail-open and fail-shut** (reviewer on Orient's PR
        #143; this port dropped the fix and a reviewer caught it here too). A
        file holding only metadata — `{"_meta": {...}}` — is valid JSON and
        non-empty, so a raw check calls it available. It has no projects, so
        every product resolves to None and EVERY FILING IS REFUSED. That is
        the dangerous middle between working and clearly broken: the registry
        looks healthy and silently refuses all work, which is worse than an
        unreadable file because nothing says anything is wrong.
        """
        return bool(self.projects)

    @functools.cached_property
    def resolution_map(self) -> dict[str, str]:
        """Every string that names a project, lowercased, to its canonical
        slug.

        Four kinds of thing resolve, and each earns its place:

        - the SLUG, so a correct value stays correct.
        - an ALIAS, because that is the registry's facility for change: a
          rename is a registry edit rather than a data migration.
        - a REPO name, because a repo is a service to a project and filers
          name the repo they were working in.
        - the NON-PROJECT SCOPES, which are not projects at all. ARCH-426
          makes the vocabulary uniform across all three vaults, so an item
          may be scoped `platform` exactly as a signal may be, and resolving
          against projects alone would refuse a legal value.

        Lowercased keys are the fix for the observed bug: `Substrate`
        resolves to `substrate` rather than creating a second scope nobody
        can see.

        Each nested read is type-checked rather than trusted. Orient's first
        version raised on a null project and on a null `aliases`, so its
        fail-open contract held for a MISSING registry and silently did not
        hold for a partially valid one — the likelier fault of the two.
        """
        out: dict[str, str] = {}
        # Scopes first, because `setdefault` gives the key to the first
        # writer and a scope must not be shadowed. They cannot collide today;
        # if one ever does the registry is malformed rather than ambiguous.
        scopes = self.raw.get("_scopes")
        if isinstance(scopes, dict):
            for slug in scopes:
                if isinstance(slug, str) and slug.strip() and not slug.startswith("_"):
                    out.setdefault(slug.strip().lower(), slug)
        for slug, meta in self.raw.items():
            if not isinstance(slug, str) or slug.startswith("_") or not slug.strip():
                continue
            names = [slug]
            if isinstance(meta, dict):
                for field in ("aliases", "repos"):
                    value = meta.get(field)
                    if isinstance(value, (list, tuple)):
                        names.extend(value)
            for name in names:
                if isinstance(name, str) and name.strip():
                    out.setdefault(name.strip().lower(), slug)
        return out

    @functools.cached_property
    def private_repos(self) -> frozenset[str] | None:
        """Every repo belonging to a project the registry marks
        `private: true`, lowercased. `None` when the registry could not be
        read.

        **`None` IS NOT AN EMPTY SET, AND A CALLER MUST NOT TREAT IT AS ONE.**
        Everything else in this module fails OPEN — an unreadable registry
        makes `resolve()` return its input unchanged, because a service that
        cannot accept a filing is a work stoppage while a wrongly-scoped row
        is something a person can fix. **That trade inverts here.** The
        question this answers is "may I show this to somebody", and the cost
        of guessing wrong is a black-box crew's live task text and worktree
        path reaching a client — which ADR-0043 exists to prevent and which
        no later edit can un-disclose.

        So the two cases are returned distinguishably rather than collapsed,
        and a caller that cannot tell "nothing is private" from "I could not
        find out" would build exactly the disclosure this is here to stop.

        ADR-0043: a black-box crew is "registered so the platform knows it
        exists, with none of its content reaching the Library and none of its
        agents visible to Orient." **Its agents, not merely its task text** —
        so a consumer excludes the row rather than redacting a field. A
        private crew's entry also carries `task_text_public: false`, which
        agrees rather than softening it.
        """
        if not self.raw:
            return None
        out = set()
        for name, entry in self.raw.items():
            if name.startswith("_") or not isinstance(entry, dict):
                continue
            if not entry.get("private"):
                continue
            # The project's own name counts as well as its repos: a run may
            # report either, and the registry spells a private crew's project
            # name and its repo name differently (one lowercase, the other
            # not).
            out.add(name.strip().lower())
            for repo in entry.get("repos") or []:
                if isinstance(repo, str) and repo.strip():
                    out.add(repo.strip().lower())
        return frozenset(out)

    @functools.cached_property
    def private_products(self) -> frozenset[str] | None:
        """The canonical slug of every project the registry marks `private`
        (the same truthy test `private_repos` uses), lowercased exactly as
        `resolve()` would resolve it. `None` when the registry could not be
        read.

        **`private_repos`'s counterpart for a signal's `product` field**,
        which names a product slug rather than a repo. Fails CLOSED on the
        same reasoning `private_repos` documents in full: `None` is
        distinguishable from "no private products today" so a caller asking
        "may I show this to somebody" cannot mistake an unreadable registry
        for a clean answer.
        """
        if not self.raw:
            return None
        out = set()
        for slug, entry in self.raw.items():
            if slug.startswith("_") or not isinstance(entry, dict):
                continue
            if not entry.get("private"):
                continue
            resolved = self.resolution_map.get(slug.strip().lower())
            if resolved:
                out.add(resolved.strip().lower())
        return frozenset(out)

    @functools.cached_property
    def created_by(self) -> dict[str, list[str]] | None:
        """{person id: sorted project slugs}, grouping every registered
        project by its registry entry's `created_by` field — the per-person
        visibility default (issue_01M2RSFKJBEHD5Y76X0B6XAYZG).

        `None` when this snapshot has no USABLE projects, keyed on
        `available` rather than on `self.raw` being falsy. The dangerous
        middle `available`'s own docstring describes applies here exactly
        as it does to validation: a registry holding only `{"_meta": {...}}`
        must not read as "nobody created anything" any more than it should
        resolve every product — both would silently serve a default
        computed from no registry at all.
        """
        if not self.available:
            return None
        out: dict[str, list[str]] = {}
        for slug, entry in self.projects.items():
            if not isinstance(entry, dict):
                continue
            creator = entry.get("created_by")
            if isinstance(creator, str) and creator:
                out.setdefault(creator, []).append(slug)
        for slugs in out.values():
            slugs.sort()
        return out

    @functools.cached_property
    def private_crew_names(self) -> frozenset[str] | None:
        """Every casefolded name a private crew (`private` exactly `True`)
        answers to: the slug, `display`, `crew`, every entry of `aliases` and
        `repos`, and the basename of `home_dir` and of every entry of `dirs`.
        `None` when the registry could not be read, distinguishably from "no
        private crews today" for the same reason `private_repos` returns
        `None` rather than an empty set — see that property's docstring.

        **Wider than `private_repos`, which collects only a private
        project's slug and its `repos`.** A caller refusing a private crew's
        name has to refuse every name that crew answers to, not just the two
        the registry happens to key it by — agent-platform's own
        `_private_crew_names()` collects the same set for the same reason
        (`issue_01M3NS89KVRXB368VA4TJ38MDG`, reviewer finding on PR #160: an
        alias admitted here while agent-platform's validator still refuses it
        is exactly the disagreement porting this set exists to close).

        A `private` value that is truthy but not exactly `True` (the string
        `"true"`, `1`) does not count — matching agent-platform#685, which
        drew the same line.
        """
        if not self.raw:
            return None
        names: set[str] = set()
        for slug, entry in self.raw.items():
            if slug.startswith("_") or not isinstance(entry, dict):
                continue
            if entry.get("private") is not True:
                continue
            collected = [slug, entry.get("display"), entry.get("crew"),
                         *(entry.get("aliases") or []), *(entry.get("repos") or [])]
            for p in [entry.get("home_dir"), *(entry.get("dirs") or [])]:
                if isinstance(p, str) and p.strip():
                    collected.append(Path(p).name)
            names.update(n.strip().casefold() for n in collected
                         if isinstance(n, str) and n.strip())
        return frozenset(names)

    @functools.cached_property
    def kb_slugs(self) -> frozenset[str] | None:
        """`kb_project` for every unit whose `kb` is `central` or `split`,
        falling back to the unit's own slug — platform/decisions/0097 point
        1's first piece, mirroring agent-platform's `lib/agent-platform/
        projects.py` function of the same name. `None` when the registry
        could not be read, distinguishably from "no central or split units
        today" for the same reason `private_crew_names` returns `None`
        rather than an empty set.

        Never the registry's top-level keys: a `kb: local` unit (the
        private crews, and any other unit the registry marks local) never
        enters this set, and a unit's slug and its `kb_project` can differ
        (`bosun` writes to `agent-platform`, `steward` to `platform`)."""
        if not self.raw:
            return None
        return frozenset(
            v.get("kb_project", k) for k, v in self.projects.items()
            if isinstance(v, dict) and v.get("kb") in ("central", "split"))

    @functools.cached_property
    def scopes(self) -> frozenset[str] | None:
        """The non-project scope slugs the registry's `_scopes` key names
        (`company`, `platform`, `commercial`, `engineering-practice`, ARCH-
        426) — platform/decisions/0097 point 1's second piece, mirroring
        agent-platform's `scopes()`. Nothing is built at these, so they have
        no home directory and no crew. `None` when the registry could not
        be read."""
        if not self.raw:
            return None
        raw_scopes = self.raw.get("_scopes")
        if not isinstance(raw_scopes, dict):
            return frozenset()
        return frozenset(s for s in raw_scopes
                          if isinstance(s, str) and not s.startswith(_META_PREFIX))

    @functools.cached_property
    def kb_legacy(self) -> frozenset[str] | None:
        """The registry's `_kb_legacy` list — platform/decisions/0097 point
        1's third piece: KB project slugs that predate the registry and name
        no unit's own `kb_project` today (`substrate` and `knowledge` as of
        this writing). A slug leaves this list when its documents are
        re-filed under a unit that names it. `None` when the registry could
        not be read."""
        if not self.raw:
            return None
        raw_legacy = self.raw.get("_kb_legacy")
        if not isinstance(raw_legacy, list):
            return frozenset()
        return frozenset(s for s in raw_legacy if isinstance(s, str))


_lock = threading.Lock()
_current: _Snapshot | None = None
_attempted_signature = _UNATTEMPTED
_had_good_snapshot = False


def _read_registry() -> dict | None:
    """Open and parse `REGISTRY_PATH`, or read `project_registry` through
    `store` when `REGISTRY_PATH` is `None`. `None` on any failure the module
    treats as "registry unusable" (missing, unparsable, or an empty table) —
    the stderr message matches the process's first-ever failed read; a
    failure after a snapshot has already served is logged instead, once per
    failed attempt, and does not print to stderr (see `_snapshot`)."""
    if REGISTRY_PATH is None:
        return _read_table_registry()
    try:
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        if not _had_good_snapshot:
            print(f"library.projects: {REGISTRY_PATH} not found — product validation "
                  f"is disabled and every product string will be accepted",
                  file=sys.stderr)
        else:
            _logger.warning(
                "library.projects: %s not found on re-read — keeping the last "
                "good registry rather than disabling validation under a "
                "running service", REGISTRY_PATH)
        return None
    except (OSError, json.JSONDecodeError) as e:
        if not _had_good_snapshot:
            print(f"library.projects: cannot read {REGISTRY_PATH} "
                  f"({type(e).__name__}: {e}) — product validation is disabled",
                  file=sys.stderr)
        else:
            _logger.warning(
                "library.projects: cannot re-read %s (%s: %s) — keeping the "
                "last good registry rather than disabling validation under a "
                "running service", REGISTRY_PATH, type(e).__name__, e)
        return None


def _report_table_unreadable(reason: str) -> None:
    """The table-mode analogue of `_read_registry`'s stderr/warning split
    above: a first-ever failure prints to stderr, a failure after a snapshot
    has already served only logs, once per failed attempt."""
    if not _had_good_snapshot:
        print(f"library.projects: {reason} — product validation is "
              f"disabled and every product string will be accepted",
              file=sys.stderr)
    else:
        _logger.warning(
            "library.projects: %s on re-read — keeping the last good "
            "registry rather than disabling validation under a running "
            "service", reason)


def _read_table_registry() -> dict | None:
    """The whole registry document, read from `project_registry` through
    `project_registry.registry_document`. `None` when the vault database
    cannot be opened, the table cannot be read, or the table holds no rows —
    the last of those is this module's "nobody has loaded the registry yet"
    case, reading exactly as an unreadable file does today (see the module
    docstring).

    `store.connect()` can fail with `OSError`, `sqlite3.Error` (not only
    `FileNotFoundError` — `sqlite3.connect()` itself can raise, e.g. a
    permission problem or a TOCTOU race after the existence check), or
    `store.MisconfiguredDatabase`. `project_registry.registry_document`
    can fail with `sqlite3.Error` reading the table, or `json.JSONDecodeError`
    / `TypeError` from `json.loads` on a corrupt or null `entry` column.
    Every one of those reads as "table unreadable" here, matching
    `_read_registry`'s `(OSError, json.JSONDecodeError)` pairing for file
    mode."""
    import project_registry
    import store
    try:
        conn = store.connect()
    except (OSError, sqlite3.Error, store.MisconfiguredDatabase) as e:
        _report_table_unreadable(
            f"cannot open the vault database ({type(e).__name__}: {e})")
        return None
    try:
        document = project_registry.registry_document(conn)
    except (sqlite3.Error, json.JSONDecodeError, TypeError) as e:
        _report_table_unreadable(
            f"cannot read the project_registry table ({type(e).__name__}: {e})")
        return None
    finally:
        conn.close()
    if document is None:
        _report_table_unreadable("the project_registry table is empty")
    return document


def _snapshot() -> _Snapshot:
    """The current snapshot, re-reading the registry when its signature has
    moved since the last attempt (see `_signature`): a file's
    `(mtime_ns, size, ino)` in file mode, or `project_registry`'s own
    `MAX(updated_at)` in table mode — the latter throttled to at most one
    query per `FRESHNESS_SECONDS` (`_throttled_table_signature`). The
    throttle exists because this function runs on every call to
    `projects.projects()` and `projects.repo_owner()`, which
    `store._pr_repo_pattern` calls for every row `store.item_dict` serves on
    an item-list route; a vault connection on every one of those calls is a
    cost file mode's cached `stat()` never had.

    **The common path takes no lock**: one cheap signature check — a
    `stat()` in file mode, a throttled check in table mode — and a
    comparison, safe to run from every thread on every request. The lock is
    only held around an actual re-read (the full open-and-parse or table-dump, and —
    on success — the swap to a new `_Snapshot`), never across the comparison
    every reader does, so concurrent requests never serialize on a registry
    that is not changing. Readers that do race a re-read either match the
    pre-swap signature and return the old, fully-formed snapshot, or they
    block on the lock and, once it is free, see the swap already done —
    never a partially-applied one, because the swap is one assignment of one
    immutable object.
    """
    global _current, _attempted_signature, _had_good_snapshot
    sig = _signature(REGISTRY_PATH)
    current = _current
    if current is not None and sig == _attempted_signature:
        return current
    with _lock:
        sig = _signature(REGISTRY_PATH)
        if _current is not None and sig == _attempted_signature:
            return _current
        raw = _read_registry()
        if raw is not None:
            _current = _Snapshot(raw)
            _had_good_snapshot = True
        elif _current is None:
            _current = _Snapshot({})
        # else: a re-read failed after a snapshot had already served — keep
        # serving that last good snapshot; `_read_registry` already logged.
        _attempted_signature = sig
        return _current


def reload() -> None:
    """Force the next read to re-fetch the registry (the file at
    `REGISTRY_PATH`, or `project_registry` in table mode), discarding the
    current snapshot and the process's memory of ever having read a good
    one.

    That second part matters for the same reason the whole-snapshot swap
    does: a caller repointing `REGISTRY_PATH` (every file-mode test in this
    suite) is starting over, not continuing a running service's history, so
    a registry that cannot be read right after `reload()` gets the same
    first-read stderr message a fresh process would print —
    not the quieter "keeping the last good snapshot" a long-lived process
    takes for a registry that was already serving.

    Every view this module derives from the registry lives on one
    `_Snapshot`, built fresh on the next read; dropping `_current` drops all
    of them together. A new view added to `_Snapshot` later needs no update
    here — it is computed from `raw` the same way the others are.

    Also clears the table-mode freshness throttle, so the very next read
    runs `_table_signature()` rather than returning a signature cached
    before this call — the property `test_a_registered_private_crew_
    resolves_without_restart_or_reload` depends on: `POST /registry/crews`
    calls this after it commits, precisely so the throttle never hides that
    write from the request that follows it.
    """
    global _current, _attempted_signature, _had_good_snapshot
    global _last_table_check, _last_table_signature
    with _lock:
        _current = None
        _attempted_signature = _UNATTEMPTED
        _had_good_snapshot = False
        _last_table_check = _UNATTEMPTED
        _last_table_signature = None


def projects() -> dict[str, dict]:
    """{slug: project}, with metadata keys stripped."""
    return _snapshot().projects


def repo_owner(project: dict, repo: str) -> str | None:
    """The GitHub owner of `repo` within one project entry: the entry's
    `repo_owners[repo]` override, else its `github_owner`, else its `owner`
    (`REGISTRY_PATH`'s own field doc) — agent-platform's `projects.repo_github_slug`
    resolves the same three fields in the same order. shipwright's
    `homebrew-tap` lives under `convivybuilder` while the project's own
    `owner` is `convivy`, and a unit whose repositories live under a person's
    account answers to whichever `github_owner` its entry names rather than
    the `owner` metadata field.

    The single resolver `ci_tracked.deck_tracked_repos` and `store._pr_repo`
    both call, so the two agree on one project's repo rather than each
    reading the registry its own way.
    """
    for candidate in ((project.get("repo_owners") or {}).get(repo),
                      project.get("github_owner"), project.get("owner")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def available() -> bool:
    """Whether the registry yielded USABLE projects. False means validation is
    disabled and every product string is accepted."""
    return _snapshot().available


def resolve(product: str | None) -> str | None:
    """The canonical slug for a product string, or None if unrecognised.

    Returns the input UNCHANGED when the registry is unreadable — see the
    module's fail-open note. `None` means the registry was read and does not know
    this name.
    """
    if product is None:
        return None
    snap = _snapshot()
    if not snap.available:
        return product
    return snap.resolution_map.get(product.strip().lower())


def known_names() -> list[str]:
    """Every accepted name, sorted, so a refusal can say what the caller MAY say
    rather than only that it was wrong."""
    return sorted(_snapshot().resolution_map)


def private_repos() -> frozenset[str] | None:
    """Every repo belonging to a project the registry marks `private: true`,
    lowercased. `None` when the registry could not be read — see
    `_Snapshot.private_repos`."""
    return _snapshot().private_repos


def private_products() -> frozenset[str] | None:
    """The canonical slug of every project the registry marks `private`.
    `None` when the registry could not be read — see
    `_Snapshot.private_products`."""
    return _snapshot().private_products


def private_crew_names() -> frozenset[str] | None:
    """Every casefolded name a private crew answers to. `None` when the
    registry could not be read — see `_Snapshot.private_crew_names`."""
    return _snapshot().private_crew_names


def kb_slugs() -> frozenset[str] | None:
    """`kb_project` for every unit with a central or split KB slice. `None`
    when the registry could not be read — see `_Snapshot.kb_slugs`."""
    return _snapshot().kb_slugs


def scopes() -> frozenset[str] | None:
    """The non-project scope slugs (`_scopes`, ARCH-426). `None` when the
    registry could not be read — see `_Snapshot.scopes`."""
    return _snapshot().scopes


def kb_legacy() -> frozenset[str] | None:
    """The registry's `_kb_legacy` list. `None` when the registry could not
    be read — see `_Snapshot.kb_legacy`."""
    return _snapshot().kb_legacy


def slugs_created_by(person_id: str) -> list[str] | None:
    """Sorted project slugs whose registry entry names `person_id` as
    `created_by` — a person's default project set
    (issue_01M2RSFKJBEHD5Y76X0B6XAYZG). `None` when the registry has no
    usable projects, distinguishable from a person who created none: a
    caller computing a default must refuse rather than serve a set derived
    from no registry at all — see `_Snapshot.created_by`."""
    created = _snapshot().created_by
    return None if created is None else created.get(person_id, [])
