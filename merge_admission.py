"""The merge-admission evaluator — one function over `(repo, pr, head_sha)`
deciding whether reviews, checks and the review-round cap admit it (CQ·218,
issue_01M5FM2K55J8RCPK218Y7JSHH2, under epic issue_01M52P72JPNM24XG219NQF9PFS).

**Not wired to a route.** `POST /merge/admission` (`app.py`) is still the
stub CQ·219 will replace; this module is the evaluator that change calls,
built and tested standalone first.

**"Attested by the run register", never "proved".** A role's review counts
only when its record is run-verified AND `verdict_bound` (library CQ·217):
the run register's own `meta.json` says this run posted this verdict on
this PR, and no later write on that key may claim a different one. That
attestation is self-reported by the run's own process — a same-account
writer could still forge it before the run posts — so an admission from
this module says the verdict is attested by the run register, never that
the dispatched reviewer in fact approved.

## Reviews

For `reviewer` and `security-reviewer`: the role's LATEST run-verified,
verdict-bound record for the pull request, by `updated_at`, across every
`reviewed_ref` the PR has ever been reviewed at — not just the current
head. (Two different runs reviewing the same head each get their own row,
keyed on `run_id`; `write_review_record`'s supersede-on-identical-key only
ever replaces a row the SAME run posted twice, so "latest" here is a plain
read across rows, never a supersede.) A role's row is uncounted — an
`unverified` or non-verdict-bound write never registers at all, since the
query that selects it filters on both.

- No row at all: `kind="review"`, not retryable — this role has never
  reviewed this pull request. When the role's only run-verified records
  are unbound (posted before verdict binding went live on 2026-10-25,
  library CQ·217), the detail names verdict binding and says a fresh
  review clears it, rather than claiming no review was ever posted.
- Its `reviewed_ref` is not `head_sha`: `kind="stale_review"`, retryable —
  the pull request has moved since this role's last review. On
  `repo == "convivy/knowledge"` an `APPROVE` latest record carries from its
  stamped commit to `head_sha` when platform/decisions/0078's three
  conditions hold (`_carry_reason`, ported from agent-platform's
  `kb_merge.carry_blocked`/`carry_for_role`): the same changed path set
  against each commit's own merge base with `origin/main`, each path mode-
  and blob-identical between the stamped commit and `head_sha`, and each
  path's main-side original identical between the two merge bases. A role
  whose latest record is not `APPROVE` never carries — there is no
  fallback to an earlier stamp the role itself superseded. Scoped to
  `convivy/knowledge` because decision 0078 grants the carry to `kb/`
  pull requests there and this evaluator's `(repo, pr, head_sha)` inputs
  carry no branch name to tell a `kb/` branch from any other in that same
  repo; every other repo keeps the plain stale-review refusal. Within that
  repository the carry is narrowed further to a Library proposal's shape
  (`_carry_shape_blocked`): the head is one commit whose only parent is the
  checkout's `origin/main`, the stamped commit has one parent, and both
  carry a `KB-Write:` trailer. That stands in for the `kb/` branch test the
  inputs cannot make, and keeps condition 3 from judging a head against a
  `main` older than the one it was built on. A missing
  git object (the stamped commit force-pushed away, an uncomputable merge
  base, any other git failure) never reads as "carries" — it stays
  `stale_review`, retryable, with the missing object named in the detail,
  the same as a condition that fails outright. A carried approval is
  still only attested by the run register (the paragraph above): it is
  evidence the SAME stamp a run once posted at an earlier commit still
  applies, never a claim that any reviewer looked at `head_sha` itself.
- It is at `head_sha` but its verdict is not `APPROVE` (a later
  `REQUEST_CHANGES` at the same head, posted by a different run than the
  one that approved it, outranks that approval because it is later):
  `kind="review"`, not retryable.

## Checks

Every `ci_tracked.tracked()` pair for `repo` marked `on_pull_request` needs
a `test-result` signal at `head_sha`, ingested by `ci-result-capture`
specifically (`POST /signals` lets any `signal.create` holder write the
same shape; a row from another credential is never read here, whatever its
content). No row at all is `kind="check_pending"`, retryable — the check
may simply not have finished. A row whose latest conclusion is not
`success` is `kind="check"`, not retryable — it ran and did not pass.

## The round cap

Reproduces `agent-platform`'s `agent_policy.review_round_cap` and
`kb_merge._round_cap` (comment 5586 on this item) without importing
agent-platform's code — the same deliberate duplication
`reference_admission.py`'s own docstring explains for
`knowledge/scripts/validate.py`, applied here to
`lib/agent-platform/agent_policy.py`. The cap is read from
`projects/platform/policy/agent-policy.yaml` at the knowledge repository's
`origin/main`, classifying the pull request's changed files the same way:
a positive floor (an instruction file, or a decision whose audience names
an agent role) takes `review_rounds_code`; anything else, a tie-break
included, takes `review_rounds_per_role`. The cap for the whole pull
request is the LOWEST cap among the current head and every distinct
`reviewed_ref` a run-verified, verdict-bound record names — a later commit
can only lower the cap, never raise it, matching `kb_merge._round_cap`.

A role exceeding the cap (counted as distinct run-verified, verdict-bound
`run_id`s, never distinct commits — a re-review at an unchanged head is
still a round) is `kind="round_cap"`, not retryable.

**The policy file unreadable, unparsable, or missing a required line
refuses the whole admission** (`kind="unreadable"`) rather than falling
back to `agent_policy.load`'s own permissive-looking Fallback numbers
(comment 5586: "the Library's own rule"). The project registry unreadable
(so the pull-request workflows `repo` is checked against are unknown)
refuses the same way. Reviews and checks are still evaluated and reported
alongside either refusal — every condition is checked, matching
`kb_merge.refusals`'s own reasoning that a refusal should tell the caller
everything it has to fix, not just the first thing.

**Known gap, not reproduced here: `_side_floor`'s decision-audience branch
is a best-effort port**, not byte-for-byte. The original reads the Library's
OWN roster live through `crew_profiles.roster` here (agent-platform's
equivalent calls back out to the Library's `/roster` route, which would be
this same service calling itself); the comparison itself is faithful, but a
roster or operator-marker read that fails here returns "cannot tell" the
same way the original does when ITS read fails, so the two gates can still
disagree during an outage of the exact thing both are reading.

**`LIBRARY_REPOS_ROOT` (default `~/Convivy`)** names the parent directory
of a local checkout this evaluator reads `repo`'s pull-request diff from,
by the same `~/Convivy/<bare-name>` convention `reference.KB_REPO` and
`reference_admission.KNOWLEDGE_ROOT` already use for the one repo they
read — `convivy/knowledge` itself always resolves to `reference.KB_REPO`
rather than this generic fallback, so the two never disagree about where
the knowledge checkout is. No local checkout, or one that cannot be fetched
to `head_sha`, reads as the pull request's changed files being unreadable
— `review_floor_of`'s own tie-break, which takes the LOWER
`review_rounds_per_role` cap rather than refusing outright, matching
`kb_merge._round_cap`'s treatment of a stamp whose head cannot be read.
Never writes to a checkout; every git call here reads only.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import ci_tracked
import crew_profiles
import operator_marker
import reference
import reference_admission
import store

REQUIRED_ROLES = ("reviewer", "security-reviewer")
APPROVE = "APPROVE"

# The credential every check this evaluator trusts must have ingested as —
# any other `signal.create` holder's `test-result` row is invisible here
# (CQ·218's own wording for the "checks" clause).
CI_RESULT_CAPTURE_INGESTED_BY = "ci-result-capture"

GIT_TIMEOUT_SECONDS = 30

# -----------------------------------------------------------------------
# The policy file (ported from agent-platform's `agent_policy.py`)
# -----------------------------------------------------------------------

POLICY_PATH = "projects/platform/policy/agent-policy.yaml"
POLICY_NAME = "platform/agent-policy"
SCHEMA_VERSION = 1
# `agent_policy.load`'s own rule: a readable file with no `review_rounds_code`
# line takes this value rather than being treated as broken.
DECIDED_REVIEW_ROUNDS_CODE = 11

_BOOL_KEYS = ("agents_may_merge_knowledge", "agents_may_merge_knowledge_instructions",
             "agents_may_merge_documents")
_NUMBER_KEYS = ("review_rounds_per_role", "max_concurrent", "max_workers_per_team",
                "max_live_supervisors")
_OPTIONAL_NUMBER_KEYS = ("review_rounds_code",)
_REQUIRED_POLICY_KEYS = ("policy", "schema_version") + _BOOL_KEYS + _NUMBER_KEYS
_POLICY_KEYS = _REQUIRED_POLICY_KEYS + _OPTIONAL_NUMBER_KEYS

_POLICY_LINE = re.compile(r"([a-z_]+): (\S+)")
_POLICY_NUMBER = re.compile(r"[1-9][0-9]{0,3}")


class PolicyNumbers(NamedTuple):
    """The two numbers `review_round_cap` needs — everything else the real
    policy file states is read only well enough to decide whether the file
    is readable at all, matching `agent_policy.load`'s own full validation
    (see `_parse_policy_numbers`)."""
    review_rounds_per_role: int
    review_rounds_code: int


def _git_env() -> dict[str, str]:
    """The environment every git call in this module runs with: every
    `GIT_*` variable dropped, so a caller running inside a git hook reads
    the given root rather than the repository the hook runs in (matching
    `agent_policy.policy_text_at_origin_main` and `kb_merge._git`), plus
    the user's global and system git config pinned to `/dev/null` — a
    divergence from both of those, added because every call here is a
    plumbing read (`show-ref`, `cat-file`, `diff --raw`) whose output must
    not depend on an operator's own `core.*`/`diff.*` settings."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _policy_text_at_origin_main(repo_root: Path) -> str | None:
    """The policy file's text at `origin/main` of the git checkout at
    `repo_root`, or `None` when it cannot be read — ported from
    `agent_policy.policy_text_at_origin_main`. Reads the committed blob, so
    a working-tree edit never shows."""
    env = _git_env()
    base = ["git", "--no-replace-objects", "-C", str(repo_root)]
    try:
        ref = subprocess.run(
            [*base, "show-ref", "--verify", "--hash", "refs/remotes/origin/main"],
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS, env=env)
        sha = ref.stdout.decode("ascii", "replace").strip()
        if ref.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            return None
        done = subprocess.run([*base, "show", f"{sha}:{POLICY_PATH}"],
                              capture_output=True, timeout=GIT_TIMEOUT_SECONDS, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse_policy_numbers(text: str | None) -> PolicyNumbers | None:
    """`review_rounds_per_role`/`review_rounds_code`, or `None` when `text`
    is missing or breaks any rule the policy file's own header states —
    ported from `agent_policy.load`, whose `Fallback` this never returns:
    CQ·218 refuses the whole admission on an unreadable policy (comment
    5586) rather than using `agent_policy.load`'s own fallback numbers."""
    if text is None:
        return None
    values: dict[str, object] = {}
    for line in text.split("\n"):
        if not line.strip(" \t") or line.startswith("#"):
            continue
        match = _POLICY_LINE.fullmatch(line)
        if not match:
            return None
        key, value = match.groups()
        if key not in _POLICY_KEYS or key in values:
            return None
        if key == "policy":
            if value != POLICY_NAME:
                return None
            values[key] = value
        elif key == "schema_version":
            if value != str(SCHEMA_VERSION):
                return None
            values[key] = SCHEMA_VERSION
        elif key in _BOOL_KEYS:
            if value not in ("true", "false"):
                return None
            values[key] = value == "true"
        else:
            if not _POLICY_NUMBER.fullmatch(value) or int(value) > 1000:
                return None
            values[key] = int(value)
    if any(key not in values for key in _REQUIRED_POLICY_KEYS):
        return None
    review_rounds_code = values.get("review_rounds_code", DECIDED_REVIEW_ROUNDS_CODE)
    return PolicyNumbers(review_rounds_per_role=values["review_rounds_per_role"],
                         review_rounds_code=review_rounds_code)


# -----------------------------------------------------------------------
# The review floor (ported from agent-platform's `agent_policy.py`)
# -----------------------------------------------------------------------

_INSTRUCTION_DIRS = frozenset({"agents", "instruction-blocks", "runbooks", "skills", "profiles"})
_INSTRUCTION_KINDS = frozenset({"agent", "skill", "runbook", "instruction-block"})
_INSTRUCTION_NAMES = frozenset({"claude.md", "claude.local.md", "agents.md"})
_REGULAR_MODES = frozenset({"100644", "100755"})

_FM_LINE = re.compile(r"([a-z0-9_-]+): ([^ ](?:.*[^ ])?)")
_FM_KIND = re.compile(r"[a-z0-9-]+")
_FM_AUDIENCE = re.compile(r"\[[a-z0-9-]+(?:, [a-z0-9-]+)*\]")


def _names(segment: str, name: str) -> bool:
    """True when `segment` is `name` in some ASCII letter case — a match
    here GRANTS a kind, so casefold is never used (it would also match a
    look-alike codepoint such as U+017F)."""
    return segment.isascii() and segment.lower() == name


def _nonprintable(text: str, *, spaces_ok: bool = False) -> str | None:
    """The first character that is not printable (tab and newline allowed),
    or None. With `spaces_ok`, any whitespace character is allowed too."""
    return next((c for c in text
                if not (c.isprintable() or c in "\t\n" or (spaces_ok and c.isspace()))),
               None)


def _front_matter_block(text: str) -> tuple[list[str] | None, str | None]:
    """The lines between the front matter's `---` delimiters, or None and
    the reason they do not open and close."""
    lines = text.split("\n")
    if lines[0] != "---":
        return None, "the opening line is not exactly ---"
    end = next((i for i in range(1, len(lines)) if lines[i].startswith("---")), None)
    if end is None:
        return None, "the front matter never closes"
    if lines[end] != "---":
        return None, "the closing line is not exactly ---"
    return lines[1:end], None


def _read_front_matter(text: str) -> tuple[dict[str, str], str | None]:
    """The front matter's keys and None, or no keys and why the rule
    cannot read it."""
    block, problem = _front_matter_block(text)
    if problem:
        return {}, problem
    keys: dict[str, str] = {}
    for number, line in enumerate(block, start=2):
        if "---" in line:
            return {}, f"line {number} contains ---"
        match = _FM_LINE.fullmatch(line)
        if not match:
            return {}, f"line {number} is not exactly `key: value`"
        key, value = match.groups()
        if key in keys:
            return {}, f"line {number} repeats {key}"
        if key == "kind" and not _FM_KIND.fullmatch(value):
            return {}, f"line {number}: kind has a value the rule cannot read"
        if key == "audience" and not _FM_AUDIENCE.fullmatch(value):
            return {}, f"line {number}: audience has a value the rule cannot read"
        keys[key] = value
    return keys, None


def _roster_names(conn: sqlite3.Connection) -> frozenset[str] | None:
    """The names `GET /roster` would answer right now, or `None` when the
    table cannot be read — never raises, since the floor this feeds is a
    tie-break on failure, the same as a `RosterUnavailable` read is for
    agent-platform's own `knowledge.audience_roster`."""
    try:
        return frozenset(row["name"] for row in crew_profiles.roster(conn))
    except sqlite3.Error:
        return None


def _non_agent_names() -> frozenset[str]:
    """`reference_admission.NON_AGENT_GROUPS` plus the operator's own
    marker when it can be read — the roster carries all of them alongside
    the real agents, matching `knowledge.NON_AGENT_AUDIENCE`'s own shape."""
    try:
        marker = operator_marker.read_marker()
    except operator_marker.OperatorMarkerUnavailable:
        return reference_admission.NON_AGENT_GROUPS
    return reference_admission.NON_AGENT_GROUPS | {marker}


def _audience_split(conn: sqlite3.Connection,
                    audience: str | None) -> tuple[list[str], list[str]]:
    """The audience entries naming an agent role, and the entries neither
    an agent role nor a person or group — ported from
    `agent_policy._audience_split`, reading the Library's OWN roster table
    directly rather than calling back out to `GET /roster` over HTTP (this
    service IS the Library). An entry outside the vocabulary cannot be
    told, so the caller takes the tie-break whenever `unknown` is
    non-empty; a roster that cannot be read makes every entry unknown,
    never "no agent role"."""
    if audience is None:
        return [], []
    entries = audience[1:-1].split(", ")
    roster = _roster_names(conn)
    if roster is None:
        return [], list(entries)
    allowed_agents = roster - _non_agent_names()
    roles = [e for e in entries if e in allowed_agents]
    unknown = [e for e in entries if e not in roster]
    return roles, unknown


def _raw_kind(text: str) -> str | None:
    """The floor kind on a raw `kind:` line, for front matter the reader
    cannot parse, or None."""
    block, _problem = _front_matter_block(text)
    if block is None:
        return None
    kinds = [line for line in block if line.startswith("kind:")]
    if len(kinds) != 1 or not kinds[0].startswith("kind: "):
        return None
    value = kinds[0][len("kind: "):]
    return value if value in _INSTRUCTION_KINDS else None


def _raw_decision_audience(conn: sqlite3.Connection, path: str,
                           text: str) -> tuple[list[str], list[str]]:
    """`_audience_split` of a decision's raw `audience:` line, for front
    matter the reader cannot parse."""
    block, _problem = _front_matter_block(text)
    if block is None:
        return [], []
    kinds = [line for line in block if line.startswith("kind:")]
    in_decisions = any(_names(seg, "decisions") for seg in path.split("/")[:-1])
    if not (in_decisions or kinds == ["kind: decision"]):
        return [], []
    audiences = [line for line in block if line.startswith("audience:")]
    if len(audiences) != 1 or not audiences[0].startswith("audience: "):
        return [], []
    value = audiences[0][len("audience: "):]
    if not _FM_AUDIENCE.fullmatch(value):
        return [], []
    return _audience_split(conn, value)


def _unreadable_front_matter_floor(conn: sqlite3.Connection, path: str, text: str,
                                   problem: str, ties: list[str]) -> tuple[bool, str]:
    """`_side_floor`'s verdict for front matter the reader refuses."""
    raw = _raw_kind(text)
    if raw is not None:
        return True, f"{path} has kind {raw} (front matter the rule cannot read: {problem})"
    roles, unknown = _raw_decision_audience(conn, path, text)
    if unknown:
        return False, (f"{path} is a decision whose audience names {', '.join(unknown)}, "
                       f"which is neither an agent role nor a person or group (front "
                       f"matter the rule cannot read: {problem})")
    if roles:
        return True, (f"{path} is a decision whose audience names {', '.join(roles)} "
                      f"(front matter the rule cannot read: {problem})")
    return False, (ties[0] if ties else f"{path} has front matter the rule cannot read: {problem}")


def _side_floor(conn: sqlite3.Connection, path: str | None, text: str | None,
                mode: str | None) -> tuple[bool, str] | None:
    """`(positive, reason)` when this side is a floor member or cannot be
    told, else None — ported from `agent_policy._side_floor`."""
    if not path or mode is None or text is None:
        return False, f"{path or 'a changed file'} cannot be read"
    parts = path.split("/")
    leaf = parts[-1]
    if any(_names(seg, d) for seg in parts[:-1] for d in _INSTRUCTION_DIRS):
        return True, f"{path} sits under an agent-instruction directory"
    if not leaf.lower().endswith(".md"):
        return True, f"{path} is not a markdown document"
    if any(_names(leaf, n) for n in _INSTRUCTION_NAMES):
        return True, f"{path} is named {leaf}"
    ties: list[str] = []
    if mode not in _REGULAR_MODES:
        ties.append(f"{path} is not a regular file (mode {mode})")
    bad = _nonprintable(text, spaces_ok=True)
    if bad is not None:
        ties.append(f"{path} holds the non-printable character {bad!r}")
    keys: dict[str, str] = {}
    if text.startswith("---"):
        keys, problem = _read_front_matter(text)
        if problem:
            return _unreadable_front_matter_floor(conn, path, text, problem, ties)
    kind = keys.get("kind")
    if kind in _INSTRUCTION_KINDS:
        return True, f"{path} has kind {kind}"
    if kind == "decision" or any(_names(seg, "decisions") for seg in parts[:-1]):
        roles, unknown = _audience_split(conn, keys.get("audience"))
        if unknown:
            return False, (f"{path} is a decision whose audience names "
                           f"{', '.join(unknown)}, which is neither an agent role "
                           f"nor a person or group")
        if roles:
            return True, f"{path} is a decision whose audience names {', '.join(roles)}"
        return False, f"{path} is a decision whose audience names no agent role"
    return (False, ties[0]) if ties else None


@dataclass(frozen=True)
class ReviewFloor:
    """Whether a pull request is in the review floor, and what put it
    there — ported from `agent_policy.ReviewFloor`."""
    floor: bool
    positive: bool
    reason: str


@dataclass(frozen=True)
class Change:
    """One changed file as git reports it, both sides — the same shape
    `agent-platform`'s `kb_merge.Change` is, so `review_floor_of` below
    reads it identically."""
    path_before: str | None
    mode_before: str | None
    text_before: str | None
    path_after: str | None
    mode_after: str | None
    text_after: str | None


def review_floor_of(conn: sqlite3.Connection, changes: list[Change] | None) -> ReviewFloor:
    """The review floor for a pull request's changed files, read on both
    sides — ported from `agent_policy.review_floor_of`. `None` means the
    changed files could not be read at all, which is a tie-break, so an
    unreadable pull request never takes the code cap."""
    if changes is None:
        return ReviewFloor(True, False, "the pull request's changed files could not be read")
    found: list[tuple[bool, str]] = []
    for c in changes:
        for path, text, mode in ((c.path_before, c.text_before, c.mode_before),
                                 (c.path_after, c.text_after, c.mode_after)):
            if path is None:
                continue
            got = _side_floor(conn, path, text, mode)
            if got is not None:
                found.append(got)
    positive = [reason for pos, reason in found if pos]
    if positive:
        return ReviewFloor(True, True, positive[0])
    if found:
        return ReviewFloor(True, False, found[0][1])
    return ReviewFloor(False, False, "")


def review_round_cap(policy: PolicyNumbers, floor: ReviewFloor) -> tuple[int, str]:
    """The round cap a pull request takes, and the clause that explains it
    — ported from `agent_policy.review_round_cap`."""
    if floor.positive:
        return policy.review_rounds_code, (
            f"review_rounds_code ({policy.review_rounds_code}) applies, "
            f"because {floor.reason}")
    if floor.floor:
        return policy.review_rounds_per_role, (
            f"review_rounds_per_role ({policy.review_rounds_per_role}) applies, "
            f"because {floor.reason}; that is in the floor only by the "
            f"tie-break, which takes the document cap")
    return policy.review_rounds_per_role, (
        f"review_rounds_per_role ({policy.review_rounds_per_role}) applies, "
        f"because no changed file is in the review floor")


# -----------------------------------------------------------------------
# Local git reads (ported from agent-platform's `kb_merge.py`)
# -----------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """`git -C root args`, bytes out, or `None` on anything that stops it
    from running at all — never raises, since every caller here treats an
    unreadable diff as a tie-break rather than a hard failure."""
    env = _git_env()
    env["GIT_GRAFT_FILE"] = os.devnull
    try:
        return subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args],
                              capture_output=True, timeout=GIT_TIMEOUT_SECONDS, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def _blob(root: Path, mode: str, sha: str) -> str | None:
    """A side's text, or None when it is not strict UTF-8 or is a
    submodule."""
    if mode == "160000":
        return None
    done = _git(root, "cat-file", "blob", sha)
    if done is None or done.returncode != 0:
        return None
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def changes_between(root: Path, base: str, head: str) -> list[Change] | None:
    """Every file `head` changes against `base`, as git reports it
    (rename-aware, from the merge base, as a merge would land it), both
    sides, or `None` when the diff cannot be read at all — ported from
    `kb_merge.changes_between`, returning `None` rather than raising
    `Unreadable`, since `review_floor_of` already treats `None` as the
    tie-break `kb_merge._round_cap` gives an unreadable stamp head."""
    done = _git(root, "diff", "--raw", "-z", "-M", "--no-abbrev", "--no-ext-diff",
               f"{base}...{head}")
    if done is None or done.returncode != 0:
        return None
    fields = done.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    out: list[Change] = []
    i = 0
    while i < len(fields):
        meta = fields[i].decode("ascii", "replace").split(" ")
        if len(meta) != 5 or not meta[0].startswith(":"):
            return None
        mode_a, mode_b, sha_a, sha_b, status = meta[0][1:], meta[1], meta[2], meta[3], meta[4]
        if status[0] not in "AMDTRC":
            return None
        width = 2 if status[0] in "RC" else 1
        raw = fields[i + 1:i + 1 + width]
        if len(raw) != width:
            return None
        i += 1 + width
        try:
            paths = [p.decode("utf-8") for p in raw]
        except UnicodeDecodeError:
            return None
        path_a, path_b = paths[0], paths[-1]
        before = None if mode_a == "000000" else (path_a, mode_a, _blob(root, mode_a, sha_a))
        after = None if mode_b == "000000" else (path_b, mode_b, _blob(root, mode_b, sha_b))
        out.append(Change(
            path_before=before[0] if before else None,
            mode_before=before[1] if before else None,
            text_before=before[2] if before else None,
            path_after=after[0] if after else None,
            mode_after=after[1] if after else None,
            text_after=after[2] if after else None))
    return out


def _local_checkout_root(repo: str) -> Path:
    """The local git checkout this evaluator reads `repo`'s pull-request
    diff from. `convivy/knowledge` always resolves to `reference.KB_REPO`,
    the Library's own canonical knowledge checkout; any other repo falls
    back to `LIBRARY_REPOS_ROOT` (default `~/Convivy`) plus its bare name,
    the same convention `reference.KB_REPO` and
    `reference_admission.KNOWLEDGE_ROOT` already use."""
    if repo == "convivy/knowledge":
        return reference.KB_REPO
    bare = repo.rsplit("/", 1)[-1]
    parent = Path(os.environ.get("LIBRARY_REPOS_ROOT", str(Path.home() / "Convivy")))
    return parent / bare


def _floor_at(conn: sqlite3.Connection, repo: str, ref: str) -> ReviewFloor:
    changes = changes_between(_local_checkout_root(repo), "origin/main", ref)
    return review_floor_of(conn, changes)


def _round_cap_for(conn: sqlite3.Connection, repo: str, head_sha: str,
                   policy: PolicyNumbers, review_refs: set[str]) -> tuple[int, str]:
    """The round cap for this pull request: the lowest cap at `head_sha`
    and at every other `reviewed_ref` a run-verified, verdict-bound record
    names — ported from `kb_merge._round_cap`. A later commit can lower the
    cap and never raise it, so a head that was documents-only keeps the
    document cap for the whole pull request."""
    cap, why = review_round_cap(policy, _floor_at(conn, repo, head_sha))
    for ref in sorted(review_refs - {head_sha}):
        ref_cap, ref_why = review_round_cap(policy, _floor_at(conn, repo, ref))
        if ref_cap < cap:
            cap, why = ref_cap, f"at {ref[:12]}, {ref_why}"
    return cap, why


# -----------------------------------------------------------------------
# Carrying an earlier APPROVE onto a rebuilt head (platform/decisions/0078,
# ported from agent-platform's `kb_merge.carry_blocked`/`carry_for_role`)
# -----------------------------------------------------------------------

ORIGIN_MAIN_REF = "refs/remotes/origin/main"
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class _CarryUnreadable(Exception):
    """Raised by this section's private helpers when a needed git object
    cannot be read at all: the stamped commit missing from the object
    store, an uncomputable merge base, or any other git failure. Caught
    once, by `_carry_reason`, and reported as `stale_review`, retryable —
    a git failure must never read as "carries"."""


def _show_ref_sha(root: Path, ref: str) -> str | None:
    """The commit `ref` names, matching that exact ref only, or `None`."""
    done = _git(root, "show-ref", "--verify", "--hash", ref)
    if done is None or done.returncode != 0:
        return None
    sha = done.stdout.decode("ascii", "replace").strip()
    return sha if _FULL_SHA.fullmatch(sha) else None


def _commit_exists(root: Path, sha: str) -> bool:
    """Whether `sha` is already a commit object in `root`'s object store.
    This is the first check `_carry_blocked` makes, specifically so a
    rebuild's earlier, force-pushed-away commit — which this module never
    fetches on its own — is reported by name rather than surfacing as an
    unexplained `git merge-base` failure."""
    done = _git(root, "cat-file", "-e", f"{sha}^{{commit}}")
    return done is not None and done.returncode == 0


def _carry_merge_base(root: Path, a: str, b: str) -> str:
    done = _git(root, "merge-base", a, b)
    sha = done.stdout.decode("ascii", "replace").strip() if done is not None else ""
    if done is None or done.returncode != 0 or not _FULL_SHA.fullmatch(sha):
        raise _CarryUnreadable(f"git merge-base {a[:12]} {b[:12]} failed")
    return sha


def _carry_changed_path_set(root: Path, base: str, head: str) -> frozenset[str]:
    """The paths changed between `base` and `head` taken directly (not a
    three-dot diff — the caller passes a merge base as `base` precisely
    when that is what is meant), with rename detection off, so the two
    sides' path sets are compared without git collapsing one into the
    other first."""
    done = _git(root, "diff", "--name-only", "-z", "--no-renames", base, head)
    if done is None or done.returncode != 0:
        raise _CarryUnreadable(f"git diff --name-only {base[:12]} {head[:12]} failed")
    raw = done.stdout.split(b"\0")
    if raw and raw[-1] == b"":
        raw.pop()
    out: list[str] = []
    for p in raw:
        try:
            out.append(p.decode("utf-8"))
        except UnicodeDecodeError:
            raise _CarryUnreadable(f"a changed path is not UTF-8: {p[:80]!r}") from None
    return frozenset(out)


def _carry_entry_at(root: Path, commit: str, path: str) -> tuple[str, str] | None:
    """`(mode, blob-sha)` for `path` as git tracks it at `commit`, or `None`
    when `path` is not tracked there at all — tells "absent" apart from
    "present but different", which the third condition below needs (an
    added file has no main-side original on either side)."""
    done = _git(root, "ls-tree", "-z", "-r", "--full-tree", commit, "--", path)
    if done is None or done.returncode != 0:
        raise _CarryUnreadable(f"git ls-tree {commit[:12]}:{path} failed")
    entries = [e for e in done.stdout.split(b"\0") if e]
    if not entries:
        return None
    if len(entries) > 1:
        raise _CarryUnreadable(f"git ls-tree {commit[:12]}:{path} returned more than one entry")
    meta, _, raw_path = entries[0].partition(b"\t")
    try:
        path_str = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        raise _CarryUnreadable(f"the path at {commit[:12]} is not UTF-8") from None
    if path_str != path:
        raise _CarryUnreadable(
            f"git ls-tree {commit[:12]}:{path} returned {path_str!r}, not {path!r}")
    fields = meta.decode("ascii", "replace").split(" ")
    if len(fields) != 3:
        raise _CarryUnreadable(f"git ls-tree output not understood: {meta!r}")
    mode, _obj_type, sha = fields
    return mode, sha


def _carry_blocked(root: Path, stamp_commit: str, head: str, base: str) -> str | None:
    """Why an APPROVE stamped at `stamp_commit` (S, an earlier commit of the
    same pull request) does not carry to the pull request's current head
    `head` (H) — platform/decisions/0078's amendment under "What the
    Library may merge" — or `None` iff it does. `base` is `origin/main`'s
    current sha. Three conditions, each returning its own reason naming
    the mismatch, in the order the amendment states them:
      1. S and H change the same set of paths, each against its own
         merge base.
      2. Every one of those paths is byte- and mode-identical between S
         and H.
      3. Every one of those paths' main-side original — the blob at the
         merge base — is identical between S's merge base and H's;
         absent on both sides is fine.
    Raises `_CarryUnreadable` when `stamp_commit` is not in `root`'s
    object store, any merge base cannot be computed, or any other git
    read along the way fails — never read as "does not carry"."""
    if not _commit_exists(root, stamp_commit):
        raise _CarryUnreadable(f"{stamp_commit[:12]} is not in {root}'s object store")
    mb_s = _carry_merge_base(root, stamp_commit, base)
    mb_h = _carry_merge_base(root, head, base)
    paths_s = _carry_changed_path_set(root, mb_s, stamp_commit)
    paths_h = _carry_changed_path_set(root, mb_h, head)
    if paths_s != paths_h:
        only_s, only_h = sorted(paths_s - paths_h), sorted(paths_h - paths_s)
        return (f"{stamp_commit[:12]} and {head[:12]} change different paths against "
               f"their own merge bases (only at {stamp_commit[:12]}: {only_s or 'none'}; "
               f"only at {head[:12]}: {only_h or 'none'})")
    for path in sorted(paths_h):
        entry_h = _carry_entry_at(root, head, path)
        entry_s = _carry_entry_at(root, stamp_commit, path)
        if entry_h != entry_s:
            return (f"{path} differs between {stamp_commit[:12]} ({entry_s}) and "
                   f"{head[:12]} ({entry_h})")
    for path in sorted(paths_h):
        before_h = _carry_entry_at(root, mb_h, path)
        before_s = _carry_entry_at(root, mb_s, path)
        blob_h = before_h[1] if before_h else None
        blob_s = before_s[1] if before_s else None
        if blob_h != blob_s:
            return (f"the main-side original of {path} differs between {mb_s[:12]} "
                   f"(blob {blob_s}) and {mb_h[:12]} (blob {blob_h})")
    return None


# The trailer every commit `agentctl kb propose` and `kb merge`'s rebuild
# write carries, as agent-platform's `kb_audit.TRAILER_RE` matches it.
KB_WRITE_TRAILER_RE = re.compile(
    r"^KB-Write:\s+(?P<verb>\S+)\s+(?P<ref>\S+)\s+(?P<provenance>\S+)\s+(?P<holder>.+)$",
    re.MULTILINE)


def _parents(root: Path, sha: str) -> list[str]:
    """The parents of commit `sha`, in order."""
    done = _git(root, "rev-list", "--parents", "-n", "1", sha)
    if done is None or done.returncode != 0:
        raise _CarryUnreadable(f"git rev-list --parents {sha[:12]} failed")
    fields = done.stdout.decode("ascii", "replace").split()
    if not fields or fields[0] != sha or not all(_FULL_SHA.fullmatch(p) for p in fields[1:]):
        raise _CarryUnreadable(f"git rev-list --parents {sha[:12]} output not understood")
    return fields[1:]


def _has_kb_write_trailer(root: Path, sha: str) -> bool:
    done = _git(root, "cat-file", "commit", sha)
    if done is None or done.returncode != 0:
        raise _CarryUnreadable(f"git cat-file commit {sha[:12]} failed")
    _headers, _, message = done.stdout.partition(b"\n\n")
    return KB_WRITE_TRAILER_RE.search(message.decode("utf-8", "replace")) is not None


def _carry_shape_blocked(root: Path, stamp_commit: str, head: str, base: str) -> str | None:
    """Why `stamp_commit` and `head` are not the shape a Library proposal
    takes, or `None` iff they are. Checked before `_carry_blocked`, and
    narrower than agent-platform's carry, for two reasons that original's
    caller settles itself:

    - Decision 0078 grants the carry to `kb/` proposals only, and `evaluate`
      gets no branch name. `agentctl kb propose` and `kb merge`'s rebuild
      each write ONE commit, parented on `main`'s tip, carrying a
      `KB-Write:` trailer, the same trailer `kb merge` requires on every
      commit of a PR it merges. A commit without one is not carried.
    - `_carry_blocked`'s third condition is sound only against a `main` at
      least as new as the one `head` was built on. `kb merge` fetches just
      before it asks; this module never fetches, and `reference.KB_REPO`'s
      `origin/main` can lag. Requiring `head`'s one parent to BE that ref
      means condition 3 compares against exactly the `main` `head` was
      built on. A head built on a newer `main` than the checkout holds is
      refused here, retryable, rather than judged against an older one that
      would miss `main`'s own edits to a file the head overwrites.
    """
    for sha in (stamp_commit, head):
        if not _FULL_SHA.fullmatch(sha):
            return f"{sha[:12]!r} is not a full commit sha"
    if not _commit_exists(root, stamp_commit):
        raise _CarryUnreadable(f"{stamp_commit[:12]} is not in {root}'s object store")
    if not _commit_exists(root, head):
        raise _CarryUnreadable(f"{head[:12]} is not in {root}'s object store")
    head_parents = _parents(root, head)
    if head_parents != [base]:
        return (f"{head[:12]} is not one commit on {ORIGIN_MAIN_REF} ({base[:12]}): its "
                f"parents are {[p[:12] for p in head_parents]}")
    if len(_parents(root, stamp_commit)) != 1:
        return f"{stamp_commit[:12]} is not a single-parent commit"
    for sha in (stamp_commit, head):
        if not _has_kb_write_trailer(root, sha):
            return f"{sha[:12]} carries no KB-Write trailer"
    return None


def _carry_reason(root: Path, stamp_commit: str, head_sha: str) -> str | None:
    """`None` when `stamp_commit`'s APPROVE carries to `head_sha`: the two
    commits have a Library proposal's shape (`_carry_shape_blocked`) and
    platform/decisions/0078's three conditions hold (`_carry_blocked`).
    Otherwise why not, or what git object made the check unreadable
    (`_CarryUnreadable`, caught here) — either way the caller reports the
    role as `stale_review`."""
    try:
        base = _show_ref_sha(root, ORIGIN_MAIN_REF)
        if base is None:
            raise _CarryUnreadable(f"{ORIGIN_MAIN_REF} does not exist in {root}")
        shape = _carry_shape_blocked(root, stamp_commit, head_sha, base)
        if shape is not None:
            return shape
        return _carry_blocked(root, stamp_commit, head_sha, base)
    except _CarryUnreadable as exc:
        return str(exc)


# -----------------------------------------------------------------------
# The evaluator itself
# -----------------------------------------------------------------------


def _missing(kind: str, *, detail: str, retryable: bool, role: str | None = None,
            workflow: str | None = None) -> dict:
    return {"kind": kind, "role": role, "workflow": workflow, "detail": detail,
            "retryable": retryable}


def _review_rows(conn: sqlite3.Connection, repo_identity: str, pr: int,
                 role: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT verdict, reviewed_ref, run_id, updated_at FROM review_records "
        "WHERE repo_identity = ? AND pr = ? AND role = ? AND auth = ? "
        "AND verdict_bound = 1",
        (repo_identity, pr, role, store.REVIEW_AUTH_RUN_VERIFIED)).fetchall()


def _unbound_review_rows(conn: sqlite3.Connection, repo_identity: str, pr: int,
                         role: str) -> list[sqlite3.Row]:
    """`_review_rows`'s same query, for the role's run-verified records that
    are NOT verdict-bound — posted before verdict binding went live on
    2026-10-25 (library CQ·217). Read only so `_review_missing` can tell
    "never reviewed" apart from "reviewed, but the record predates verdict
    binding"; these rows never count toward admission."""
    return conn.execute(
        "SELECT verdict, reviewed_ref, run_id, updated_at FROM review_records "
        "WHERE repo_identity = ? AND pr = ? AND role = ? AND auth = ? "
        "AND verdict_bound = 0",
        (repo_identity, pr, role, store.REVIEW_AUTH_RUN_VERIFIED)).fetchall()


def _all_review_refs(conn: sqlite3.Connection, repo_identity: str,
                     pr: int) -> set[str]:
    """Every `reviewed_ref` a run-verified record on this pull request names,
    whatever its role. `kb_merge._round_cap` lowers the cap at every
    authenticated stamp's head, `shadow-reviewer`'s included, so reading only
    the required roles here would let the two gates disagree (CQ·218 round
    1, reviewer). `verdict_bound` is not required: a head can only lower the
    cap, so counting more heads fails closed."""
    return {r["reviewed_ref"] for r in conn.execute(
        "SELECT DISTINCT reviewed_ref FROM review_records "
        "WHERE repo_identity = ? AND pr = ? AND auth = ?",
        (repo_identity, pr, store.REVIEW_AUTH_RUN_VERIFIED)).fetchall()}


def _review_missing(conn: sqlite3.Connection, repo_identity: str, pr: int, role: str,
                    rows: list[sqlite3.Row], head_sha: str, repo: str) -> list[dict]:
    if not rows:
        if _unbound_review_rows(conn, repo_identity, pr, role):
            detail = (f"{role} has a run-verified review that predates verdict "
                      f"binding; a fresh review from {role}, not a missing one, "
                      f"is what clears this")
        else:
            detail = f"no run-verified, verdict-bound review from {role}"
        return [_missing("review", role=role, retryable=False, detail=detail)]
    latest = max(rows, key=lambda r: r["updated_at"])
    if latest["reviewed_ref"] != head_sha:
        detail = (f"{role}'s latest run-verified review (run {latest['run_id']}) "
                  f"is at {latest['reviewed_ref']}, not {head_sha}")
        # Exact and case-sensitive, as `_local_checkout_root` spells repos:
        # another spelling skips the carry and keeps the refusal.
        if repo == "convivy/knowledge" and latest["verdict"] == APPROVE:
            carry_reason = _carry_reason(reference.KB_REPO, latest["reviewed_ref"], head_sha)
            if carry_reason is None:
                return []
            detail = f"{detail}, and does not carry: {carry_reason}"
        return [_missing("stale_review", role=role, retryable=True, detail=detail)]
    if latest["verdict"] != APPROVE:
        return [_missing(
            "review", role=role, retryable=False,
            detail=f"{role}'s latest run-verified review at this head is "
                   f"{latest['verdict']}, not {APPROVE}")]
    return []


def _repo_run_url_like(repo: str) -> str:
    """The `LIKE` pattern matching a `test-result` signal's `run_url`
    against exactly `repo` — mirrors `app.py`'s own private
    `_repo_run_url_like`, duplicated rather than imported since importing
    `app.py` here would pull in the whole FastAPI application."""
    escaped = repo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"https://github.com/{escaped}/actions/runs/%"


def _latest_check_conclusion(conn: sqlite3.Connection, repo: str, product: str,
                             workflow_file: str, head_sha: str) -> str | None:
    """The latest `test-result` signal's conclusion for `repo`'s
    `workflow_file` at `head_sha`, ingested by
    `CI_RESULT_CAPTURE_INGESTED_BY`, or `None` when no such row exists."""
    source = store.signals_source("kind = 'test-result'")
    params = [_repo_run_url_like(repo), ci_tracked.workflow_slug(workflow_file),
             head_sha, product, CI_RESULT_CAPTURE_INGESTED_BY]
    row = conn.execute(
        f"SELECT json_extract(refs, '$.conclusion') AS conclusion FROM {source} "
        f"AS signals WHERE json_extract(refs, '$.run_url') LIKE ? ESCAPE '\\' AND "
        f"json_extract(refs, '$.workflow') = ? AND "
        f"json_extract(refs, '$.commit') = ? AND product = ? AND ingested_by = ? "
        f"ORDER BY produced_at DESC NULLS LAST, ingested_at DESC, id DESC LIMIT 1",
        params).fetchone()
    return row["conclusion"] if row is not None else None


def _check_missing(conn: sqlite3.Connection, repo: str, head_sha: str) -> list[dict]:
    entries = [e for e in ci_tracked.tracked()
              if e["repo"] == repo and e["on_pull_request"]]
    if not entries:
        return []
    reason = store.attach_cold_or_reason(conn, readonly=True)
    missing: list[dict] = []
    for entry in entries:
        workflow = entry["workflow_file"]
        if reason is not None:
            missing.append(_missing(
                "check_pending", workflow=workflow, retryable=True,
                detail=f"the archive tier is unavailable, so {workflow}'s "
                       f"result at {head_sha} cannot be confirmed: {reason}"))
            continue
        conclusion = _latest_check_conclusion(conn, repo, entry["product"],
                                              workflow, head_sha)
        if conclusion is None:
            missing.append(_missing(
                "check_pending", workflow=workflow, retryable=True,
                detail=f"{workflow} has not reported a result at {head_sha} yet"))
        elif conclusion != "success":
            missing.append(_missing(
                "check", workflow=workflow, retryable=False,
                detail=f"{workflow}'s latest conclusion at {head_sha} is "
                       f"{conclusion}, not success"))
    return missing


def evaluate(repo: str, pr: int | str, head_sha: str, *,
            conn: sqlite3.Connection | None = None) -> dict:
    """Whether `(repo, pr, head_sha)` is admitted to merge — `{"approved":
    bool, "missing": [...]}`, every entry shaped `{kind, role, workflow,
    detail, retryable}` with `role`/`workflow` `None` where the kind names
    the other (or neither, for `"unreadable"`). See the module docstring
    for what each `kind` means and why `approved` says the verdict is
    attested by the run register, never that a human or a model in fact
    approved.

    `repo` is `owner/name` (`"convivy/library"`); `pr` is a pull-request
    number, int or a numeric string; `head_sha` is the commit this call
    judges. The caller — the future route this evaluator is not yet wired
    to — is responsible for validating their shape; this function trusts
    it the way `reference_admission.check_checkout` trusts its own
    caller-validated arguments.

    `conn` defaults to a fresh `store.connect()`, closed before returning;
    a caller already holding one (a future route, inside its own request)
    passes it through instead.
    """
    repo = repo.strip()
    head_sha = head_sha.strip()
    pr_int = int(str(pr).strip())
    repo_identity = f"github:{repo}"

    owns_conn = conn is None
    if owns_conn:
        conn = store.connect()
    try:
        missing: list[dict] = []

        registry_reason = ci_tracked.registry_unusable_reason()
        if registry_reason is not None:
            missing.append(_missing(
                "unreadable", retryable=False,
                detail=f"the project registry could not be read, so the "
                       f"pull-request workflows {repo} is checked against "
                       f"are unknown: {registry_reason}"))

        rows_by_role = {role: _review_rows(conn, repo_identity, pr_int, role)
                        for role in REQUIRED_ROLES}
        for role in REQUIRED_ROLES:
            missing.extend(_review_missing(conn, repo_identity, pr_int, role,
                                           rows_by_role[role], head_sha, repo))

        policy_text = _policy_text_at_origin_main(reference.KB_REPO)
        policy = _parse_policy_numbers(policy_text)
        if policy is None:
            missing.append(_missing(
                "unreadable", retryable=False,
                detail="the agent policy file at knowledge origin/main could "
                       "not be read or does not parse, so no review-round "
                       "cap can be computed"))
        else:
            review_refs = _all_review_refs(conn, repo_identity, pr_int)
            cap, why = _round_cap_for(conn, repo, head_sha, policy, review_refs)
            for role in REQUIRED_ROLES:
                run_ids = {r["run_id"] for r in rows_by_role[role]}
                if len(run_ids) > cap:
                    missing.append(_missing(
                        "round_cap", role=role, retryable=False,
                        detail=f"{role} has posted {len(run_ids)} run-verified "
                               f"rounds, past the cap of {cap} ({why})"))

        missing.extend(_check_missing(conn, repo, head_sha))

        return {"approved": len(missing) == 0, "missing": missing}
    finally:
        if owns_conn:
            conn.close()
