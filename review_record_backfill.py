"""One-time backfill: an `unverified` review record for every stamped review
comment captured since 2026-06-18, linked by comment id to the
`github_review`/`pr_comment` signal the Archives already hold
(issue_01M2HS4DY0M5S5D4ND4V76VXJ5, step 4 of "All reviews are memory",
issue_01M2HS6XNVPG6ZB5RNQDN2TJKN). Needs the table (`468a1789d29e`,
`e4894432e1aa`) and the write route's store function, `store.write_review_record`.

**No new signal, and nothing embedded.** The operator's ruling on the epic is that
memory is not duplicated: a stamped comment's prose already lives in the
Archives as the `pr_comment` signal this backfill links to, so the record's
`signal_id` is stamped onto that EXISTING signal — `store.set_review_record_signal`,
the same function `POST /review-records` uses to link a freshly minted one,
called here with an id this script never mints.

**Every record is `unverified`, `auth_reason` "backfilled comment", and this
never asks the run-facts socket.** `_review_record_auth` (`app.py`) decides
`run-verified` by checking a review's dispatch run against the Bosun's
register AT WRITE TIME; these comments were posted weeks or months ago, so
asking that register about them now would answer a question about today's
run state, not about what was true when the review was posted. A comment is
not a run-verified write regardless of whether it happens to carry a
`run-id` — the id is kept as provenance (and as part of the uniqueness key
when present), never as grounds for authentication.

**The 2026-06-18 boundary is the epic's own boundary for this backfill,
not an arbitrary date.** issue_01M2HS4DY0M5S5D4ND4V76VXJ5 fixes it as the
start of the population this backfill covers; this script does not
re-derive or second-guess it.

**Processed in ascending `produced_at` order.** Comment 2748 on
issue_01M2HS394KTFMRATFK1N3G4ZNB measured, over 10,890 stamped comments
grouped on (repo, run, role, reviewed commit): 50 groups of identical
re-posts, 3 sharing a verdict but differing in prose, and 6 differing in
verdict outright — one run posting APPROVE, then REQUEST_CHANGES two
minutes later. `store.write_review_record`'s own key treats an identical
repeat as a no-op and a differing one as a supersede, so processing
oldest-first is what leaves the LATEST comment in each group as the
current row and every earlier one preserved in `review_record_versions` —
matching what a reader of that table expects, and matching what today's
merge gate already does by taking the last stamp per agent.

**`--write` performs it; passing nothing (`--dry-run`, the default) only
reports what would happen**, matching `backfill_ci_meta_status.py`.
**Idempotent across passes, by comparing each comment's `produced_at`
against the stored record's `updated_at`, not by content hash alone.**
A shared-key group's comments are matched against each other correctly
only forward in time: replayed on a second pass, the OLDEST comment in
the group now differs from the record (which already holds the newest
comment's content) and would read as a fresh supersede under a
content-only check, and so would every comment after it — the version
history would grow without bound on every re-run. A comment strictly
older than the stored `updated_at` is left alone rather than compared by
content at all; see `plan`.

**A key already `run-verified` is left alone, never superseded.** Rehearsed
against a copy of the live database, the first real `run-verified` record
in the Archives came out downgraded to `unverified` by this backfill: the
comment the Archives captured for that same review shares its uniqueness
key (repo, run, role, reviewed commit) but differs in content (the
comment's own prose, and `auth` itself), so `store.write_review_record`'s
ordinary supersede rule pushed the verified row into history. A `POST
/review-records` write and its own stamped comment are the same review
told twice; the comment is a copy, and a copy must never overwrite the
record the Library already holds under its own authority. See `plan`.

**That guard is enforced twice, and only the second one closes it.**
`plan()`'s check above is a snapshot, read once per key, up to ~11k rows
and several minutes before the write loop reaches a given row — a genuine
`POST /review-records` call can mark a key `run-verified` at any point in
that window, after `plan()` already decided the comment sharing that key
belonged in `planned`. So every write this script issues also passes
`skip_if_verified=True` and `only_if_after=(produced_at, signal_id)` to
`store.write_review_record`, which re-checks both conditions against a
fresh read inside its own write transaction — the only place a check
against a live table can't be raced. `plan()`'s pre-filtering still stands,
as the cheap common case; the store-level guard is what makes it correct
under a concurrent writer. The same double-check covers the tie case
directly below: two different comments can share a `produced_at` (GitHub
timestamps are second-precision), and only the store's own
`(updated_at, signal_id)` position check — not `plan()`'s single-timestamp
comparison — can tell which of them the load order says is later.

**255 of the matched comments carry an abbreviated `reviewed-commit`**
(7 to 12 hex characters, e.g. `1d4e2184a`), all from an older stamp format
posted between June and July 2026, the latest on 2026-07-27. These are
correctly skipped as "invalid reviewed_ref": a record's `reviewed_ref` is
the exact commit, and expanding an abbreviation would be guessing.
"""
from __future__ import annotations

