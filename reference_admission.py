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

**THIS ROUTE IS CALLED ON THE HUMAN PATH AND BYPASSED ON THE AGENT PATH.**
That asymmetry is the whole state of this split, and it is more dangerous than
an uncalled gate, because the route has real traffic and looks exercised to
anyone who checks whether it is live.

`convivy-orient`'s in-window KB editor calls it on every save: `kb_write_api.py`
builds a partial frontmatter, calls `library_client.check_reference_admission`,
returns 422 on `may_land: false`, and otherwise commits through
`kb_commit.commit_document`, which shells to `agentctl kb checkout` and
`agentctl kb apply` and returns only once the write has landed on `origin/main`.
That caller has been live since 2026-08-25. Writes DO go through this route.

Nothing in `agent-platform` calls it. `agentctl kb apply`, `scribe apply` and
`triage.py` validate locally and push; none of them asks. So every
agent-initiated write bypasses the check that every Orient-initiated one passes.

Both halves are claims about other repositories and either can stop being true
without anything here changing. Run both rather than trusting this paragraph:

    grep -rnE "reference/admission|reference_admission" ~/Convivy/agent-platform/{bin,lib}
    grep -rnE "reference/admission|reference_admission" ~/Convivy/convivy-orient

On 2026-08-29 the first returned nothing; the second returned `library_client.py`
and `kb_write_api.py`.

**AN EARLIER VERSION OF THIS DOCSTRING SAID NOTHING CALLED THIS ROUTE AND GAVE
ONLY THE FIRST COMMAND AS ITS PROOF.** The claim was about every caller
everywhere; the check looked in one repository. Orient's caller had already been
live for a day when it was written. **A check narrower in scope than the claim it
supports reports success for the part it looked at**, which is how this survived
three days, two crews, and a steward ruling that repeated it.

**WHAT IS GATED TODAY, PRECISELY.** The checkout clause itself is no longer
optional — every request evaluates it, and one that names no `owner` is
refused rather than answered on frontmatter alone. What is still gated is
*who calls this route at all*: `agentctl` doesn't yet, so an agent-initiated
write is still not checked (the asymmetry two paragraphs up). `clauses_checked`
on the response still says which clauses actually ran — read that rather than
inferring enforcement from a green answer. Orient's editor sends no `owner` as
of this writing, so this change refuses its saves until Orient starts sending
one; that deploy-ordering question is Orient's crew's to sequence, not this
route's.

**THE CHECKOUT CLAUSE HAS LANDED, WHICH WAS THE PRECONDITION FOR WIRING
`kb apply` TO CALL THIS.** The steward's revised ordering (`issue_01M0YAS4S64P5TXN7S2C17GHF6`, comment
1204, superseding the ruling in comment 1197): `kb checkout` returns a
content-sha without minting a competing lease, then this file gains the checkout
clause, then `kb apply` calls admission and honours the answer. Wiring before the
clause produces a gate that is present, correct, called, and blind to the case it
was called for, which is worse than an uncalled one because it leaves nothing
open to find. The failure contract for that call is comment 1202: hard refuse on
refusal, on unreachability and on any other error, with no override flag.

That is a half-wired split rather than a design that changed its mind, and the
distinction is invisible from inside this file: everything here works, the
tests pass, and the absence lives entirely in another repo. Written down
because inferring it from an absence of callers requires knowing to look, and
because this exact shape — a control that is present, correct and unreached —
has cost the fleet repeatedly: guards pointing at moved constants, a test
mocking a server into accepting what it rejects, a filing path returning 400 to
workers who believed they had filed.

Wiring the Bosun's path is the Bosun's half, and
`issue_01M08YECXZ68NG1WWHG0NRD086` carries the state.
`issue_01KZQA8A1SEEWSNYV5H8N0EQW7`'s branch protection waits on it: direct
pushes to `knowledge/main` cannot be blocked while a push path exists that
never asks, and `agentctl kb apply` is that path.

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
something else). Its WARNINGS (roster drift on `audience`, an unresolved
`depends_on`) are drift-prone rather than structural and are not part of an
admission decision — they don't stop the document from indexing.

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
})

