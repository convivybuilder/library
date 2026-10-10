"""The Library's read of the Keel's canonical project list (ARCH-426).

The registry is `agent-platform/config/projects.json`, and it is the single
record of what projects exist. This reads it so a work item's `product` is
VALIDATED rather than accepted as any short string — the fault that let
`Substrate` sit beside `substrate` in the live store, one issue invisible to
every view asking for `substrate`.

**The data is shared and the code is not, deliberately.** agent-platform ships a
loader for the same file, and importing it would reuse tested code including its
path-containment guard. Orient declined that import because it runs in its own
venv while agent-platform's library is deployed as a symlink farm — a farm that
has had dead links and once pointed at a feature-branch worktree — so depending
on it trades a data problem for an availability problem. The Library is a third
deployable with the same shape, so it makes the same trade, and a drift test
binds this reader to agent-platform's loader the way Orient's does.

That this is now the Library's OWN config, read from a file another repo owns, is
worth noticing rather than settling here. Once the Library is the platform's
storage service, serving the registry is a plausible Library route and Orient
becomes a client of that instead of a second reader. Filed rather than decided
(`issue_01KZKR3PDY8K4VA157KZ1GM0F8`); reading the file is what Orient does today
and matching it is what keeps the two from disagreeing during the shadow.

**FAIL-OPEN, DELIBERATELY.** An unreadable registry makes `resolve()` return its
input unchanged, so the caller keeps the old accept-anything behaviour. That is
the uncomfortable choice and the right one: an unreadable registry is an
operational fault, a service that cannot accept a filing is a work stoppage, and
a wrongly-scoped item is a row someone can fix. The failure is logged, never
silent. `None` therefore means "the registry was read and does not know this",
which is the only case a caller should refuse on.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import sys
import threading
from pathlib import Path

import registry_root

# steward, 2026-10-02: the Library and agent-platform could read different
# registry files on any install whose root is not `$HOME/Convivy`, because
# each named the file its own way. REGISTRY_PATH now resolves as:
#
# 1. `PROJECTS_JSON`, when set and non-empty, names the file directly — the
#    one step agent-platform's own `lib/agent-platform/projects.py` shares,
#    by the same variable name.
# 2. Else `AGENT_PLATFORM_PROJECTS`, when set and non-empty — a deprecated,
#    Library-local alias (predating this change; kept so nothing that sets
#    it today breaks), with no counterpart in agent-platform's loader.
# 3. Else `<root>/agent-platform/config/projects.json`, where `<root>` is
#    `registry_root.root()`, the Library's own copy of the same `CREWS_ROOT`
#    resolution agent-platform's `builder_root.root()` does. Agent-platform's
#    loader checks its own tree for a `config/projects.json` ahead of this
#    step; that step has no meaning for the Library, a separate repository
#    with no such tree of agent-platform's to check, so it has no
#    counterpart here either.
_PROJECTS_JSON_ENV = os.environ.get("PROJECTS_JSON", "")
_AGENT_PLATFORM_PROJECTS_ENV = os.environ.get("AGENT_PLATFORM_PROJECTS", "")
if _PROJECTS_JSON_ENV:
    REGISTRY_PATH = Path(_PROJECTS_JSON_ENV)
elif _AGENT_PLATFORM_PROJECTS_ENV:
    REGISTRY_PATH = Path(_AGENT_PLATFORM_PROJECTS_ENV)
else:
    REGISTRY_PATH = registry_root.root() / "agent-platform" / "config" / "projects.json"

# Registry keys starting with this are metadata rather than projects — `_scopes`
# is the one that matters, and `_meta` exists too. Stripping them is what makes
# `available()` mean "there are projects" rather than "the file parsed".
_META_PREFIX = "_"

_logger = logging.getLogger(__name__)

# A sentinel for "no read has been attempted yet", distinct from both `None`
# (the signature of a registry that could not be stat'd) and any real
# `(mtime_ns, size, ino)` tuple — so the very first call always attempts a
# read rather than matching a signature by accident.
_UNATTEMPTED = object()


def _signature(path: Path):
    """`(st_mtime_ns, st_size, st_ino)`, or `None` when `path` cannot be
    stat'd. The inode is load-bearing: agent-platform#757 registers a crew by
    writing a temp file in the registry's directory and renaming it over the
    target, and that rename can leave size identical and, within mtime's
    resolution, mtime identical too — only the inode reliably moves."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


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
        so a consumer excludes the row rather than redacting a field. `cfo`
        also carries `task_text_public: false`, which agrees rather than
        softening it.
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
            # report either, and the registry spells them differently (`cfo`
            # the project, `CFO` the repo).
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


_lock = threading.Lock()
_current: _Snapshot | None = None
_attempted_signature = _UNATTEMPTED
_had_good_snapshot = False


def _read_registry() -> dict | None:
    """Open and parse `REGISTRY_PATH`. `None` on any failure the module
    treats as "registry unusable" (missing or unparsable) — the stderr
    message matches the process's first-ever failed read; a failure after a
    snapshot has already served is logged instead, once per failed attempt,
    and does not print to stderr (see `_snapshot`)."""
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


def _snapshot() -> _Snapshot:
    """The current snapshot, re-reading the registry when its stat signature
    has moved since the last attempt (see `_signature`).

    **The common path takes no lock**: a `stat()` and a tuple comparison,
    cheap and safe to run from every thread on every request. The lock is
    only held around an actual re-read (open, parse, and — on success — the
    swap to a new `_Snapshot`), never across the comparison every reader
    does, so concurrent requests never serialize on a registry that is not
    changing. Readers that do race a re-read either match the pre-swap
    signature and return the old, fully-formed snapshot, or they block on the
    lock and, once it is free, see the swap already done — never a
    partially-applied one, because the swap is one assignment of one
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
    """Force the next read to re-open and re-parse `REGISTRY_PATH`, discarding
    the current snapshot and the process's memory of ever having read a good
    one.

    That second part matters for the same reason the whole-snapshot swap
    does: a caller repointing `REGISTRY_PATH` (every test in this suite that
    exercises the registry) is starting over, not continuing a running
    service's history, so a registry that cannot be read right after `reload()`
    gets the same first-read stderr message a fresh process would print —
    not the quieter "keeping the last good snapshot" a long-lived process
    takes for a registry that was already serving.

    Every view this module derives from the registry lives on one
    `_Snapshot`, built fresh on the next read; dropping `_current` drops all
    of them together. A new view added to `_Snapshot` later needs no update
    here — it is computed from `raw` the same way the others are.
    """
    global _current, _attempted_signature, _had_good_snapshot
    with _lock:
        _current = None
        _attempted_signature = _UNATTEMPTED
        _had_good_snapshot = False


def projects() -> dict[str, dict]:
    """{slug: project}, with metadata keys stripped."""
    return _snapshot().projects


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
