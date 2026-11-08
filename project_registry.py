"""The `project_registry` table in the Chart House (CQ·241, platform/
decisions/0098's 2026-10-25 amendment: "an install has one crew registry,
held by the Library in its Chart House"). Write and read both live here,
the same single-module shape `machine_registry.py` takes for `machine_
certificates` — unlike `crew_registry.py`/`crew_profiles.py`'s split,
which exists because an HTTP write route (`roster.write`) needed its
capability check kept out of a module a read-only caller would otherwise
have to pull in. `load_project_registry.py` is `replace()`'s one caller
and runs as a trusted local operator, so `replace()` itself checks no
capability; `register_crew()` below is the second writer CQ·241's module
docstring named as coming later, and it is reachable over HTTP, so its
capability check (`registry.write`) lives here rather than in `app.py` —
the same reasoning `crew_registry.py`'s own module docstring gives for
`roster.write`: every mutating function checks it first, so a second route
cannot skip it by not knowing it exists.

**`GET /registry` is the one read path**, gated on `registry.read` at the
route (`identity.py`) rather than here — matching `crew_profiles.roster`'s
read functions, which carry no capability check of their own either.

**Agent-platform's own file-based reader (`projects.py`) keeps reading its
file in this item.** This module builds the Chart House's copy and serves
it; nothing yet reads this table in place of that file.

**`register_crew()`'s schema checks are a deliberate port of agent-platform's
`lib/agent-platform/crew_registration.py`** (`build_row`, the private-crew
check it delegates to `projects.privacy_invariant_errors`, `_kb_project_
syntax_errors`, and `register`'s `claimed_names` collision check) rather than
an import, for the same reason `registry_root.py` is a copy of agent-
platform's `builder_root.root()` rather than a dependency on it: the Library
must not depend on agent-platform's code at runtime. A change to one has to
be made to the other by hand; `tests/test_registry_crews_drift.py` pins a
pinned fixture copy of that file's source against this module's own
constants and logic so the two cannot drift apart silently.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

import identity
import store


class InvalidRegistryDocument(ValueError):
    """`document` is not a JSON object, or one of its project entries (a
    key not starting with `_`) is not itself a JSON object — the shape
    every reader in `projects.py` assumes once a value is attached to a
    slug."""


def validate_document(document) -> None:
    """The document's shape: the top level must be a JSON object, and every
    entry whose key does not start with `_` (a project, not registry
    metadata) must itself be a JSON object. `projects.py`'s own read checks
    only that the file parses as JSON; this adds the object checks, and does
    not repeat the field-level checks registration will run (CQ·242). A metadata entry (`_scopes`,
    `_meta`, `_kb_legacy`, and so on) is not required to be an object,
    because the file's own metadata keys are not all shaped that way.

    Raises rather than returning a bool, so a caller cannot forget to
    check a return value the way it could a bare `is_valid()` — the same
    reasoning `crew_registry.validate_profile` gives for its own shape.
    """
    if not isinstance(document, dict):
        raise InvalidRegistryDocument(
            "the registry document must be a JSON object")
    for slug, entry in document.items():
        if not isinstance(slug, str) or not slug.strip():
            raise InvalidRegistryDocument(
                "every key in the registry document must be a non-empty "
                "string")
        if len(slug) > store.MAX_IDENT:
            raise InvalidRegistryDocument(
                f"{slug!r} is longer than {store.MAX_IDENT} characters")
        if not slug.startswith("_") and not isinstance(entry, dict):
            raise InvalidRegistryDocument(
                f"{slug!r} must be a JSON object, like every project entry")


class RegistryPopulated(RuntimeError):
    """`replace` was called without `allow_populated` on a table that
    already holds rows. Nothing was written."""


class RegistryWouldRemove(RuntimeError):
    """`replace` with `allow_populated` would remove rows or fields its
    caller did not name in `remove`/`drop_fields`, or one of those names a
    row or field the replace would not remove. Nothing was written.
    `unnamed`, `not_removed`, `unnamed_fields` and `not_removed_fields` are
    the four sorted lists the message reports (CQ·250 adds the last two); a
    field is reported as `<slug>.<field>`, never with its value."""

    def __init__(self, unnamed: list[str] = (), not_removed: list[str] = (),
                 unnamed_fields: list[str] = (),
                 not_removed_fields: list[str] = ()):
        self.unnamed = list(unnamed)
        self.not_removed = list(not_removed)
        self.unnamed_fields = list(unnamed_fields)
        self.not_removed_fields = list(not_removed_fields)
        parts = []
        if self.unnamed:
            parts.append("would remove " + ", ".join(self.unnamed)
                         + ", which --remove does not name")
        if self.not_removed:
            parts.append("--remove names " + ", ".join(self.not_removed)
                         + ", which this replace would not remove")
        if self.unnamed_fields:
            parts.append("would remove " + ", ".join(self.unnamed_fields)
                         + ", which --drop-field does not name")
        if self.not_removed_fields:
            parts.append("--drop-field names " + ", ".join(self.not_removed_fields)
                         + ", which this replace would not remove")
        super().__init__("; ".join(parts) + "; nothing was written")


def _leaf_paths(mapping: dict, prefix: tuple = ()) -> dict:
    """Every path to a value that is not itself a map, at any depth inside
    `mapping`, keyed by the tuple of its keys (e.g. `{("fields",
    "agent_prefix"): "x"}` for `{"fields": {"agent_prefix": "x"}}`) — the
    shape §6.8 uses to name a field inside a `_`-prefixed entry. A tuple,
    so a key containing a dot cannot share a path with a nested key."""
    leaves = {}
    for key, value in mapping.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            leaves.update(_leaf_paths(value, path))
        else:
            leaves[path] = value
    return leaves


def _removed_fields(before: dict, after: dict, removing: set) -> set:
    """Every field the replace would remove from a slug `after` keeps
    (CQ·250), as a tuple `(slug, key, ...)`: a field §6.8 defines as a
    top-level key of a crew's row, or the path to a non-map value inside a
    `_`-prefixed entry's own map value. A slug `removing` drops is left
    out, since `--remove` already governs it; a `_`-prefixed entry whose
    stored value is not a map has no fields, since dropping it is a slug
    removal and changing it is a changed value.

    Tuples rather than dotted strings, because a slug or key may itself
    contain a dot: `foo.bar`'s `secret` and `foo`'s `bar.secret` join to the
    same string, and one `--drop-field` would then authorize both."""
    removed = set()
    for slug, stored in before.items():
        if slug in removing or not isinstance(stored, dict):
            continue
        file_entry = after.get(slug)
        if slug.startswith("_"):
            file_map = file_entry if isinstance(file_entry, dict) else {}
            file_leaves = _leaf_paths(file_map)
            for path, value in _leaf_paths(stored).items():
                if value is not None and file_leaves.get(path) is None:
                    removed.add((slug,) + path)
        else:
            file_row = file_entry if isinstance(file_entry, dict) else {}
            for key, value in stored.items():
                if value is not None and file_row.get(key) is None:
                    removed.add((slug, key))
    return removed


def _now_iso(now=None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    if isinstance(now, str):
        now = _dt.datetime.fromisoformat(now)
    if now.tzinfo is not None:
        now = now.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return now.isoformat(" ")


def replace(document: dict, *, actor: str, allow_populated: bool = False,
            remove=(), drop_fields=(), now=None) -> tuple[list[str], list[str]]:
    """Replace every `project_registry` row with `document`'s own keys, in
    one transaction. Returns `(added, removed)`, each sorted: the slugs
    now present that were not before, and the slugs that were present and
    are gone.

    **A POPULATED TABLE REFUSES UNLESS `allow_populated`.** Once the table
    is the install's registry, a crew can be registered into it that no
    file holds, so a second load from a seed file would erase that crew.
    Without `allow_populated`, a table with any row raises
    `RegistryPopulated` and nothing is written. That makes an install's
    run-once load safe to run twice whatever guard its caller uses
    (CQ·241's thread, comment 5907). `BEGIN IMMEDIATE` holds the write lock
    from the emptiness check to the commit, so no write can land between
    the two.

    **A POPULATED REPLACE REMOVES ONLY THE ROWS ITS CALLER NAMES** (CQ·247).
    `POST /registry/crews` only adds rows, so an edit to an existing row is
    made in the seed file and loaded with `allow_populated`. A crew
    registered over the wire exists only in this table, so a replace from a
    file that lacks it would erase it, and the only sign would be one line
    of output. Unless the slugs the replace would remove equal `remove`
    exactly, it raises `RegistryWouldRemove` and writes nothing. Exact
    equality also refuses a named slug that would stay, which is how a
    typo'd name shows. A replace that removes nothing needs no `remove`, and
    an empty table has nothing to remove.

    **A POPULATED REPLACE REMOVES ONLY THE FIELDS ITS CALLER NAMES**
    (CQ·250). For a slug `after` keeps, a field is removed when the stored
    row holds a non-null value at that field and the file's row lacks the
    field or sets it null — `_removed_fields`'s own docstring gives what
    counts as a field for a crew's row and for a `_`-prefixed entry. Unless
    the removed fields equal `drop_fields` exactly, this raises
    `RegistryWouldRemove` and writes nothing, the same exact-equality rule
    `remove` uses above. A changed value is not a removal and needs no
    entry in `drop_fields`. Each `drop_fields` name splits on every dot, so
    a removed field whose slug or key contains a dot cannot be named and
    always refuses.

    **VALIDATED BY THE CALLER, NOT HERE.** `crew_registry`'s mutating
    functions validate every field themselves because an HTTP route is a
    second caller that could otherwise skip the check; this function's
    only caller is `load_project_registry.py`, which validates the whole
    document before calling in (the same "stop before any write" shape its
    sibling `load_crew_registry.py` already takes), and a second pass over
    rows this function is about to serialise would only repeat the first.

    **A FULL REPLACE, NOT AN UPSERT.** Unlike `crew_registry.roster_add`,
    which revises one name at a time, this function's caller holds the
    whole document on every run — the registry file in full — so the
    correct write is "make the table equal this document", not "merge this
    document into the table".
    """
    now_iso = _now_iso(now)
    after = set(document)
    rows = [(slug, json.dumps(entry), now_iso, actor)
            for slug, entry in document.items()]
    conn = store.connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute(
            "SELECT slug, entry FROM project_registry").fetchall()
        before = {row[0] for row in stored}
        if before and not allow_populated:
            conn.rollback()
            raise RegistryPopulated(
                f"project_registry already holds {len(before)} row(s); "
                "pass --replace to replace them")
        removing = before - after
        named = set(remove)
        before_doc = {row[0]: json.loads(row[1]) for row in stored}
        removed_fields = _removed_fields(before_doc, document, removing)
        # A name splits on every dot, so a field whose slug or key holds a
        # dot can never be named and its removal always refuses.
        named_fields = {tuple(name.split(".")) for name in drop_fields}
        if removing != named or removed_fields != named_fields:
            conn.rollback()
            raise RegistryWouldRemove(
                sorted(removing - named), sorted(named - removing),
                [".".join(f) for f in sorted(removed_fields - named_fields)],
                [".".join(f) for f in sorted(named_fields - removed_fields)])
        conn.execute("DELETE FROM project_registry")
        conn.executemany(
            "INSERT INTO project_registry (slug, entry, updated_at, "
            "updated_by) VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return sorted(after - before), sorted(before - after)


def registry_document(conn) -> dict | None:
    """The whole project registry, reconstructed from `project_registry`'s
    rows into the file's own document shape — every project entry and
    every `_`-prefixed metadata key.

    `None` when the table holds no rows, so a caller can tell "the loader
    has not run yet" from a real document — the same `None`-means-
    unreadable contract `projects.py`'s own reads give every caller that
    must fail closed on it.
    """
    rows = conn.execute("SELECT slug, entry FROM project_registry").fetchall()
    if not rows:
        return None
    return {row["slug"]: json.loads(row["entry"]) for row in rows}


# --- registering a crew over HTTP (CQ·242) ---------------------------------
#
# Every check below is a port of agent-platform's `lib/agent-platform/
# crew_registration.py` (`build_row`, `claimed_names`,
# `_kb_project_syntax_errors`) and of `projects.privacy_invariant_errors`,
# which `build_row` calls. See this module's own docstring for why a port
# rather than an import, and `tests/test_registry_crews_drift.py` for what
# pins the two together.


class RegistrationForbidden(PermissionError):
    """`ident` does not hold `registry.write`. Distinct from
    `RegistrationRefused` (a 400-shaped problem) and from
    `store.WritesDisabled` (the shadow gate), the same three-way split
    `crew_registry.RosterWriteForbidden`'s docstring draws."""


class RegistrationRefused(ValueError):
    """A field in the posted row fails a schema check `build_crew_row` runs,
    or the registry already holds a `kb_scope_exceptions`/`_kb_legacy` value
    that fails the KB-project slug rule. Nothing is written."""


class RegistrationCollision(ValueError):
    """The slug, an alias or a repo in the posted row is already claimed by
    another entry. Nothing is written."""


class RegistryNotLoaded(RuntimeError):
    """The table holds no rows, so a collision cannot be checked against an
    unloaded registry — the loader's first run needs an empty table
    (`replace`'s own docstring). Nothing is written."""


# A registry slug: lowercase, letters/digits/hyphens, leading letter — the
# literal agent-platform's `crew_registration._SLUG_OK` uses.
_SLUG_OK = re.compile(r"^[a-z][a-z0-9-]*$")

# knowledge.py's own KB-project slug rule, the literal agent-platform's
# `crew_registration._KB_PROJECT_SLUG_OK` uses.
_KB_PROJECT_SLUG_OK = re.compile(r"^[a-z][a-z-]*$")

# The literal agent-platform's `projects._CI_WORKFLOW_FILE_RE` uses: a bare
# workflow file name, no path separator or `..` segment.
_CI_WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")

# A Library person id: `person_` and a 26-character ULID (Crockford base32,
# uppercase) — the shape `store.new_ulid` mints, matching agent-platform's
# `projects.PERSON_ID_RE`.
_PERSON_ID_RE = re.compile(r"^person_[0-9A-HJKMNP-TV-Z]{26}$")

# The stored form `home_dir` takes when the caller does not supply one —
# agent-platform's `crew_registration._ROOT_PREFIX`.
_ROOT_PREFIX = "<root>/"

CLASSES = ("product", "project")
KB_VALUES = ("central", "local", "split")
LIFECYCLES = ("active", "sunset", "archived")
MODEL_TIERS = ("opus", "sonnet")


def _ci_workflows_errors(repos: list, ci_workflows) -> list[str]:
    """Ported from agent-platform's `projects.ci_workflows_errors`, taking
    the row's own `repos` rather than re-deriving them from a unit dict."""
    if not isinstance(ci_workflows, dict):
        return [f"ci_workflows must be a dict, got {ci_workflows!r}"]
    errors = []
    for repo, files in ci_workflows.items():
        if repo not in repos:
            errors.append(f"ci_workflows names {repo!r}, not one of its own repos")
            continue
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            errors.append(
                f"ci_workflows.{repo} must be a list of strings, got {files!r}")
            continue
        for name in files:
            if not _CI_WORKFLOW_FILE_RE.match(name):
                errors.append(
                    f"ci_workflows.{repo} names {name!r}, not a bare workflow "
                    f"file name (no path separator or '..' segment)")
    return errors


def _privacy_invariant_errors(row: dict) -> list[str]:
    """Ported from agent-platform's `projects.privacy_invariant_errors`
    (ADR-0043): why `row` breaks the black-box contract a private crew must
    meet, or `[]` when it holds."""
    if row.get("private") is not True:
        return []
    errors = []
    if row.get("kb") != "local":
        errors.append(f"kb={row.get('kb')!r}, not 'local'")
    if row.get("task_text_public") is True:
        errors.append("task_text_public is true")
    return errors


def build_crew_row(payload) -> tuple[str, dict]:
    """The posted body, as `(slug, row)` — `row` in the same shape
    `replace()`'s document stores under `slug`, never carrying `slug` itself
    as a field. Raises `RegistrationRefused`, naming the field, on anything
    that would fail agent-platform's `crew_registration.build_row`. Ported
    rather than shared, in the order that module checks them, so a refusal
    here names the same field a refusal there would."""
    if not isinstance(payload, dict):
        raise RegistrationRefused("body must be a JSON object")

    slug = payload.get("slug")
    if not isinstance(slug, str) or not _SLUG_OK.match(slug):
        raise RegistrationRefused(
            f"slug: expected a lowercase letter followed by lowercase "
            f"letters, digits or hyphens, got {slug!r}")

    display = payload.get("display")
    if not isinstance(display, str) or not display.strip():
        raise RegistrationRefused("display: a non-empty string is required")

    raw_repos = payload.get("repos")
    if not isinstance(raw_repos, list) or not all(
            isinstance(r, str) for r in raw_repos):
        raise RegistrationRefused("repos: must be a list of strings")
    repos = [r for r in raw_repos if r.strip()]
    if not repos:
        raise RegistrationRefused("repos: at least one repo is required")

    cls = payload.get("class", "project")
    if cls not in CLASSES:
        raise RegistrationRefused(f"class: must be one of {CLASSES}, got {cls!r}")

    kb = payload.get("kb", "central")
    if kb not in KB_VALUES:
        raise RegistrationRefused(f"kb: must be one of {KB_VALUES}, got {kb!r}")

    lifecycle = payload.get("lifecycle", "active")
    if lifecycle not in LIFECYCLES:
        raise RegistrationRefused(
            f"lifecycle: must be one of {LIFECYCLES}, got {lifecycle!r}")

    model_tier = payload.get("model_tier")
    if model_tier is not None and model_tier not in MODEL_TIERS:
        raise RegistrationRefused(
            f"model_tier: must be one of {MODEL_TIERS}, got {model_tier!r}")

    created_by = payload.get("created_by")
    if created_by is not None and not (
            isinstance(created_by, str) and _PERSON_ID_RE.fullmatch(created_by)):
        raise RegistrationRefused(
            f"created_by: must be a Library person id (person_<ULID>), got "
            f"{created_by!r}")

    owner = payload.get("owner", "convivy")
    if not isinstance(owner, str) or not owner.strip():
        raise RegistrationRefused("owner: must be a non-empty string")

    aliases = payload.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise RegistrationRefused("aliases: must be a list of strings")

    products = payload.get("products", [])
    if not isinstance(products, list) or not all(isinstance(p, str) for p in products):
        raise RegistrationRefused("products: must be a list of strings")

    dirs = payload.get("dirs", [])
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise RegistrationRefused("dirs: must be a list of strings")

    home_dir = payload.get("home_dir")
    if home_dir is not None and not isinstance(home_dir, str):
        raise RegistrationRefused("home_dir: must be a string")

    task_text_public = payload.get("task_text_public", False)
    if not isinstance(task_text_public, bool):
        raise RegistrationRefused("task_text_public: must be a boolean")

    private = payload.get("private", False)
    if not isinstance(private, bool):
        raise RegistrationRefused("private: must be a boolean")

    data_local = payload.get("data_local", False)
    if not isinstance(data_local, bool):
        raise RegistrationRefused("data_local: must be a boolean")

    agent_prefix = payload.get("agent_prefix")
    if agent_prefix is not None and not isinstance(agent_prefix, str):
        raise RegistrationRefused("agent_prefix: must be a string")

    raw_ci_workflows = payload.get("ci_workflows", {})
    ci_errors = _ci_workflows_errors(repos, raw_ci_workflows)
    if ci_errors:
        raise RegistrationRefused(f"ci_workflows: {'; '.join(ci_errors)}")

    crew_cockpit = payload.get("crew_cockpit")
    if crew_cockpit is not None and not isinstance(crew_cockpit, bool):
        raise RegistrationRefused("crew_cockpit: must be a boolean")

    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        raise RegistrationRefused("note: must be a string")

    crew = payload.get("crew")
    if crew is not None and not isinstance(crew, str):
        raise RegistrationRefused("crew: must be a string")

    kb_project = payload.get("kb_project")
    if kb_project is not None and not isinstance(kb_project, str):
        raise RegistrationRefused("kb_project: must be a string")
    if kb == "local":
        if kb_project:
            raise RegistrationRefused(
                "kb_project: must not be set when kb='local'")
    else:
        kb_project = kb_project or slug
    if kb_project and not _KB_PROJECT_SLUG_OK.fullmatch(kb_project):
        raise RegistrationRefused(
            f"kb_project: {kb_project!r} is not a valid KB project slug — "
            f"expected a lowercase letter followed by lowercase letters or "
            f"hyphens, no digits")

    row: dict = {"class": cls, "display": display, "owner": owner}
    if created_by:
        row["created_by"] = created_by
    row.update({
        "home_dir": home_dir or f"{_ROOT_PREFIX}{slug}",
        "dirs": list(dirs),
        "repos": repos,
        "aliases": list(aliases),
        "crew": crew if crew is not None else slug,
        "lifecycle": lifecycle,
        "products": list(products),
        "kb": kb,
    })
    if kb_project:
        row["kb_project"] = kb_project
    # Written explicitly even when false, matching every existing row —
    # agent-platform's `CrewSpec` docstring states why.
    row["task_text_public"] = bool(task_text_public)
    if data_local:
        row["data_local"] = True
    if model_tier:
        row["model_tier"] = model_tier
    if private:
        row["private"] = True
    if agent_prefix:
        row["agent_prefix"] = agent_prefix
    if raw_ci_workflows:
        row["ci_workflows"] = {
            repo: list(files) for repo, files in raw_ci_workflows.items()}
    crew_cockpit_effective = (
        crew_cockpit if crew_cockpit is not None else (kb == "central"))
    if crew_cockpit_effective:
        row["crew_cockpit"] = True
    if note:
        row["note"] = note

    # ADR-0043: refused rather than defaulted, for the reason agent-platform's
    # `build_row` gives — a silent switch to `kb='local'` would make the row
    # depend on whether `kb` was typed at all, not on what it said.
    violations = _privacy_invariant_errors(row)
    if violations:
        raise RegistrationRefused(
            f"private: {slug!r} is private, which requires kb='local' and "
            f"task_text_public=false (ADR-0043 — a black box crew must not "
            f"be centrally indexed or publish task text), but "
            f"{'; '.join(violations)}")

    return slug, row


def _claimed_names(document: dict) -> set[str]:
    """Ported from agent-platform's `crew_registration.claimed_names`: every
    name already spoken for in `document`, lowercased.

    **A SET, NOT A MAP TO WHAT CLAIMS EACH NAME.** `registry.write` does not
    carry `registry.read`, so a refusal that named the claiming entry would
    let a registering caller learn a private crew's slug by guessing its
    aliases and repos. Keeping no claimant here leaves the refusal nothing
    to name."""
    claimed: set[str] = set()
    scopes = document.get("_scopes", {})
    if isinstance(scopes, dict):
        for name in scopes:
            if isinstance(name, str) and name.strip() and not name.startswith("_"):
                claimed.add(name.strip().lower())
    for slug, unit in document.items():
        if not isinstance(slug, str) or slug.startswith("_") or not slug.strip():
            continue
        names = [slug]
        if isinstance(unit, dict):
            for key in ("aliases", "repos"):
                value = unit.get(key)
                if isinstance(value, list):
                    names.extend(value)
        for name in names:
            if isinstance(name, str) and name.strip():
                claimed.add(name.strip().lower())
    return claimed


def _answered_names(slug: str, entry: dict) -> set[str]:
    """Every casefolded name `entry` answers to: the set
    `projects.Registry.private_crew_names` collects for a private entry
    (the slug, `display`, `crew`, `aliases`, `repos`, and the basename of
    `home_dir` and of each `dirs` entry).

    **NO NAME MAY BE ANSWERED TO BY BOTH A PRIVATE AND A PUBLIC ENTRY.**
    Every name a private entry answers to joins `private_crew_names`, which
    Reference Room admission and the roster refuse. `_claimed_names` covers
    only slugs, aliases and repos, so without this a private registration
    could put a public crew's slug in its own `crew`, `display` or `dirs`
    and withhold that public crew, and a public crew registered after a
    private row already answering to its name would be withheld the same
    way. So a private row is checked against every public entry and a
    public row against every private one. Several projects may share a
    crew (ARCH-426), so two entries on the same side are not compared."""
    collected = [slug, entry.get("display"), entry.get("crew"),
                 *(entry.get("aliases") or []), *(entry.get("repos") or [])]
    for p in [entry.get("home_dir"), *(entry.get("dirs") or [])]:
        if isinstance(p, str) and p.strip():
            collected.append(Path(p).name)
    return {n.strip().casefold() for n in collected
            if isinstance(n, str) and n.strip()}


def _kb_project_syntax_errors(document: dict) -> list[str]:
    """Ported from agent-platform's
    `crew_registration._kb_project_syntax_errors`: every existing
    `kb_scope_exceptions` value's `kb_project` and every `_kb_legacy` entry
    in `document` that fails the KB-project slug rule, so a bad value
    already in the table is refused rather than carried forward into
    another write."""
    errors = []
    for slug, unit in document.items():
        if not isinstance(unit, dict):
            continue
        for repo, exc in (unit.get("kb_scope_exceptions") or {}).items():
            kb_project = exc.get("kb_project") if isinstance(exc, dict) else None
            if kb_project is not None and not _KB_PROJECT_SLUG_OK.fullmatch(kb_project):
                errors.append(
                    f"{slug!r}.kb_scope_exceptions[{repo!r}].kb_project "
                    f"{kb_project!r} is not a valid KB project slug")
    for entry in document.get("_kb_legacy", []) or []:
        if isinstance(entry, str) and not _KB_PROJECT_SLUG_OK.fullmatch(entry):
            errors.append(f"_kb_legacy entry {entry!r} is not a valid KB project slug")
    return errors


def register_crew(ident: identity.Identity, payload, *, now=None) -> dict:
    """Add one crew to the registry. Returns the stored row, with `slug`,
    `updated_at` and `updated_by` included.

    **GATED ON `registry.write`, CHECKED FIRST** — before the payload's own
    shape, matching `crew_registry._require_write`'s placement ahead of
    every other check in its own mutating functions, so a caller lacking
    the capability learns nothing about why its payload would otherwise be
    refused.

    **REGISTRATION ADDS AN ENTRY AND NEVER REPLACES OR REMOVES ONE** — unlike
    `replace()`, which makes the table equal a whole document. A slug that
    already exists collides with itself in `_claimed_names` the same way an
    alias or a repo would, so this never needs a separate "already exists"
    check.

    **ONE TRANSACTION, `BEGIN IMMEDIATE` HELD FROM THE EMPTINESS CHECK
    THROUGH THE INSERT**, the same shape `replace()` uses: so two concurrent
    registrations cannot both claim one name, and so a collision is checked
    against a document no write can change out from under it.

    **503 ON AN EMPTY TABLE** — a collision cannot be checked against a
    registry nobody has loaded, and `load_project_registry.py`'s own first
    run needs the table empty (`replace`'s docstring); refusing to register
    into that state rather than writing the row alone keeps `GET /registry`
    from answering with a single crew and nothing else.
    """
    if not identity.authorized(ident, "registry.write"):
        raise RegistrationForbidden(
            f"{ident.kind!r} identity lacks 'registry.write' — only an "
            f"orchestrator or registrar credential may register a crew")
    if not isinstance(ident.actor, str) or len(ident.actor) > store.MAX_ACTOR:
        raise RegistrationRefused(
            f"actor must be a string of at most {store.MAX_ACTOR} characters")
    slug, row = build_crew_row(payload)
    now_iso = _now_iso(now)
    conn = store.connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT slug, entry FROM project_registry").fetchall()
        if not rows:
            conn.rollback()
            raise RegistryNotLoaded(
                "the project registry is empty; load_project_registry.py "
                "has not been run against this vault yet")
        document = {r["slug"]: json.loads(r["entry"]) for r in rows}
        syntax_errors = _kb_project_syntax_errors(document)
        if syntax_errors:
            # The count only: the errors name other entries' slugs and
            # repos, private ones included, which a caller without
            # `registry.read` must not learn from a refusal.
            conn.rollback()
            raise RegistrationRefused(
                f"the registry already has {len(syntax_errors)} KB-project "
                f"value(s) that fail the slug rule, so this write is "
                f"refused rather than carrying them forward; an operator "
                f"must correct the registry with load_project_registry.py "
                f"--replace")
        # Both checks below refuse with this one message, so the wording
        # never tells a caller which check fired, and so never that the
        # claimant is private. That a refusal happened at all still says
        # so for a display, crew or directory name, which only the second
        # check covers; the PR that added it (library#334) records why
        # that residual is accepted.
        collision = "{!r} is already claimed; pick a different name"
        claimed = _claimed_names(document)
        for name in (slug, *row["aliases"], *row["repos"]):
            if name.strip().lower() in claimed:
                conn.rollback()
                raise RegistrationCollision(collision.format(name))
        private = row.get("private") is True
        other_side = set()
        for other_slug, entry in document.items():
            if (not other_slug.startswith("_") and isinstance(entry, dict)
                    and (entry.get("private") is True) != private):
                other_side |= _answered_names(other_slug, entry)
        for name in sorted(_answered_names(slug, row) & other_side):
            conn.rollback()
            raise RegistrationCollision(collision.format(name))
        conn.execute(
            "INSERT INTO project_registry (slug, entry, updated_at, "
            "updated_by) VALUES (?, ?, ?, ?)",
            (slug, json.dumps(row), now_iso, ident.actor))
        conn.commit()
    finally:
        conn.close()
    return {"slug": slug, **row, "updated_at": now_iso, "updated_by": ident.actor}


# --- editing a crew over HTTP (CQ·249) -------------------------------------
#
# `PATCH /registry/crews/{slug}` merges a body of fields over a stored row,
# reruns every check `register_crew` runs on the result, and writes an audit
# row to `project_registry_edits`. §6.8 (knowledge#1062, proposed alongside
# CQ·249) is the text this section builds.

# Every field `POST /registry/crews` takes from its payload, `slug` itself
# excluded — this route never renames a crew, so `slug` is refused before
# the declared-field check even runs (see `patch_crew`).
_POST_FIELDS = frozenset({
    "display", "repos", "class", "kb", "lifecycle", "model_tier",
    "created_by", "owner", "aliases", "products", "dirs", "home_dir",
    "task_text_public", "private", "data_local", "agent_prefix",
    "ci_workflows", "crew_cockpit", "note", "crew", "kb_project",
})

# The fields every stored row carries, ported from `build_crew_row`'s own
# always-written set (the dict literal and the two `row.update`/assignment
# calls that follow it). `null` is refused on each of these, because
# `build_crew_row` would otherwise apply a silent default rather than
# refuse — `class` defaulting back to `"project"`, `owner` to `"convivy"`,
# and so on — for every field below that build_crew_row defaults instead of
# requiring outright.
ALWAYS_CARRIED_FIELDS = frozenset({
    "class", "display", "owner", "home_dir", "dirs", "repos", "aliases",
    "crew", "lifecycle", "products", "kb", "task_text_public",
})


def _is_string(value) -> bool:
    return isinstance(value, str)


def _is_boolean(value) -> bool:
    return isinstance(value, bool)


def _is_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_string_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_string_map(value) -> bool:
    return (isinstance(value, dict)
            and all(isinstance(k, str) and isinstance(v, str)
                    for k, v in value.items()))


def _is_kb_scope_exceptions_shape(value) -> bool:
    return (isinstance(value, dict)
            and all(isinstance(k, str) and _is_string_map(v)
                    for k, v in value.items()))


def _is_typecheck_shape(value) -> bool:
    if not isinstance(value, dict):
        return False
    return all(isinstance(k, str) and (_is_string_map(v) or _is_string_list(v))
               for k, v in value.items())


# The 27 fields stored rows already carried on 2026-10-27, each mapped to
# `(shape check, shape description)` — the description is what a 400 names
# the field as needing. `PATCH /registry/crews/{slug}` is the one place
# these are validated; `build_crew_row` does not know about them and carries
# them through unchanged once they pass here (`patch_crew`, below).
EXTRA_FIELDS: dict = {
    "co_accent": (_is_string, "a string"),
    "co_cockpit_gate": (_is_string, "a string"),
    "co_fg": (_is_string, "a string"),
    "co_label": (_is_string, "a string"),
    "co_style_id": (_is_string, "a string"),
    "github_owner": (_is_string, "a string"),
    "signal_product": (_is_string, "a string"),
    "co_generic_refresh": (_is_boolean, "a boolean"),
    "co_self_contained": (_is_boolean, "a boolean"),
    "company_workspace": (_is_boolean, "a boolean"),
    "github_owner_private": (_is_boolean, "a boolean"),
    "local_agents": (_is_boolean, "a boolean"),
    "postgres_socket": (_is_boolean, "a boolean"),
    "velocity_order": (_is_integer, "an integer"),
    "activation_lanes": (_is_string_list, "a list of strings"),
    "adr_kb_projects": (_is_string_list, "a list of strings"),
    "channel_prefixes": (_is_string_list, "a list of strings"),
    "code_health": (_is_string_list, "a list of strings"),
    "cost_fold_aliases": (_is_string_list, "a list of strings"),
    "profiled_dirs": (_is_string_list, "a list of strings"),
    "rollup": (_is_string_list, "a list of strings"),
    "sandbox_repos": (_is_string_list, "a list of strings"),
    "default_profile": (_is_string_map, "a map from a string to a string"),
    "repo_display": (_is_string_map, "a map from a string to a string"),
    "repo_owners": (_is_string_map, "a map from a string to a string"),
    "kb_scope_exceptions": (
        _is_kb_scope_exceptions_shape,
        "a map from a string to a map from a string to a string"),
    "typecheck": (
        _is_typecheck_shape,
        "a map from a string to a map from a string to a string, or to a "
        "list of strings"),
}

class CrewNotFound(LookupError):
    """No entry has `slug`, including a `_`-prefixed key, which is registry
    metadata and never a crew. Nothing is written."""


# The fields a `PATCH /registry/crews/{slug}` body may not name, in the
# order its refusal names the first one present.
#
# - `repos`, `ci_workflows`, `lifecycle` and `private` decide which (repo,
#   workflow) pairs `GET /ci/tracked` derives, and so which CI result merge
#   admission requires.
# - `private`, `kb`, `kb_project` and `task_text_public` are what the
#   privacy invariant and `kb_project`'s own rule read. Each check passes or
#   fails by whether the stored crew is private (every private crew has
#   `kb="local"`, which refuses any `kb_project`), so a refusal on any of
#   them would answer that question to a caller without `registry.read`.
# - `owner`, `github_owner`, `github_owner_private` and `repo_owners`
#   decide each repo's GitHub owner (`projects.repo_owner`, and
#   agent-platform's velocity report for `github_owner_private`).
# - `kb_project_by_repo` decides which crew's knowledge-base context an
#   agent-platform dispatch receives, and `kb_scope_exceptions` narrows a
#   repo below its crew's `kb_project` and so decides it too. agent-platform
#   checks every `kb_project_by_repo` target against the
#   `kb_scope_exceptions` values and refuses to load the registry when one
#   is missing.
#
# Changing any of them on an existing crew is made in the install's
# registry file and loaded with `load_project_registry.py --replace`. The
# refusal is on the name and runs before the stored row is read, so it says
# nothing about that row.
OPERATOR_FIELDS = (
    "repos", "ci_workflows", "lifecycle", "private",
    "kb", "kb_project", "task_text_public",
    "owner", "github_owner", "github_owner_private", "repo_owners",
    "kb_project_by_repo", "kb_scope_exceptions",
)

# The declared field set `PATCH /registry/crews/{slug}` accepts in its body,
# `slug` excluded (refused separately, before this set is even consulted).
DECLARED_PATCH_FIELDS = (
    _POST_FIELDS | frozenset(EXTRA_FIELDS) | frozenset(OPERATOR_FIELDS))


def patch_crew(ident: identity.Identity, slug: str, payload, *,
               now=None) -> dict:
    """Merge `payload`'s fields over the crew stored at `slug`. Returns
    `slug`, `changed` (the sorted names of the fields this patch changed —
    `[]` for a no-op, which writes nothing), `updated_at` and `updated_by`,
    and no stored field: a `registry.write` holder need not hold
    `registry.read`, so echoing the row would hand it any crew's contents.

    **GATED ON `registry.write`, CHECKED FIRST**, before the body is even
    parsed for shape — the same placement `register_crew`'s own docstring
    gives for the same reason.

    **EVERY FIELD-LEVEL CHECK THAT DOES NOT NEED THE STORED ROW RUNS BEFORE
    `store.connect_rw()`**: the body's own shape, `slug` named in the body,
    a field outside `DECLARED_PATCH_FIELDS`, a field in `OPERATOR_FIELDS`,
    and `null` on a field in `ALWAYS_CARRIED_FIELDS`, matching
    `register_crew`'s own schema-before-503 order. The checks that need the
    stored row — the rebuild through `build_crew_row`, the KB-project syntax
    check, then the collision check and the private-line check — run
    inside the one `BEGIN IMMEDIATE` transaction below, reusing
    `register_crew`'s own helpers rather than forking copies of them.

    **THE COLLISION AND PRIVATE-LINE CHECKS EXCLUDE THIS CREW'S OWN STORED
    ROW.** Both are run against `document` with `slug` removed, so a crew's
    own existing aliases and repos never collide with themselves.
    """
    if not identity.authorized(ident, "registry.write"):
        raise RegistrationForbidden(
            f"{ident.kind!r} identity lacks 'registry.write' — only an "
            f"orchestrator or registrar credential may edit a crew")
    if not isinstance(ident.actor, str) or len(ident.actor) > store.MAX_ACTOR:
        raise RegistrationRefused(
            f"actor must be a string of at most {store.MAX_ACTOR} characters")
    if not isinstance(payload, dict) or not payload:
        raise RegistrationRefused(
            "body must be a JSON object naming at least one field to set")
    if "slug" in payload:
        raise RegistrationRefused(
            "slug: this route edits a crew's fields and does not rename it")
    unknown = sorted(set(payload) - DECLARED_PATCH_FIELDS)
    if unknown:
        raise RegistrationRefused(
            f"{unknown[0]}: not a field this route accepts")
    for field in OPERATOR_FIELDS:
        if field in payload:
            raise RegistrationRefused(f"{field!r} is an operator change")
    for field in ALWAYS_CARRIED_FIELDS:
        if field in payload and payload[field] is None:
            raise RegistrationRefused(
                f"{field}: every stored row carries this field; it cannot "
                f"be removed with null")
    for field, value in payload.items():
        if value is None or field not in EXTRA_FIELDS:
            continue
        check, shape = EXTRA_FIELDS[field]
        if not check(value):
            raise RegistrationRefused(f"{field}: must be {shape}, got {value!r}")

    now_iso = _now_iso(now)
    conn = store.connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT slug, entry, updated_at, updated_by "
            "FROM project_registry").fetchall()
        if not rows:
            conn.rollback()
            raise RegistryNotLoaded(
                "the project registry is empty; load_project_registry.py "
                "has not been run against this vault yet")
        document = {r["slug"]: json.loads(r["entry"]) for r in rows}
        if slug.startswith("_") or slug not in document:
            conn.rollback()
            raise CrewNotFound(f"no crew named {slug!r}")
        meta = {r["slug"]: (r["updated_at"], r["updated_by"]) for r in rows}
        stored = document[slug]
        stored_updated_at, stored_updated_by = meta[slug]

        rebuild_payload = {"slug": slug}
        for field in _POST_FIELDS:
            if field in stored:
                rebuild_payload[field] = stored[field]
        # The stored row alone first. A refusal there is about a value the
        # caller never sent, and `build_crew_row`'s message quotes that
        # value, so it is replaced by one naming only the field: a
        # `registry.write` holder need not hold `registry.read`.
        try:
            build_crew_row(rebuild_payload)
        except RegistrationRefused as e:
            conn.rollback()
            field = str(e).split(":", 1)[0]
            raise RegistrationRefused(
                f"{field}: this crew's stored value fails a registration "
                f"check, so no patch to it is accepted until an operator "
                f"corrects it with load_project_registry.py --replace"
            ) from None
        for field, value in payload.items():
            if field not in _POST_FIELDS:
                continue
            if value is None:
                rebuild_payload.pop(field, None)
            else:
                rebuild_payload[field] = value
        try:
            _, merged_row = build_crew_row(rebuild_payload)
        except RegistrationRefused:
            conn.rollback()
            raise

        merged_extras = {k: v for k, v in stored.items() if k in EXTRA_FIELDS}
        for field, value in payload.items():
            if field not in EXTRA_FIELDS:
                continue
            if value is None:
                merged_extras.pop(field, None)
            else:
                merged_extras[field] = value
        merged_row.update(merged_extras)

        # The row to store keeps every field the body does not name exactly
        # as stored, present or absent, a stored field outside the declared
        # set (one loaded with `--replace` before `EXTRA_FIELDS` names it)
        # included, and so is every operator field, which a body may not
        # name. `merged_row` is the rebuilt form the checks below read;
        # storing it whole would also rewrite a field the caller never
        # named wherever the builder's default differs from the stored value (seven live rows carried no `crew_cockpit` on
        # 2026-10-27, and one a null `task_text_public`).
        new_row = {k: v for k, v in stored.items() if k not in payload}
        new_row.update({k: merged_row[k] for k in payload if k in merged_row})

        # Over the registry as this patch would leave it. A body cannot name
        # `kb_scope_exceptions`, but this keeps the check on the row being
        # written, as `register_crew`'s is.
        syntax_errors = _kb_project_syntax_errors({**document, slug: new_row})
        if syntax_errors:
            conn.rollback()
            raise RegistrationRefused(
                f"the registry would hold {len(syntax_errors)} KB-project "
                f"value(s) that fail the slug rule, counting this patch's "
                f"own, so this write is refused; a value already stored is "
                f"corrected with load_project_registry.py --replace")

        other_entries = {k: v for k, v in document.items() if k != slug}
        collision = "{!r} is already claimed; pick a different name"
        claimed = _claimed_names(other_entries)
        for name in (slug, *merged_row["aliases"], *merged_row["repos"]):
            if name.strip().lower() in claimed:
                conn.rollback()
                raise RegistrationCollision(collision.format(name))
        private = merged_row.get("private") is True
        other_side = set()
        for other_slug, entry in other_entries.items():
            if (not other_slug.startswith("_") and isinstance(entry, dict)
                    and (entry.get("private") is True) != private):
                other_side |= _answered_names(other_slug, entry)
        for name in sorted(_answered_names(slug, merged_row) & other_side):
            conn.rollback()
            raise RegistrationCollision(collision.format(name))

        changed = sorted(
            k for k in set(new_row) | set(stored)
            if new_row.get(k) != stored.get(k))
        if not changed:
            conn.rollback()
            return {"slug": slug, "changed": [],
                    "updated_at": stored_updated_at,
                    "updated_by": stored_updated_by}

        changes = {k: {"before": stored.get(k), "after": new_row.get(k)}
                  for k in changed}
        conn.execute(
            "UPDATE project_registry SET entry = ?, updated_at = ?, "
            "updated_by = ? WHERE slug = ?",
            (json.dumps(new_row), now_iso, ident.actor, slug))
        conn.execute(
            "INSERT INTO project_registry_edits (slug, actor, at, changes) "
            "VALUES (?, ?, ?, ?)",
            (slug, ident.actor, now_iso, json.dumps(changes)))
        conn.commit()
    finally:
        conn.close()
    return {"slug": slug, "changed": changed, "updated_at": now_iso,
            "updated_by": ident.actor}
