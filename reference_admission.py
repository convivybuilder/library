"""Whether a Reference Room document change may land — the Library's half of
the write split.

`issue_01M08YECXZ68NG1WWHG0NRD086`. The operator, 2026-08-18: "KB writes don't go
through the Library, they go from agentctl to gh after approval from the
Library. The mechanics of the action are a question for the Bosun's Bead and
Course, not the Library's." So this answers one question — may this land —
and does none of the pushing.

**BOTH CLAUSES RUN ON EVERY REQUEST.** Frontmatter validation was v1 (the operator,
2026-08-19); the checkout clause is `check_checkout` below, made unconditional
in `issue_01M19QW42CFK7TDZCKWK18ECY5`. A caller must name an `owner` — an
absent or null one is refused, naming the checkout clause, rather than
answered on frontmatter alone. Reaching the clause also needs `reference.read`
alongside `reference.admit`. Whether a reviewer approved it is the Bosun's
merge-admission call (`issue_01KZCGQ43C0W6TBYTPDMR0HNVG`) to design, on
purpose — "one mechanism at two moments" answered separately is how it ends
up with two shapes.

**THIS ROUTE IS CALLED ON BOTH THE HUMAN PATH AND THE AGENT PATH, AS OF
agent-platform#966 (CQ·531).** Until that PR merged, it was called on the
human path and bypassed on the agent path — an asymmetry more dangerous than
an uncalled gate, because the route had real traffic and looked exercised to
anyone who checked whether it was live. It no longer is one.

`convivy-orient`'s in-window KB editor calls it on every save: `kb_write_api.py`
builds a partial frontmatter, calls `library_client.check_reference_admission`,
returns 422 on `may_land: false`, and otherwise commits through
`kb_commit.commit_document`, which shells to `agentctl kb checkout` and
`agentctl kb apply` and returns only once the write has landed on `origin/main`.
That caller has been live since 2026-08-25.

`agentctl kb apply` now calls it too: `_kb_admission_check` asks
`POST /reference/admission` as the last gate before the commit and honours
the answer with no override flag, refusing hard on `may_land: false`, on the
Library being unreachable, and on any other error in the call (CQ·96 comment
1202, CQ·531). `scribe apply` and `triage.py` still validate locally and
push without asking; they are a narrower remaining gap than "every
agent-initiated write", not the whole of it.

The agent-path claim above is about another repository and can stop being
true without anything here changing. Run this rather than trusting the
paragraph above:

    grep -rnE "reference/admission|reference_admission" ~/Convivy/agent-platform/{bin,lib}

On 2026-10-21 this returned `bin/agentctl` (the admission call itself) and
`bin/mcp-kb-server` (a comment naming it).

**AN EARLIER VERSION OF THIS DOCSTRING SAID NOTHING CALLED THIS ROUTE AND GAVE
ONLY THE FIRST COMMAND AS ITS PROOF.** The claim was about every caller
everywhere; the check looked in one repository. Orient's caller had already been
live for a day when it was written. **A check narrower in scope than the claim it
supports reports success for the part it looked at**, which is how this survived
three days, two crews, and a steward ruling that repeated it.

**WHAT IS GATED TODAY, PRECISELY.** The checkout clause itself is no longer
optional — every request evaluates it, and one that names no `owner` is
refused rather than answered on frontmatter alone. `clauses_checked` on the
response still says which clauses actually ran — read that rather than
inferring enforcement from a green answer.

**THE CHECKOUT CLAUSE LANDING WAS THE PRECONDITION FOR WIRING `kb apply` TO
CALL THIS, AND BOTH HAVE NOW HAPPENED.** The steward's revised ordering
(`issue_01M0YAS4S64P5TXN7S2C17GHF6`, comment 1204, superseding the ruling in
comment 1197): `kb checkout` returns a content-sha without minting a
competing lease, then this file gains the checkout clause, then `kb apply`
calls admission and honours the answer. Wiring before the clause would have
produced a gate that is present, correct, called, and blind to the case it
was called for, which is worse than an uncalled one because it leaves
nothing open to find. The failure contract for that call is comment 1202,
landed as agent-platform#966 (CQ·531): hard refuse on refusal, on
unreachability and on any other error, with no override flag.

The half-wired state this paragraph used to describe — "everything here
works, the tests pass, and the absence lives entirely in another repo" — is
the shape to watch for again whenever a split like this one lands in two
repositories on two schedules: a control that is present, correct and
unreached costs the fleet exactly as much as one that is absent, and looks
healthy where an absent one does not. `issue_01M08YECXZ68NG1WWHG0NRD086`
carries this split's state; `issue_01KZQA8A1SEEWSNYV5H8N0EQW7`'s branch
protection, which waited on `agentctl kb apply` asking before a push path
that never asks could be blocked, can now proceed on that front.

**`validate_frontmatter` and the constants it checks against are duplicated
from `knowledge/scripts/validate.py`, deliberately**, for the reason
`reference.py`'s BM25 constants already state: the Library and `knowledge`
are separate deployables, so sharing a package is not free, and copy-paste is
the most fragile way to hold an invariant two surfaces both depend on.
`tests/test_reference_admission.py` pins the values here the same way
`test_the_ranking_constants_have_not_drifted` pins the BM25 ones, and
cross-checks them against the real source file when a local `knowledge`
checkout exists.

**Structural errors only — the ones `knowledge/scripts/validate.py` calls
ERRORS** (unparseable structure, missing required fields, an invalid
kind/project/status/visibility, a malformed id or date, a list field holding
something else). `validate.py`'s own WARNINGS are mostly drift-prone rather
than structural and stay outside admission's scope: an unresolved
`depends_on` doesn't stop a document from indexing, and this route doesn't
check it either. Roster drift on `audience` and `binds_agents` is the
deliberate exception (`issue_01M34SYVH2M48K2S0FQK3K6REY`, the operator's
ruling, 2026-10-11): when a caller passes a `roster`, `validate_frontmatter` refuses a
value either field names that is not on it, which is stricter than
`validate.py`, where `audience` drift against the roster is only a warning.
The knowledge repo's own roster copy is deleted in a later change, once this
route is the one every write goes through.

**This module never parses YAML.** `agentctl` already parses the document's
frontmatter locally — it has to, to build the push in the first place — and
sends the result as a JSON object. Re-parsing YAML here would need a new
dependency for a check that's really about the SCHEMA, not the syntax; a
caller that cannot parse its own file's YAML fails before it ever reaches
this route, which is where that failure belongs.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import NamedTuple

import projects

ALLOWED_KINDS = frozenset({
    "agent", "skill", "architecture", "decision", "spec", "runbook",
    "playbook", "reference", "prd", "brand", "overview", "legal",
    "marketing", "meeting-notes", "feature-request", "observation",
    "proposal", "brief", "field-notes", "bead",
    # knowledge#527 (2026-09-16). This port previously refused every
    # instruction block that Orient's writing surface sent here.
    "instruction-block",
    # issue_01M2TWRWHJMA6MF9NKZ9Z05KXN (2026-10-12): the build plan, minted
    # in knowledge's validate.py by knowledge#876. Added here in step so this
    # port does not refuse it the way it once refused instruction blocks.
    "build-plan",
})

ALLOWED_STATUSES = frozenset({"draft", "current", "deprecated", "archived"})
ALLOWED_VISIBILITY = frozenset({"public", "team", "internal"})

REQUIRED = ("id", "kind", "project", "title", "status")
# `binds_*` since knowledge#527 (`binds_sessions` since knowledge#549, `binds_roles` since knowledge#614): an
# instruction block's selector fields, which must be lists. The block-specific ERRORS (`instruction_block_errors` in
# validate.py: scope, selector vocabularies, the floor rule, the body budget and
# the relative-cross-reference check) are NOT ported. Most of them read the
# body, which this route never receives, and porting the frontmatter half would
# add a second copy of rules that knowledge's CI already enforces on every
# commit.
LIST_FIELDS = ("topics", "audience", "applies_to", "depends_on", "authors", "tools",
               "binds_crews", "binds_agents", "binds_topics", "binds_sessions",
               "binds_roles")
# A project slug is one directory name under `projects/` matching the syntax
# below — the same rule an id's kind segment follows. Anchored with `\Z` and
# matched with `fullmatch`: `$` also matches before a trailing newline, so a
# name of `foo\n` would otherwise slip every refusal below (agent-platform#685).
_PROJECT_SLUG = re.compile(r"^[a-z][a-z-]*\Z")
# The scopes ARCH-438 retired. A directory with one of these names is refused
# even though it matches the slug syntax.
RETIRED_PROJECTS = frozenset({"shared", "convivy", "utility", "engineering"})


def id_pattern_for(allowed) -> re.Pattern:
    """The id pattern whose project alternatives are exactly `allowed`. Every
    member must already satisfy `_PROJECT_SLUG`, so nothing needs escaping."""
    return re.compile(
        r"^(" + "|".join(sorted(allowed)) + r")/[a-z][a-z-]*/[a-z0-9][a-z0-9-]*\Z")


def _registry_projects() -> frozenset[str] | None:
    """platform/decisions/0097 point 1's set — the only list of legal
    knowledge-base project names: `projects.kb_slugs()` union
    `projects.scopes()` union `projects.kb_legacy()`. `None` when any of the
    three could not be read, distinguishably from "the registry is readable
    and admits nothing" for the same reason each of them returns `None`
    rather than an empty set.

    The three are read off one snapshot (`projects._snapshot`) taken once
    here, so they come from one registry generation even when a rewrite
    lands mid-call (security-reviewer, PR #307 round 1).

    **Every name is held to `_PROJECT_SLUG` here, where registry data
    enters**, and a name that fails it is dropped. `id_pattern_for` puts
    these names into a regex unescaped and relies on that rule. The literal
    list this replaces met it by construction; the registry is written by
    another repository, whose schema test checks only that `kb_project` is
    truthy, so a non-string value would otherwise fail `sorted()` at import
    and stop the Library, and a string holding a regex metacharacter would
    silently widen `ID_PATTERN` (both reviewers, PR #307 round 1)."""
    snap = projects._snapshot()
    kb, scope, legacy = snap.kb_slugs, snap.scopes, snap.kb_legacy
    if kb is None or scope is None or legacy is None:
        return None
    return frozenset(n for n in kb | scope | legacy
                     if isinstance(n, str) and _PROJECT_SLUG.fullmatch(n))


# Computed once, at import, from whatever the registry says at that moment —
# used ONLY by `validate_frontmatter`'s `catalogue=None` default, for a
# direct caller with no knowledge tree in hand and no catalogue built for
# it. `project_catalogue` never reads this pair: it reads the registry LIVE,
# through `_registry_projects()`, on every call, so a registry edit reaches
# it without a restart the way every other view in this module does. This
# pair is fixed for a process's whole life, matching the literal
# `ALLOWED_PROJECTS`/`ID_PATTERN` pair it replaces, which was equally fixed.
_BASELINE = _registry_projects() or frozenset()
ID_PATTERN = id_pattern_for(_BASELINE)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Where the knowledge tree lives whose `projects/` directories widen the
# baseline above — unset in the live job until this change's plist edit
# deploys (`issue_01M3NS89KVRXB368VA4TJ38MDG`, comment 4301). Read once at
# import, matching `reference.py`'s `LIBRARY_KB_INDEX`. **Unset or unreadable
# admits the baseline only** — today's behaviour — so deploying this code
# ahead of the plist change is safe.
KNOWLEDGE_ROOT = os.environ.get("LIBRARY_KNOWLEDGE_ROOT")


class ProjectCatalogue(NamedTuple):
    """The projects one knowledge tree admits. `allowed` is the registry's
    admitted set (platform/decisions/0097 point 1) plus the tree's
    REGISTERED `projects/` directories (and a `pending` name not yet on disk,
    admitted on the same registry-membership terms); `id_pattern` is built
    from `allowed`; `refused` maps each directory entry or `pending` name
    that was NOT admitted to why."""
    allowed: frozenset
    id_pattern: re.Pattern
    refused: dict


def _syntax_and_privacy_refusal(name: str) -> str | None:
    """Why `name` cannot be a project slug on syntax and privacy grounds
    alone, irrespective of whether it is a registered project: a retired
    scope, a private crew, or a name that is not a plain lowercase
    directory name. `None` when none of these refuses it.

    Shared by `_name_refusal` (which additionally requires registry
    membership, platform/decisions/0097 point 4) and `project_dir_refusal`
    (which does not, when the registry cannot be read)."""
    if name in RETIRED_PROJECTS:
        return (f"'{name}' is a retired scope (ARCH-438); nothing may be filed "
                f"under it")
    # Checked before the slug rule, so a private crew's name is refused for
    # being private rather than merely for its casing.
    #
    # `private_crew_names()`, not `private_repos()`: the latter collects only
    # a private project's slug and its `repos`, so an alias, a `display`
    # name, a `crew` name or a `home_dir`/`dirs` basename would validate here
    # as an ordinary project while agent-platform's own validator still
    # refuses it (reviewer finding, PR #160). `None` — the registry could
    # not be read — is not a private-crew match either way; the caller's own
    # registry-membership check (or its absence) decides that case.
    private = projects.private_crew_names()
    if private is not None and name.casefold() in private:
        return (f"'{name}' is a private crew (platform/decisions/0043); none "
                f"of its content may reach the Library")
    if not _PROJECT_SLUG.fullmatch(name):
        return (f"'{name}' is not a plain lowercase directory name (expected "
                f"[a-z][a-z-]*, the id syntax's project segment)")
    return None


def _name_refusal(name: str) -> str | None:
    """Why `name` cannot be a project slug whatever is on disk, or None.

    Refuses a name outside platform/decisions/0097 point 1's registry set
    WHETHER OR NOT THE REGISTRY IS READABLE (point 4) — used for a `pending`
    name, which has no on-disk directory for `project_dir_refusal` to trust
    instead when the registry cannot be read."""
    why = _syntax_and_privacy_refusal(name)
    if why:
        return why
    admitted = _registry_projects()
    if admitted is None:
        return (f"'{name}' cannot be admitted: the project registry could "
                f"not be read, so an unregistered name cannot be created "
                f"(platform/decisions/0097)")
    if name not in admitted:
        return f"'{name}' is not a registered project (platform/decisions/0097)"
    return None


def project_dir_refusal(root, name: str) -> str | None:
    """Why `<root>/projects/<name>` is not a project directory, or None when
    it is. `root` is the knowledge tree being validated, never assumed to be
    `KNOWLEDGE_ROOT`.

    When the registry can be read, `name` must also be in its admitted set
    (platform/decisions/0097 point 4), the same requirement `_name_refusal`
    applies to a `pending` name. When it cannot, an on-disk directory is
    admitted anyway, trusting what the knowledge tree already holds rather
    than refusing every document under it.
    """
    why = _syntax_and_privacy_refusal(name)
    if why:
        return why
    path = Path(root) / "projects" / name
    if not path.is_dir():
        return f"'{name}' is not a directory"
    try:
        path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return f"'{name}' is a symlink pointing outside the knowledge tree"
    admitted = _registry_projects()
    if admitted is not None and name not in admitted:
        return f"'{name}' is not a registered project (platform/decisions/0097)"
    return None


def project_catalogue(root, pending: str | None = None) -> ProjectCatalogue:
    """Read the projects the knowledge tree at `root` holds.

    `root` of `None` — `KNOWLEDGE_ROOT` unset — admits the baseline alone,
    with `pending` ignored: that is today's behaviour, on purpose, so this
    code deploys safely before the tree is wired up. Given a real `root`, a
    missing `projects/` under it likewise admits the baseline alone. `pending`
    names a directory a write is about to create; when it is not already on
    disk, it is admitted only when the registry names it
    (platform/decisions/0097 point 4) — never on its name alone.

    When the registry cannot be read, the baseline is empty and every
    on-disk directory is admitted regardless of registry membership
    (`project_dir_refusal`'s fallback), while every `pending` name is
    refused (`_name_refusal` has no on-disk directory to trust instead).

    Reads the registry LIVE, through `_registry_projects()`, never the
    import-time `_BASELINE` — this is the dynamic half of the pair
    `_BASELINE`/`ID_PATTERN` describe; see their own comment."""
    allowed = set(_registry_projects() or frozenset())
    refused: dict[str, str] = {}
    names: list[str] = []
    if root is not None:
        try:
            names = sorted(p.name for p in (Path(root) / "projects").iterdir())
        except OSError:
            names = []
        for name in names:
            why = project_dir_refusal(root, name)
            if why is None:
                allowed.add(name)
            else:
                refused[name] = why
        if pending and pending not in names:
            why = _name_refusal(pending)
            if why is None:
                allowed.add(pending)
            else:
                refused[pending] = why
    for name in RETIRED_PROJECTS | (projects.private_repos() or frozenset()):
        if root is not None:
            refused.setdefault(name, project_dir_refusal(root, name))
        else:
            refused.setdefault(name, _name_refusal(name))
    return ProjectCatalogue(frozenset(allowed), id_pattern_for(allowed), refused)


def catalogue_for_frontmatter(fm) -> ProjectCatalogue:
    """The catalogue a request's frontmatter should validate against:
    `KNOWLEDGE_ROOT`'s tree, with the frontmatter's own `project` admitted by
    name when this write is the one creating it."""
    pending = fm.get("project") if isinstance(fm, dict) else None
    return project_catalogue(
        KNOWLEDGE_ROOT, pending=pending if isinstance(pending, str) else None)


def validate_frontmatter(fm, catalogue: ProjectCatalogue | None = None,
                         roster: frozenset[str] | None = None,
                         operator_marker: str | None = None) -> list[str]:
    """Structural errors only — empty list means the frontmatter validates.

    `catalogue` is the project set of the knowledge tree the document lives in
    (`project_catalogue`/`catalogue_for_frontmatter`); without one, the
    import-time baseline `_BASELINE`/`ID_PATTERN` applies.

    `roster` is the names `GET /roster` currently serves. Without one —
    `None`, not an empty set — an `audience` or `binds_agents` value is left
    unchecked, matching a call with no live roster to offer;
    `frontmatter_needs_roster` is the same gate a caller uses to decide
    whether reading one at all is worth a database round trip. Given a
    roster, every string either field names must be on it, refused by name;
    an EMPTY roster — the table exists and reads clean, with zero rows —
    refuses outright, naming the cause instead of checking any value. A
    roster that cannot be read at all is a database fault, not an empty
    roster: it never reaches this function as one, because its caller's
    existing database-fault handling turns it into the service's ordinary
    error response first, the same path any other such fault takes.

    `operator_marker` is the operator's own roster name, read by the caller
    (`operator_marker.read_marker`) rather than by this function — this
    module takes it as a value, the same way it takes `roster` as one,
    rather than importing the config reader itself. A `binds_agents` value
    equal to it, or in `NON_AGENT_GROUPS`, is refused with the person wording
    before roster membership is even checked, since `binds_agents` names what
    obeys a document and a person or a group is an audience rather than an
    agent, even though the roster carries all of them alongside the real
    agents (`load_crew_registry.py` seeds it from the same superset
    `validate.py`'s own `ALLOWED_AUDIENCE` is). `audience` is unaffected: a
    document may still address a person or a group. `binds_agents_needs_
    operator_marker` is the gate a caller uses to decide whether reading the
    marker at all is worth it.

    Ported from `knowledge/scripts/validate.py`'s function of the same name,
    with five differences the network-facing route this feeds requires and
    the source function doesn't:

    - `last_reviewed` is checked as a string here rather than also accepting
      a `datetime.date`: the source function's second branch exists because
      `yaml.safe_load` auto-converts an unquoted `YYYY-MM-DD` scalar to a
      `date` object, and this module never calls `yaml.safe_load` — every
      caller here sends already-parsed JSON, whose date fields arrive as
      strings by construction.
    - `kind`/`project`/`status`/`visibility` are type-checked before the
      membership test, where the source isn't. `in` against a frozenset
      hashes the probe, so an unhashable value (a JSON list or object)
      would otherwise raise `TypeError` uncaught. The source never needed
      the guard — it only ever sees `yaml.safe_load` output from documents
      already merged into a git-reviewed repo; this route sees live network
      input from any credential holding `reference.admit`.
    - A NON-STRING `id` is an error here, where the source lets it pass: its
      presence loop sees the key and its format check is guarded by
      `isinstance`, so `{"id": 123}` validates clean there. That gap is
      harmless at the source, which only ever sees `yaml.safe_load` output
      and where a real id contains `/` and therefore always parses as a
      string. It is not harmless on a route that takes JSON directly, and the
      checkout clause cannot look a document up by a value that is not an id.
    - `audience` and `binds_agents` are checked against a ROSTER PASSED IN
      rather than a module-level set: the source reads its own
      `ALLOWED_AUDIENCE`/`NON_AGENT_AUDIENCE` literals, and this route reads
      the Library's own `roster_agents` table instead, because that table is
      the one a `PUT /roster/{name}` write can change without either side
      redeploying (`issue_01M34SYVH2M48K2S0FQK3K6REY`).
    - `NON_AGENT_AUDIENCE` splits in two here: `NON_AGENT_GROUPS` holds the
      names this module can carry as a literal, since this repository
      carries no personal names (library#228), and the operator's own
      marker is passed in separately as `operator_marker`, read from the
      operator config the same way `load_crew_registry.py`'s roster load
      already reads it.
    """
    if not isinstance(fm, dict):
        return ["frontmatter is not a JSON object"]
    errs = []
    for field in REQUIRED:
        if field not in fm or fm[field] in (None, ""):
            errs.append(f"missing required field: {field}")
    # **`in` AGAINST A FROZENSET HASHES THE PROBE** (security-reviewer, PR #42
    # — the same bug class `app.py`'s `privacy_class` gate was hardened
    # against in PR #4). `validate.py`'s own version of this check never
    # needed the guard: it only ever sees `yaml.safe_load` output from
    # documents already merged into a git-reviewed repo. This route is new —
    # it's live network input from any `reference.admit` credential, and an
    # unhashable value (a JSON list or object where a string was expected)
    # would otherwise raise `TypeError` uncaught rather than return a
    # validation error, a 500 on caller-controlled JSON reachable by
    # essentially every non-`approved_client` token in the fleet.
    project_allowed = catalogue.allowed if catalogue else _BASELINE
    id_pattern = catalogue.id_pattern if catalogue else ID_PATTERN
    for field, allowed in (("kind", ALLOWED_KINDS), ("project", project_allowed),
                           ("status", ALLOWED_STATUSES),
                           ("visibility", ALLOWED_VISIBILITY)):
        if field not in fm:
            continue
        if not isinstance(fm[field], str) or fm[field] not in allowed:
            reason = None
            if field == "project" and catalogue and isinstance(fm[field], str):
                reason = catalogue.refused.get(fm[field]) or _name_refusal(fm[field])
            suffix = f" ({reason})" if reason else ""
            errs.append(f"invalid {field}: {fm[field]!r}{suffix}")
    doc_id = fm.get("id")
    # A NON-STRING id validated clean before this: the presence loop above sees
    # the key, and the format check below was guarded by `isinstance`, so
    # `{"id": 123}` skipped validation entirely and was answered may_land true.
    # Found while adding the checkout clause, which cannot look up a document by
    # a value that is not an id.
    if "id" in fm and not isinstance(doc_id, str):
        errs.append(f"id must be a string: {doc_id!r}")
    elif isinstance(doc_id, str) and not id_pattern.match(doc_id):
        errs.append(f"invalid id format (expected <project>/<kind>/<slug>): {doc_id}")
    for field in LIST_FIELDS:
        if field in fm and not isinstance(fm[field], list):
            errs.append(f"{field} must be a list")
    lr = fm.get("last_reviewed")
    if lr is not None and not (isinstance(lr, str) and ISO_DATE.match(lr)):
        errs.append(f"last_reviewed must be YYYY-MM-DD: {lr!r}")
    # Roster membership — only when a roster was actually given, so a caller
    # validating structure alone (`frontmatter_needs_roster` said no database
    # round trip was needed) still gets a plain structural answer rather than
    # an empty-roster refusal it never asked for.
    if roster is not None:
        for field, wording in ROSTERED_FIELDS:
            values = fm.get(field)
            if not (isinstance(values, list)
                    and any(isinstance(v, str) for v in values)):
                continue
            if not roster:
                errs.append(f"{field}: the roster could not be read or is "
                            f"empty")
                continue
            for v in values:
                if not isinstance(v, str):
                    continue
                # Checked before roster membership, so a person or a group
                # named in `binds_agents` gets the person wording rather
                # than passing membership on the strength of being on the
                # roster too — the roster carries `NON_AGENT_GROUPS` and
                # the operator marker alongside the real agents.
                if field == "binds_agents" and (
                        v in NON_AGENT_GROUPS or v == operator_marker):
                    errs.append(
                        f"binds_agents names a person or a group, not an "
                        f"agent: {v}")
                    continue
                if v not in roster:
                    errs.append(f"{wording}: {v}")
    return errs


# The roster names denoting a group rather than a dispatchable agent — ported
# from `knowledge/scripts/validate.py`'s `NON_AGENT_AUDIENCE`
# (`issue_01M34SYVH2M48K2S0FQK3K6REY`), minus the operator's own marker,
# which this repository holds as data (`operator_marker.read_marker`) rather
# than as a literal (library#228). `audience` may still name any of these;
# `binds_agents` names what obeys a document, and a group is an audience
# rather than an agent. The roster carries all of them alongside the real
# agents (`load_crew_registry.py` seeds it from the same superset
# `validate.py`'s own `ALLOWED_AUDIENCE` is), so a plain membership test
# would pass them into `binds_agents` unrefused.
#
# `consultant` is NOT here, though the source listed it: it names
# `agent-platform/agents/consultant`, a dispatchable agent, and every
# document addressing `audience: [consultant]` addresses that agent. Listing
# it made the agent unbindable by any instruction block
# (`issue_01M350YE8J0KFQ9P73VZQ4CWBD`).
NON_AGENT_GROUPS = frozenset({"team", "contractor"})

# Shared by `validate_frontmatter`'s roster clause and
# `frontmatter_needs_roster` below, so the two agree on what "checking a
# field against the roster" means without saying so twice. The wording for
# `audience` is the indexer's own (`agent-platform/lib/knowledge/knowledge.py`,
# `audience value not in roster: {value}`); `binds_agents` takes the same
# shape as the indexer's own refusal for a name the roster does not carry,
# `binds_agents names an unknown agent: {value}`.
ROSTERED_FIELDS = (
    ("audience", "audience value not in roster"),
    ("binds_agents", "binds_agents names an unknown agent"),
)


def frontmatter_needs_roster(fm) -> bool:
    """Whether `validate_frontmatter(fm, roster=...)` would actually check a
    roster — true when `audience` or `binds_agents` is a list carrying at
    least one string. A caller uses this to decide whether reading the
    roster at all is worth a database round trip: a frontmatter naming
    neither field validates the same whether `roster` is given or not."""
    if not isinstance(fm, dict):
        return False
    return any(
        isinstance(fm.get(field), list)
        and any(isinstance(v, str) for v in fm[field])
        for field, _ in ROSTERED_FIELDS)


def binds_agents_needs_operator_marker(fm) -> bool:
    """Whether `validate_frontmatter(fm, operator_marker=...)` would
    actually compare `binds_agents` against the marker — true when
    `binds_agents` is a list carrying at least one string. A caller uses
    this to decide whether reading the marker at all is worth it:
    `audience` never needs it, since a document may still address the
    operator."""
    if not isinstance(fm, dict):
        return False
    values = fm.get("binds_agents")
    return isinstance(values, list) and any(isinstance(v, str) for v in values)


# The checkout clause. `validate_frontmatter` answers the first of the two
# questions this route asks; this answers the second.
#
# **It takes the holder as a value rather than reading the database**, so this
# module keeps the property its docstring already claims for YAML: no
# dependency beyond `re`. Importing `lease` here would pull in `store`, which
# refuses to resolve a database unless one is named — turning a pure validator
# into something that cannot be imported without configuration. The route does
# the lookup and passes the answer down.
CLAUSE_FRONTMATTER = "frontmatter"
CLAUSE_CHECKOUT = "checkout"


def check_checkout(doc_id: str, *, held_by: str | None, owner: str) -> list[str]:
    """Whether `owner` may write `doc_id` under the checkout discipline —
    structural errors only, an empty list meaning the clause passes.

    `held_by` is the holder of the live checkout on the document, or `None`
    when there is none. **A DEAD checkout is None here**, matching how
    `lease.is_dead` is read everywhere else in this service: an expired hold
    is the row still occupying the document, and the next writer is entitled
    to take it.

    **THIS ASKS WHETHER A CHECKOUT IS HELD, NOT WHETHER THE CALLER IS WHO IT
    SAYS IT IS.** `owner` is the same coordination label `_claim_owner`
    resolves everywhere else, and that function's docstring is explicit that it
    is not an authorization principal. Binding a claimed owner to an
    authenticated identity needs the Library's client authentication
    (`issue_01M0VPY5GVK3WAFFV3GJCJTAM4`), which does not exist. So this clause
    enforces ADR-0020's discipline — a write requires a checkout, and not over
    somebody else's — and it establishes no authorship. **A caller that names
    another holder still fails, because it must match the row; a caller that
    names ITSELF falsely gains only what its token already allowed.**
    """
    if held_by is None:
        return [f"no live checkout is held on {doc_id}; take one before writing"]
    if held_by != owner:
        # Naming the holder is what lets a caller schedule against it rather
        # than retry blindly, matching `_checkout_refusal`. The route gates
        # reaching this clause at all on `reference.read`, so a caller that
        # gets here could read the checkout through the status route anyway.
        return [f"{doc_id} is checked out to {held_by}, not {owner}"]
    return []

