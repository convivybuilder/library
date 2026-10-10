"""One-time backfill: correct `product` on existing `pr_state` signals so it
matches what the crew registry resolves each row's repository to
(issue_01M2K33M9NG0S9HCJQJ2EPD7RP).

`bin/pr-state-capture` (agent-platform, around line 292) mapped each
repository to a product with `signal_client.product_for_channel`, a
Slack-channel prefix heuristic that sends anything it doesn't recognise to
`platform`. The Library's own review-record write resolves products through
the crew registry instead (`app.py`'s `_review_record_product`, which calls
`projects.resolve` on the bare repo name), so a product-scoped read can't
join a PR's state to its review records. The Bosun is fixing the capture,
but the store keeps the first post of a dedup key, so rows already written
are never rewritten by a fixed capture — they have to be corrected here.

**Selection is per row: `kind = 'pr_state'` and the product the registry
resolves `meta.repo`'s bare name to differs from the row's stored
`product`.** The bare-repo derivation (`repo.rsplit("/", 1)[-1]` on an
`owner/repo` value) is `_merge_repo_scope`'s in `app.py`. `_review_record_product`
reaches the same name by a different route, group 2 of
`_REVIEW_GITHUB_IDENTITY_RE` (`^github:([^\\s/]+)/([^\\s/]+)$`), and the two
agree on every `owner/name` value: checked against all 21 distinct `meta.repo`
values in the live store on 2026-09-16, with no disagreement. They would differ
on a value with more than one slash, which the regex refuses outright.

**PRECONDITION, RECHECK IT BEFORE EVERY RUN: every identity that can read
signals is scoped `*`, or its scope is unaffected by these moves.** Changing a
row's `product` changes which callers can see it, since `in_scope` compares an
identity's `products` with that column. On 2026-09-16 every identity was `*`
except two scoped to `vivy`, and no `vivy` row moves, so the correction changed
no one's visibility. That was the whole reason it could be made without asking
the operator. If a narrowly scoped identity exists at a later run, moving a row out of
its product silently removes the row from what it can see, and moving one in
silently grants it a row. Identities come from exactly two places,
`identity._tokens()` and `identity._client_tokens()`, and every run prints each
identity's scope and names any narrow one whose products a move touches, so the
precondition is visible in the output rather than remembered. This precondition
and its refusal (`backfill_common.report_reader_scopes` /
`refuse_write_on_scope_change`) are shared with `backfill_model_resolution_product.py`
so the two scripts cannot disagree about what "safe to write" means.

**`--dry-run` is the default.** This runs against the Library's live
production store; reporting what would change costs nothing, and `--write`
is the one flag that turns the report into an update.

**Idempotent by construction, not by a guard bolted on afterward**: the
selection is "stored product differs from the registry's", so a row this
script has already corrected no longer matches and a second `--write` pass
over it is a clean no-op.

**Fails closed when the registry is unreadable.** `projects.resolve` fails
OPEN on its own (an unreadable registry makes it return its input
unchanged), which here would make every stored product look "correct"
because nothing could be resolved to compare it against — an unreadable
registry must never read as "nothing to correct". `backfill_common.registry_available`
(`projects.available()`) is checked first, and refuses (exit 2, no read, no
write) rather than resolving anything.

**Rows whose repo resolves to no product at all are left alone and
reported**, with a non-zero exit — that is a repo the crew registry does
not know, which is worth a person's attention rather than a guess.

**Only `kind = 'pr_state'` rows are touched.** `model_resolution`, `alert`
and other kinds also sit under `product = 'platform'`, but they carry no
repository to resolve from, so they are outside this query entirely.
`model_resolution` gets its own backfill, `backfill_model_resolution_product.py`,
because it resolves a repo from its run's own record rather than from `meta`.

**Only the `product` column changes.** `meta`, `refs`, `dedup_key`, `id`,
`ingested_at` and any embeddings are left exactly as they are — the update
is a single-column `UPDATE ... WHERE id = ?`.

**Batched and paced, not one statement over ~2,000 rows.** This store is
live: a writer blocks readers, and the service returns 503 after a
5-second lock wait (`store.BUSY_TIMEOUT_MS`). `backfill_common.apply_changes`
commits `BATCH_SIZE` single-row `UPDATE ... WHERE id = ?` statements (`id`
is the primary key) at a time and pauses between commits — see that
module's docstring for the incident that pacing is for and the arithmetic
behind the pause length.

**Prints each group's `repo stored->registry count` before and after
`--write`.** The old product for a given repo was deterministic (the
Slack-channel heuristic), so those per-group lines are what a person would
need to reverse the correction — a per-row dump is not needed for that.
"""
from __future__ import annotations

import argparse
import sys

import backfill_common
import store

# Rows this backfill considers at all.
KIND = "pr_state"