import argparse
import re
import sys

import projects
import store

SOURCE = "github_review"
KIND = "pr_comment"
STAMP_PREFIX = "<!-- agentctl-review"
SINCE = "2026-06-18"
AUTH_REASON = "backfilled comment"

# **HOW OFTEN `--write` PRINTS PROGRESS, NOT A TRANSACTION BOUNDARY.**
# `store.write_review_record` and `store.set_review_record_signal` each open
# their own connection, take `BEGIN IMMEDIATE` (or a plain write), and commit
# before returning — so this script never holds one transaction across more
# than a single record, and the database (rollback-journal mode, live
# traffic throughout) is never blocked for longer than one record's write
# takes. What this constant bounds is how often a person watching a
# ~10,978-row live run sees a line telling them it is still moving.
FLUSH_EVERY = 200

# The stamp header this backfill parses, duplicated from
# `agent-platform/lib/agent-platform/review_protocol.py` rather than
# imported — this service does not depend on that repo, the same reason
# `store.db_uri` is duplicated rather than shared with `migrate.py` (see
# that function's own docstring). Six lines here beats a coupling to
# another repo's install layout.
_BLOCK_RE = re.compile(r"<!--\s*agentctl-review\s*(.*?)-->", re.DOTALL)
_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
# Exactly `_review_record_product`'s own `reviewed_ref` shape (`app.py`) — a
# git sha (40 hex) or a document content hash (64 hex, sha256).
_REVIEWED_REF_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_COMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)")

# The org every repo `projects.json` names belongs to. Fixed rather than
# read from anywhere, matching the stamp itself: `repo_stamp` in
# `agentctl review-post` is already the bare repo name with the owner
# stripped, so there is no owner in the data to read back.
GITHUB_OWNER = "convivy"


class UnparseableStamp(ValueError):
    """The comment's `<!-- agentctl-review ... -->` block is missing a field
    a review record needs, or names a verdict this vault does not know."""


def _parse_stamp(body: str) -> dict:
    """The stamp's fields, or `UnparseableStamp` naming what is missing.

    Structural parsing only — this validates that the header exists and
    carries `repo`, `agent` and a recognised `verdict`. `reviewed-commit`'s
    hex shape is checked by the caller, as its own, differently-reported
    skip reason (per the item's own requirement that an unresolvable repo,
    a malformed `reviewed_ref`, and an unparseable stamp are reported
    distinctly).
    """
    m = _BLOCK_RE.search(body or "")
    if not m:
        raise UnparseableStamp("no agentctl-review block found")
    kv: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        kv[k.strip()] = v.strip()
    verdict = kv.get("verdict", "").upper()
    if verdict not in _VERDICTS:
        raise UnparseableStamp(
            f"verdict {kv.get('verdict', '')!r} is not one of {_VERDICTS}")
    repo = kv.get("repo", "").strip()
    if not repo:
        raise UnparseableStamp("no repo field in the stamp header")
    agent = kv.get("agent", "").strip()
    if not agent:
        raise UnparseableStamp("no agent field in the stamp header")
    reviewed_commit = kv.get("reviewed-commit", "").strip()
    if not reviewed_commit:
        raise UnparseableStamp("no reviewed-commit field in the stamp header")
    pr_raw = kv.get("pr", "").strip()
    pr = None
    if pr_raw:
        try:
            pr = int(pr_raw)
        except ValueError:
            raise UnparseableStamp(f"pr {pr_raw!r} is not an integer")
    run_id = kv.get("run-id", "").strip() or None
    return {"repo": repo, "reviewed_commit": reviewed_commit, "verdict": verdict,
            "agent": agent, "pr": pr, "run_id": run_id}