# Kept as one flat set rather than reproducing `validate.py`'s per-entry
# history — that commentary belongs with the registry it explains, not
# duplicated a second time here. `knowledge/scripts/validate.py`
# (`ALLOWED_PROJECTS`) is the source of truth for what's actually current.
#
# **THIS IS THE BASELINE, NOT THE WHOLE SET** (agent-platform#685, ported here
# by `issue_01M3NS89KVRXB368VA4TJ38MDG` so the two validators agree).
# `project_catalogue()` below adds every valid `projects/<name>/` directory the
# knowledge tree at `KNOWLEDGE_ROOT` actually holds, so an adopter's own
# project — one Convivy has never heard of — validates without a row here.
# The baseline stays so every project below keeps validating even when
# `KNOWLEDGE_ROOT` is unset or its directory is absent from the tree.
ALLOWED_PROJECTS = frozenset({
    "constance", "vivy", "sous-chef", "agent-platform",
    # constance-landing (knowledge 6f9ac60, 2026-08-25): the constance.team apex
    # landing page's own project, separate from `constance` so a dispatch against
    # convivy/constance-landing resolves its narrow profile by default. Added here
    # a day late, and the lateness is the point — see the drift note below.
    "constance-landing",
    "hollis", "selector", "convivy-app-kit", "convivy-services",
    "nyh-studio",
    # placard (crew created 2026-09-05): registered on the knowledge side only,
    # so this port refused every `placard` admission until now. A project needs
    # adding to `ID_PATTERN` below as well as to this set — `validate.py`'s own
    # comment says "list AND ID_PATTERN below", and adding it here alone leaves
    # every `placard/...` document id still rejected.
    "placard",
    # signals (2026-09-16, the operator): Convivy's internal product for commercial
    # signal, briefings and message boards. An open crew that authors under
    # projects/signals/. Added to validate.py by knowledge#540. It needs this
    # set AND ID_PATTERN below, the same as placard.
    "signals",
    # archy (2026-10-02, the operator): an agentic operations system for small
    # businesses that a specialist installs; an open crew. Added to validate.py
    # by knowledge#722, which turned this repo's drift check red until it
    # landed here. ID_PATTERN is derived from this set, so one entry covers both.
    "archy",
    # shipwright (2026-09-06, the operator): the crew that works Convivy Builder, with a
    # slug of its own for its runbooks and its dispatch profile.
    #
    # **A SLUG IS NOT A BEAD.** shipwright must not write the Builder's bead —
    # that part is the operator's and stands — but a crew can carry documents under its
    # own project while working another product's bead. An earlier comment here
    # asserted shipwright was deliberately excluded from the slug on the
    # strength of the bead rule; that inference was wrong, and nothing about
    # its slug had in fact been decided at the time. The distinction is written
    # down here because conflating the two is what produced that error.
    "shipwright",
    # www (2026-09-12): convivy/www, the public convivy.com site, given its own
    # project so its dispatch profile can exclude `kind: reference` entirely.
    # validate.py carries the reasoning.
    "www",
    # vivy-landing (2026-10-02): convivy/vivy-landing, the public vivy.rentals
    # site, given its own project for the same reason as www, so its dispatch
    # profile can exclude `kind: reference`. Added to validate.py by
    # knowledge#730; ID_PATTERN is derived from this set.
    "vivy-landing",
    "substrate", "knowledge", "convivy-orient", "keel", "library", "svc",
    "deck", "write-better",
    "company", "platform", "commercial", "engineering-practice",
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


# The baseline set's pattern, for a caller with no knowledge tree in hand.
ID_PATTERN = id_pattern_for(ALLOWED_PROJECTS)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Where the knowledge tree lives whose `projects/` directories widen the
# baseline above — unset in the live job until this change's plist edit
# deploys (`issue_01M3NS89KVRXB368VA4TJ38MDG`, comment 4301). Read once at
# import, matching `reference.py`'s `LIBRARY_KB_INDEX`. **Unset or unreadable
# admits the baseline only** — today's behaviour — so deploying this code
# ahead of the plist change is safe.
KNOWLEDGE_ROOT = os.environ.get("LIBRARY_KNOWLEDGE_ROOT")


class ProjectCatalogue(NamedTuple):
    """The projects one knowledge tree admits. `allowed` is the baseline plus
    the tree's valid `projects/` directories (and a `pending` name not yet on
    disk); `id_pattern` is built from `allowed`; `refused` maps each directory
    entry or `pending` name that was NOT admitted to why."""
    allowed: frozenset
    id_pattern: re.Pattern
    refused: dict


def _name_refusal(name: str) -> str | None:
    """Why `name` cannot be a project slug whatever is on disk, or None."""
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
    # refuses it (reviewer finding, PR #160).
    private = projects.private_crew_names()
    if private is None:
        if name not in ALLOWED_PROJECTS:
            return (f"'{name}' cannot be admitted: the project registry could "
                    f"not be read, so private crews cannot be told apart "
                    f"(platform/decisions/0043)")
    elif name.casefold() in private:
        return (f"'{name}' is a private crew (platform/decisions/0043); none "
                f"of its content may reach the Library")
    if not _PROJECT_SLUG.fullmatch(name):
        return (f"'{name}' is not a plain lowercase directory name (expected "
                f"[a-z][a-z-]*, the id syntax's project segment)")
    return None


def project_dir_refusal(root, name: str) -> str | None:
    """Why `<root>/projects/<name>` is not a project directory, or None when
    it is. `root` is the knowledge tree being validated, never assumed to be
    `KNOWLEDGE_ROOT`."""
    why = _name_refusal(name)
    if why:
        return why
    path = Path(root) / "projects" / name
    if not path.is_dir():
        return f"'{name}' is not a directory"
    try:
        path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return f"'{name}' is a symlink pointing outside the knowledge tree"
    return None


def project_catalogue(root, pending: str | None = None) -> ProjectCatalogue:
    """Read the projects the knowledge tree at `root` holds.

    `root` of `None` — `KNOWLEDGE_ROOT` unset — admits the baseline alone,
    with `pending` ignored: that is today's behaviour, on purpose, so this
    code deploys safely before the tree is wired up. Given a real `root`, a
    missing or unreadable `projects/` under it likewise admits the baseline
    (never refuses everything). `pending` names a directory a write is about
    to create; when it is not already on disk, it is admitted on its name
    alone.
    """
    allowed = set(ALLOWED_PROJECTS)
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


def validate_frontmatter(fm, catalogue: ProjectCatalogue | None = None) -> list[str]:
    """Structural errors only — empty list means the frontmatter validates.

    `catalogue` is the project set of the knowledge tree the document lives in
    (`project_catalogue`/`catalogue_for_frontmatter`); without one, the
    baseline `ALLOWED_PROJECTS`/`ID_PATTERN` apply.

    Ported from `knowledge/scripts/validate.py`'s function of the same name,
    with three differences the network-facing route this feeds requires and
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
    project_allowed = catalogue.allowed if catalogue else ALLOWED_PROJECTS
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
    return errs

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

