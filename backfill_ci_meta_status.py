"""One-time backfill: re-key the CI-poller rows already in the Archives so
`meta.status` and `meta.run_url` are populated the way
`convivy-orient/engineering_e2e_build.py` reads them
(issue_01M1F431KC6DPYY2D185GD036J).

`bin/ci_result_fetch.py` writes `meta.status`/`meta.run_url` for every run it
captures from here on — this script is only for the rows it already wrote
before that fix landed (fourteen of them, at the time this was written).
Every one of those rows already carries `refs.conclusion` and `refs.run_url`
(`ci_result_fetch.signal_from_run`'s shape, unchanged by the fix), so this is
a pure local re-key: no `gh api` call, no network, no dependency on the run
that produced the row still existing on GitHub.

**`refs` is left exactly as it is**, for the same reason `ci_result_fetch.py`
leaves it alone: `refs.conclusion` is the only place the raw, un-mapped
GitHub value survives once `meta.status`'s mapping — fail-closed for
anything outside `success`/`failure` — has thrown information away.

**`--dry-run` is the default.** This runs against the Library's live,
264 MB production store, serving Orient and the ops tools right now —
reporting what would change costs nothing; writing to it is a call made
after reading that report. `--write` is the one flag that turns this from a
report into an update.

**Idempotent by construction, not by a guard bolted on afterward**: the
selection is "a row with `refs.conclusion` and no `meta.status` yet"
(`_needs_backfill`), so a row this script has already fixed no longer
matches and a second `--write` pass is a clean no-op over it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# `bin` holds `ci_result_fetch.py` and nothing importable elsewhere in this
# repo depends on it being on `sys.path` — pytest.ini adds it the same way,
# for the same reason: `conclusion_to_meta_status` must be the one mapping
# both the writer and this backfill use, not two copies that can drift.
sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))
from ci_result_fetch import conclusion_to_meta_status  # noqa: E402

import store  # noqa: E402


def _needs_backfill(refs: dict, meta: dict) -> bool:
    """A row this script should touch: the pre-fix poller shape (a
    `refs.conclusion` and no `meta.status` yet). A row that already has
    `meta.status` — whether this script put it there or the retired push
    path did — is left alone.

    **The two populations were measured, not inferred**: over the 46
    `source='ci'` rows in the live store on 2026-09-01, 14 carried
    `refs.conclusion` with no `meta.status` and 32 the reverse, with no
    overlap. That was a query against the Archives rather than anything
    `bin/ci_result_fetch.py` states about itself, and this predicate does
    not depend on it holding — it decides per row."""
    return "conclusion" in refs and "status" not in meta


def _load_ci_rows(conn):
    # **Selection is tied to provenance as well as shape** (security review,
    # 2026-09-01). `POST /signals` lets any caller holding `signal.create`
    # choose `source` and `refs`, so "source='ci' with a refs.conclusion" is a
    # shape anyone can mint. Requiring the fetcher's own deterministic
    # `ci:<product>:<workflow-slug>:<run-id>` key as well does not make that
    # unforgeable, and it does mean a re-run cannot relabel a row that never
    # came through this path on shape alone. Both populations in the store
    # already carry it, so nothing legitimate is excluded.
    return conn.execute(
        "SELECT id, refs, meta FROM signals "
        "WHERE source = 'ci' AND dedup_key LIKE 'ci:%'").fetchall()


def plan(conn) -> list[dict]:
    """Every row this backfill would touch, as `{id, refs, meta, new_meta}`
    — read-only, and shared by the dry-run report and the write pass so the
    two can never disagree about which rows qualify."""
    changes = []
    for row in _load_ci_rows(conn):
        refs = store._json_or(row["refs"], {})
        meta = store._json_or(row["meta"], {})
        if not _needs_backfill(refs, meta):
            continue
        new_meta = dict(meta)
        status = conclusion_to_meta_status(refs.get("conclusion"))
        if status is not None:
            new_meta["status"] = status
        if refs.get("run_url") is not None:
            new_meta["run_url"] = refs["run_url"]
        changes.append({"id": row["id"], "refs": refs, "meta": meta,
                        "new_meta": new_meta})
    return changes


def run(*, write: bool) -> int:
    conn = store.connect_rw() if write else store.connect()
    try:
        changes = plan(conn)
        print(f"backfill_ci_meta_status: {len(changes)} row(s) to update",
              file=sys.stderr)
        for change in changes:
            verb = "updating" if write else "would update"
            print(f"backfill_ci_meta_status: {verb} {change['id']}: "
                  f"meta {change['meta']!r} -> {change['new_meta']!r}")
            if write:
                conn.execute("UPDATE signals SET meta = ? WHERE id = ?",
                             (json.dumps(change["new_meta"]), change["id"]))
        if write:
            conn.commit()
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true",
        help="actually update the rows; without it this only reports what "
             "would change")
    args = parser.parse_args(argv)
    return run(write=args.write)


if __name__ == "__main__":
    sys.exit(main())