def _strip_stamp(body: str) -> str:
    """The comment body with the header removed — this is `prose`, per the
    epic's ruling that the review's own words are the memory, and the
    machine-parseable block that makes a comment gate-readable is not part
    of what a human reads back."""
    return _BLOCK_RE.sub("", body or "", count=1).strip()


def _comment_id(refs: dict) -> str | None:
    """The GitHub comment id out of `refs.url`
    (`.../pull/521#issuecomment-5685007473`), or `None` when the signal
    carries no such url — every row this backfill's own query selects is
    measured to carry one, so `None` here is a signal this script does not
    otherwise expect rather than a case it was built to accept."""
    url = refs.get("url") if isinstance(refs, dict) else None
    if not isinstance(url, str):
        return None
    m = _COMMENT_ID_RE.search(url)
    return m.group(1) if m else None


def _load_stamped_comments(conn):
    """Every `github_review`/`pr_comment` signal whose body carries the
    stamp header, produced on or after `SINCE`, oldest first — the read
    this backfill's whole plan is built from."""
    return conn.execute(
        "SELECT id, body, refs, produced_at FROM signals WHERE source = ? "
        "AND kind = ? AND body LIKE ? AND produced_at >= ? "
        "ORDER BY produced_at ASC, id ASC",
        (SOURCE, KIND, STAMP_PREFIX + "%", SINCE)).fetchall()


def _existing_record_state(conn, key: tuple) -> dict | None:
    """The `content_sha`, `auth` and `updated_at` already stored under this
    uniqueness key, or `None`.

    `content_sha`, `auth` and `updated_at` are all read here only to build
    `plan()`'s PREVIEW — the dry run's report, and the pre-filter that
    keeps an already-verified key's comments out of `planned` in the common
    case. None of the three is authoritative for `--write`: that pass
    always takes `store.write_review_record`'s own answer, read fresh
    inside its own transaction via `skip_if_verified` and `only_if_after`,
    which is what stays correct if this snapshot goes stale before the
    write loop reaches the row it was read for.
    """
    repo_identity, identity_key, role, reviewed_ref = key
    row = conn.execute(
        "SELECT content_sha, auth, updated_at FROM review_records WHERE "
        "repo_identity = ? AND identity_key = ? AND role = ? AND "
        "reviewed_ref = ?",
        (repo_identity, identity_key, role, reviewed_ref)).fetchone()
    if row is None:
        return None
    return {"content_sha": row["content_sha"], "auth": row["auth"],
            "updated_at": row["updated_at"]}