SCRIPT = "backfill_pr_state_product"


def _bare_repo(repo: str) -> str:
    """The bare repo name out of an `owner/repo` value. A thin alias onto
    `backfill_common.bare_repo`, kept so nothing outside this module needs to
    know the derivation moved."""
    return backfill_common.bare_repo(repo)


def plan(conn) -> tuple[list[dict], list[dict]]:
    """Every `pr_state` row whose stored `product` differs from what the
    registry resolves its `meta.repo` to, grouped by `(repo, stored
    product, registry product)` as `{repo, old_product, new_product, ids}`
    — and separately, every row whose repo resolves to no product at all,
    grouped by repo as `{repo, ids}`.

    Read-only, and shared by the dry-run report and the `--write` pass so
    the two can never disagree about which rows qualify.
    """
    groups: dict[tuple, list[str]] = {}
    unresolved: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT id, product, meta FROM signals WHERE kind = ?", (KIND,))
    for row in rows:
        meta = store._json_or(row["meta"], {})
        repo = meta.get("repo") if isinstance(meta, dict) else None
        if not isinstance(repo, str) or not repo.strip():
            unresolved.setdefault(repr(repo), []).append(row["id"])
            continue
        repo = repo.strip()
        new_product = backfill_common.resolve_product(repo)
        if new_product is None:
            unresolved.setdefault(repo, []).append(row["id"])
            continue
        if new_product == row["product"]:
            continue
        key = (repo, row["product"], new_product)
        groups.setdefault(key, []).append(row["id"])
    changes = [{"repo": repo, "old_product": old, "new_product": new, "ids": ids}
               for (repo, old, new), ids in sorted(groups.items())]
    unresolved_list = [{"repo": repo, "ids": ids}
                        for repo, ids in sorted(unresolved.items())]
    return changes, unresolved_list


def run(*, write: bool, accept_scope_change: bool = False) -> int:
    # Checked up front, before any read: `store.connect_rw` would hit this
    # same refusal on the first write anyway, but raising `WritesDisabled`
    # as an uncaught traceback out of a one-off operator script is a worse
    # failure mode than the refusal every other write path here already
    # prints cleanly (`review_record_backfill.py`'s own check is the same
    # shape).
    if write and not store.WRITES_ENABLED:
        print(f"{SCRIPT}: LIBRARY_WRITES_ENABLED is not set "
              "to '1'. --write asks to write, and this goes through "
              "store.connect_rw(), the same door every other write path in "
              "this service is refused at. Set it and run it again.",
              file=sys.stderr)
        return 2

    if not backfill_common.registry_available():
        print(f"{SCRIPT}: the crew registry is unreadable — "
              "refusing rather than correcting, since projects.resolve "
              "fails open when the registry cannot be read, which here "
              "would make every stored product look correct against "
              "nothing to compare it to", file=sys.stderr)
        return 2

    read_conn = store.connect()
    try:
        changes, unresolved = plan(read_conn)
    finally:
        read_conn.close()

    affected = backfill_common.report_reader_scopes(changes, script=SCRIPT)
    # A failed precondition REFUSES --write unless the operator names it.
    # Printing it alone would not do: an operator who goes straight to --write
    # without a dry run is exactly how a visibility change would ship unread.
    if backfill_common.refuse_write_on_scope_change(
            write=write, affected=affected,
            accept_scope_change=accept_scope_change, script=SCRIPT):
        return 2

    total_rows = sum(len(c["ids"]) for c in changes)
    print(f"{SCRIPT}: {len(changes)} group(s), "
          f"{total_rows} row(s) to correct", file=sys.stderr)
    backfill_common.print_group_lines(
        changes, verb="updating" if write else "would update", script=SCRIPT)

    if write and changes:
        backfill_common.apply_changes(changes)
        backfill_common.print_group_lines(changes, verb="updated", script=SCRIPT)

    exit_code = 0
    if affected:
        exit_code = 1
    if unresolved:
        exit_code = 1
        total_unresolved = sum(len(u["ids"]) for u in unresolved)
        print(f"{SCRIPT}: {len(unresolved)} repo(s), "
              f"{total_unresolved} row(s) left alone — repo does not "
              f"resolve to any product in the crew registry", file=sys.stderr)
        for u in unresolved:
            print(f"{SCRIPT}: unresolved repo {u['repo']!r}: "
                  f"{len(u['ids'])} row(s)", file=sys.stderr)
    return exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true",
        help="actually update the rows; without it this only reports what "
             "would change")
    parser.add_argument(
        "--accept-scope-change", action="store_true",
        help="allow --write even when a narrowly scoped reader's visibility "
             "changes; see the docstring's PRECONDITION section")
    args = parser.parse_args(argv)
    return run(write=args.write, accept_scope_change=args.accept_scope_change)


if __name__ == "__main__":
    sys.exit(main())