def plan(conn, rows: list) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Every stamped comment `rows` (already ordered oldest-first) sorts
    into a planned write, a skip, "left verified" alone, or "already
    current", read-only — shared by the dry-run report and the real
    `--write` pass so the two can never disagree about which rows qualify.

    Each planned entry carries the row `store.write_review_record` needs,
    the source signal id and comment id to link afterward, and a PREDICTED
    outcome (`created`/`superseded`/`unchanged`) for the dry-run report.
    The prediction replays the same key lookup `store.write_review_record`
    performs, in the same order, seeded from this table's current content —
    which is what makes two backfilled comments that share a key and land
    in the SAME pass classify correctly against each other, not only
    against whatever the table already held before this run started.

    **A key whose CURRENT record is `run-verified` is never planned at
    all.** A review can exist twice: as a record written through
    `POST /review-records` (which can be `run-verified`) and as the stamped
    comment the Archives captured for the same run, role and reviewed
    commit — sharing this table's uniqueness key. Their content differs
    (the comment's own prose, and `auth` itself, are baked into
    `content_sha`), so treating the comment like any other input would
    make `store.write_review_record` supersede the verified row and push
    it into history, downgrading a real verification to `unverified`. A
    captured comment is a copy of a review the Library already holds under
    its own authority, and a copy must never overwrite the record — so
    once a key's stored record is `run-verified`, every comment sharing
    that key is counted in `left_verified` and neither superseded nor
    relinked, this pass or any later one.

    **A comment strictly OLDER than the record's stored `updated_at` is
    never planned either — it is counted in `already_current` instead.**
    `updated_at` is written from the comment's own `produced_at` (`run`'s
    `at=` argument to `store.write_review_record`), so it names exactly
    which comment the record currently reflects. Within one pass, a
    shared-key group is processed oldest-first and each later comment IS
    newer than what came before, so this never fires on a first pass. On a
    repeat pass, though, the record already holds the group's newest
    content, and every comment before that one is older than the stored
    `updated_at` — comparing by content alone would read the oldest
    comment as differing (superseding) and every one after it as differing
    again, filling `review_record_versions` with churn on every re-run.
    Comparing produced_at against the stored updated_at is what a reader
    actually wants answered ("does this comment postdate what the record
    already reflects?"), and it settles the question without touching
    content_sha at all.

    **Strictly older, not "older than or equal to".** The one comment
    whose `produced_at` exactly equals the stored `updated_at` is the
    comment the record currently reflects, so it still needs the ordinary
    content comparison below — which resolves it to `unchanged`, since its
    content matches what is stored (an identical repeat with a later
    `produced_at` lands here too, by the same comparison). Skipping it
    outright here, rather than letting it fall through, would also skip
    `run`'s post-write relink check, which fires only for rows this
    function still hands to `store.write_review_record` — and a resumed
    pass that crashed between a write and its link would then never finish
    linking that one row.
    """
    simulated: dict[tuple, dict] = {}
    verified_keys: set[tuple] = set()
    planned: list[dict] = []
    skipped: list[dict] = []
    left_verified: list[dict] = []
    already_current: list[dict] = []
    for row in rows:
        refs = store._json_or(row["refs"], {})
        comment_id = _comment_id(refs)
        if comment_id is None:
            skipped.append({"signal_id": row["id"], "reason": "no comment id",
                            "detail": "refs.url carries no #issuecomment-<id>"})
            continue
        try:
            stamp = _parse_stamp(row["body"])
        except UnparseableStamp as exc:
            skipped.append({"signal_id": row["id"], "reason": "unparseable stamp",
                            "detail": str(exc)})
            continue
        if not _REVIEWED_REF_RE.match(stamp["reviewed_commit"]):
            skipped.append({
                "signal_id": row["id"], "reason": "invalid reviewed_ref",
                "detail": f"{stamp['reviewed_commit']!r} is not 40 or 64 "
                          f"lowercase hex characters"})
            continue
        # A stamp's `repo` field is not always bare: 25 of the comments this
        # backfill has seen carry an owner-qualified value
        # (`convivy/vivy`, `convivy/constance`, `convivy/agent-platform`),
        # which `projects.resolve` does not know (it matches only the bare
        # name). Stripping to the last path segment before resolving matches
        # `_merge_repo_scope` in `app.py`, which does the same for the same
        # reason on the live write route.
        bare_repo = stamp["repo"].rsplit("/", 1)[-1]
        product = projects.resolve(bare_repo)
        if product is None:
            skipped.append({
                "signal_id": row["id"], "reason": "repo does not resolve",
                "detail": f"{stamp['repo']!r} maps to no product in the "
                          f"crew registry"})
            continue

        prose = _strip_stamp(row["body"])
        repo_identity = f"github:{GITHUB_OWNER}/{bare_repo}"
        content_sha = store.review_content_sha(
            stamp["verdict"], [], prose, [], [], [], store.REVIEW_AUTH_UNVERIFIED)
        identity_key = stamp["run_id"] or f"content:{content_sha}"
        key = (repo_identity, identity_key, stamp["agent"], stamp["reviewed_commit"])
        if key in verified_keys:
            left_verified.append({"signal_id": row["id"], "comment_id": comment_id})
            continue
        produced_at = row["produced_at"]
        state: dict | None
        if key in simulated:
            state = simulated[key]
        else:
            existing = _existing_record_state(conn, key)
            if existing is not None and existing["auth"] == store.REVIEW_AUTH_RUN_VERIFIED:
                verified_keys.add(key)
                left_verified.append({"signal_id": row["id"], "comment_id": comment_id})
                continue
            state = ({"content_sha": existing["content_sha"],
                      "updated_at": existing["updated_at"]}
                     if existing is not None else None)
        if state is not None and produced_at < state["updated_at"]:
            already_current.append({"signal_id": row["id"], "comment_id": comment_id})
            continue
        prior_sha = state["content_sha"] if state is not None else None
        if prior_sha is None:
            outcome = store.REVIEW_OUTCOME_CREATED
        elif prior_sha == content_sha:
            outcome = store.REVIEW_OUTCOME_UNCHANGED
        else:
            outcome = store.REVIEW_OUTCOME_SUPERSEDED
        simulated[key] = {"content_sha": content_sha, "updated_at": produced_at}

        planned.append({
            "signal_id": row["id"], "comment_id": comment_id,
            "produced_at": row["produced_at"], "predicted_outcome": outcome,
            "row": {
                "repo_identity": repo_identity, "product": product,
                "pr": stamp["pr"], "reviewed_ref": stamp["reviewed_commit"],
                "role": stamp["agent"], "run_id": stamp["run_id"],
                "verdict": stamp["verdict"], "findings": [], "prose": prose,
                "paths": [], "doc_ids": [], "item_ids": [],
                "auth": store.REVIEW_AUTH_UNVERIFIED,
                "auth_reason": AUTH_REASON,
                "source_comment_id": comment_id,
            },
        })
    return planned, skipped, left_verified, already_current


_OUTCOME_VERB = {
    store.REVIEW_OUTCOME_CREATED: "would write",
    store.REVIEW_OUTCOME_SUPERSEDED: "would supersede",
    store.REVIEW_OUTCOME_UNCHANGED: "no-op",
}


def run(*, write: bool, limit: int | None = None) -> int:
    # Checked up front, before any read: `store.write_review_record` would
    # hit this same refusal on the first planned row anyway (it goes
    # through `store.connect_rw()`, the one door every write in this
    # service is refused at), but raising `store.WritesDisabled` as an
    # uncaught traceback out of a one-off operator script is a worse
    # failure mode than the same refusal every other write path here
    # already prints cleanly (`replay_stranded.py`'s own `--i-mean-it`
    # check is the same shape).
    if write and not store.WRITES_ENABLED:
        print("review_record_backfill: LIBRARY_WRITES_ENABLED is not set to "
              "'1'. --write asks to write, and this goes through "
              "store.connect_rw(), the same door every other write path in "
              "this service is refused at. Set it and run it again.",
              file=sys.stderr)
        return 2

    read_conn = store.connect()
    try:
        if not projects.available():
            print("review_record_backfill: the crew registry is unreadable — "
                  "refusing rather than resolving every repo to itself "
                  "(projects.resolve fails open when the registry cannot be "
                  "read, which here would silently mis-scope every record)",
                  file=sys.stderr)
            return 2
        rows = _load_stamped_comments(read_conn)
        found = len(rows)
        if limit is not None:
            rows = rows[:limit]
        planned, skipped, left_verified, already_current = plan(read_conn, rows)
    finally:
        read_conn.close()

    print(f"review_record_backfill: {found} stamped comment(s) found since "
          f"{SINCE}" + (f", {len(rows)} in scope for this run (--limit "
                        f"{limit})" if limit is not None else ""),
          file=sys.stderr)

    created = superseded = unchanged = relinked = 0
    # Real write-time occurrences of the two guarded outcomes — distinct
    # from `left_verified`/`already_current` above, which are `plan()`'s
    # PREDICTED buckets. A `planned` item can still resolve to either at
    # write time: `plan()`'s read of a key's state is a snapshot, and the
    # store-level guard re-checks it fresh, inside the write's own
    # transaction, against whatever is true right now (see `plan` and
    # `store.write_review_record`'s own docstring).
    write_left_verified: list[dict] = []
    already_current_at_write = 0
    for i, item in enumerate(planned, 1):
        if write:
            _status, outcome, out = store.write_review_record(
                item["row"], at=item["produced_at"], skip_if_verified=True,
                only_if_after=(item["produced_at"], item["signal_id"]))
            # Gate on the record's ACTUAL current link, not on the write
            # outcome, and only for an outcome that means THIS item's
            # content is what the record now holds (`created`, `superseded`,
            # or `unchanged` reflecting this exact comment) — `left_verified`
            # and `already_current` both mean the record's real content is
            # something else, and relinking either to this item's signal
            # would misattribute it. `write_review_record` always inserts or
            # supersedes with `signal_id` NULL (store.py's INSERT `VALUES`
            # clause and `_supersede_review_record`'s own UPDATE both
            # hardcode it), so the link is always a second, independent
            # write — and if a prior `--write` pass died between the two, a
            # resumed pass recomputes the same `content_sha`,
            # `write_review_record` answers `unchanged`, and an
            # outcome-gated check would skip the link forever. Checking for
            # `None` specifically, rather than any difference from
            # `item["signal_id"]`, matters here too: two DIFFERENT stamped
            # comments can share one unchanged record (an identical
            # re-post), and the record's existing link to whichever comment
            # created it is a real answer, not a gap to repair — only an
            # actual NULL means the link never landed.
            if (outcome not in (store.REVIEW_OUTCOME_LEFT_VERIFIED,
                                store.REVIEW_OUTCOME_ALREADY_CURRENT)
                    and out.get("signal_id") is None):
                store.set_review_record_signal(
                    out["id"], out["version"], item["signal_id"])
                if outcome == store.REVIEW_OUTCOME_UNCHANGED:
                    relinked += 1
        else:
            outcome = item["predicted_outcome"]
            print(f"review_record_backfill: {_OUTCOME_VERB[outcome]} for "
                  f"signal {item['signal_id']} (comment {item['comment_id']})")
        if outcome == store.REVIEW_OUTCOME_CREATED:
            created += 1
        elif outcome == store.REVIEW_OUTCOME_SUPERSEDED:
            superseded += 1
        elif outcome == store.REVIEW_OUTCOME_UNCHANGED:
            unchanged += 1
        elif outcome == store.REVIEW_OUTCOME_LEFT_VERIFIED:
            write_left_verified.append(item)
        elif outcome == store.REVIEW_OUTCOME_ALREADY_CURRENT:
            already_current_at_write += 1
        if write and i % FLUSH_EVERY == 0:
            print(f"review_record_backfill: processed {i}/{len(planned)}",
                  file=sys.stderr)

    for lv in left_verified:
        print(f"review_record_backfill: left {lv['signal_id']} (comment "
              f"{lv['comment_id']}) alone — the stored record for this key "
              f"is already run-verified, and a backfilled comment must "
              f"never supersede it", file=sys.stderr)
    for lv in write_left_verified:
        print(f"review_record_backfill: left {lv['signal_id']} (comment "
              f"{lv['comment_id']}) alone — a live write marked this key "
              f"run-verified while this run was still in progress, after "
              f"plan() read it as not yet verified", file=sys.stderr)

    by_reason: dict[str, list[dict]] = {}
    for s in skipped:
        by_reason.setdefault(s["reason"], []).append(s)
    for reason, entries in by_reason.items():
        for s in entries:
            print(f"review_record_backfill: skipped {s['signal_id']} — "
                  f"{reason}: {s['detail']}", file=sys.stderr)

    total_left_verified = len(left_verified) + len(write_left_verified)
    total_already_current = len(already_current) + already_current_at_write

    # Not printed per-row like `left_verified` above: a repeat `--write`
    # pass over the full population lands almost every row here (measured
    # at 10,703 of 10,978 on a second pass), and a line per row would
    # flood the operator watching this run with nothing to act on. The
    # count in the summary line below is what a person watching it needs.
    print(f"review_record_backfill: found {found}"
          + (f" ({len(rows)} in scope)" if limit is not None else "")
          + f", written {created}, superseded {superseded}, "
            f"no-op {unchanged}, left {total_left_verified} verified, "
            f"already current {total_already_current}, "
            f"skipped {len(skipped)}"
          + (f", relinked {relinked}" if write and relinked else "")
          + (f" ({', '.join(f'{len(v)} {k}' for k, v in by_reason.items())})"
             if by_reason else ""),
          file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true",
        help="actually write the records; without it this only reports "
             "what would happen")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="process at most this many matched comments, oldest first — "
             "for a bounded first pass")
    args = parser.parse_args(argv)
    return run(write=args.write, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
