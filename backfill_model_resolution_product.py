"""One-time backfill: correct `product` on existing `model_resolution`
signals so it matches what the crew registry resolves each row's run's own
repository to (issue_01M2K33M9NG0S9HCJQJ2EPD7RP).

`bin/agentctl` (agent-platform, around line 5986) filed every `model_resolution`
signal under a product chosen by `signal_client.product_for_channel`, a
Slack-channel prefix heuristic that sends anything unrecognised to
`platform`. On 2026-09-16, 2,224 `model_resolution` rows sat under
`platform`. These rows record which model each dispatch used per product,
input to the cost work, so misattributing them skews that measurement.
agent-platform#603 fixes the writer; the store keeps the first post of a
dedup key, so rows already written are never rewritten by a fixed writer —
they have to be corrected here, the same shape as
`backfill_pr_state_product.py` (library#124).

**THE REPOSITORY IS NOT IN THE ROW.** `model_resolution` `meta` carries no
`repo` key — only `meta.run`, the run id (`_post_model_resolution_record`
in agent-platform's `bin/agentctl`). The run's own record is the source of
its repository: `<RUN_ROOT>/<date>/<run-id>/meta.json` has a top-level
`repo` key holding a bare repository name (e.g. `"agent-platform"`).
Measured on 2026-09-16, the 2,224 rows name 2,223 distinct runs; 2,221 of
those have a `meta.json` with `repo`, 2 run directories are already deleted
(agent-platform's runs thin at 35 days and delete at 100, so this source
shrinks daily), and 1 row names no run at all. **The body text
(`"{agent} on {repo} resolved ..."`) is never parsed as a fallback** — a
guessed repository is how these rows went wrong in the first place, and the
run's own record is the one source that isn't a guess.

**Selection is per row: `kind = 'model_resolution'` and the product the
registry resolves the row's run's `repo` to differs from the row's stored
`product`.** A repo already bare (no `/`) is unaffected by
`backfill_common.bare_repo`, so the same resolution helper
`backfill_pr_state_product.py` uses applies here unchanged.

**Four ways a row can fail to resolve, each counted and reported
separately, and each makes the run exit non-zero:**

- the row's `meta` carries no `run` at all;
- `meta.run` names a run this `RUN_ROOT` has no directory for (deleted by
  the 100-day thinning, or never existed);
- the run's `meta.json` exists but carries no usable `repo`;
- the repo is real but the crew registry does not resolve it to a product.

A row in any of these is left exactly as it is — the same rule
`backfill_pr_state_product.py` applies to an unresolvable repo. Nothing here
guesses; an unreadable source is reported, not corrected around.

**The run-directory root is configurable** (`AGENT_RUNS_ROOT`, defaulting to
`~/Convivy/agent-runs`), so tests point it at a temporary tree rather than
the real one. **The run index is built once per invocation**, one glob over
the whole tree (`_run_index`), not a glob per row — the query returns
low thousands of rows and a glob per row would each walk the same tree
again.

**PRECONDITION, the reader-scope check.** Shared with
`backfill_pr_state_product.py` via `backfill_common.report_reader_scopes` /
`refuse_write_on_scope_change`, so the two scripts cannot disagree about
what "safe to write" means. See that module's docstring, and
`backfill_pr_state_product.py`'s, for the reasoning.

**`--dry-run` is the default; `--write` turns the report into an update.**
**Idempotent by construction**: the selection is "stored product differs
from the registry's", so a row this script has already corrected no longer
matches. **Fails closed when the registry is unreadable**
(`backfill_common.registry_available`). **Only the `product` column
changes.** **Batched and paced** via `backfill_common.apply_changes` — see
that module's docstring for the live-store incident pacing exists to
prevent.

**Prints each group's `repo stored->registry count` before and after
`--write`.**
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import backfill_common
import store

# Rows this backfill considers at all.
KIND = "model_resolution"

SCRIPT = "backfill_model_resolution_product"

# The root of the run-directory tree, one date directory per day and one run
# directory per run under each. Configurable so tests use a temporary tree
# rather than the real one, which thins at 35 days and deletes at 100.
RUN_ROOT = Path(os.environ.get(
    "AGENT_RUNS_ROOT", str(Path.home() / "Convivy" / "agent-runs")))


def _run_index(root: Path) -> dict[str, Path]:
    """{run_id: path to that run's meta.json}, built with one glob over the
    whole tree rather than one glob per row — see the module docstring's
    "run index" note."""
    index: dict[str, Path] = {}
    for meta_path in root.glob("*/*/meta.json"):
        index[meta_path.parent.name] = meta_path
    return index


def _run_repo(meta_path: Path) -> str | None:
    """The `repo` a run's own `meta.json` names, or None if the file can't
    be read, isn't a JSON object, or names nothing usable. Never raises —
    an unreadable or malformed `meta.json` is exactly as unresolvable as one
    with no `repo` key at all."""
    try:
        data = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    repo = data.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return None
    return repo.strip()


def plan(conn) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Every `model_resolution` row whose stored `product` differs from
    what the registry resolves its run's `repo` to, grouped by `(repo,
    stored product, registry product)` as `{repo, old_product, new_product,
    ids}` — and, separately, four groups of rows this can't resolve and
    must leave alone:

    - `no_run`: rows whose `meta` carries no `run` id, as `{ids}`.
    - `missing_run_dir`: `meta.run` names a run with no indexed directory,
      grouped by run id as `{run, ids}`.
    - `no_repo`: the run directory exists but its `meta.json` names no
      usable `repo`, grouped by run id as `{run, ids}`.
    - `unresolved`: the repo is real but the registry doesn't resolve it to
      a product, grouped by repo as `{repo, ids}`.

    Read-only, and shared by the dry-run report and the `--write` pass so
    the two can never disagree about which rows qualify.
    """
    index = _run_index(RUN_ROOT)
    groups: dict[tuple, list[str]] = {}
    no_run: list[str] = []
    missing_run_dir: dict[str, list[str]] = {}
    no_repo: dict[str, list[str]] = {}
    unresolved: dict[str, list[str]] = {}

    rows = conn.execute(
        "SELECT id, product, meta FROM signals WHERE kind = ?", (KIND,))
    for row in rows:
        meta = store._json_or(row["meta"], {})
        run_id = meta.get("run") if isinstance(meta, dict) else None
        if not isinstance(run_id, str) or not run_id.strip():
            no_run.append(row["id"])
            continue
        run_id = run_id.strip()

        meta_path = index.get(run_id)
        if meta_path is None:
            missing_run_dir.setdefault(run_id, []).append(row["id"])
            continue

        repo = _run_repo(meta_path)
        if repo is None:
            no_repo.setdefault(run_id, []).append(row["id"])
            continue

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
    no_run_list = [{"ids": no_run}] if no_run else []
    missing_run_dir_list = [{"run": run, "ids": ids}
                             for run, ids in sorted(missing_run_dir.items())]
    no_repo_list = [{"run": run, "ids": ids}
                     for run, ids in sorted(no_repo.items())]
    unresolved_list = [{"repo": repo, "ids": ids}
                        for repo, ids in sorted(unresolved.items())]
    return changes, no_run_list, missing_run_dir_list, no_repo_list, unresolved_list


def run(*, write: bool, accept_scope_change: bool = False) -> int:
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
        changes, no_run, missing_run_dir, no_repo, unresolved = plan(read_conn)
    finally:
        read_conn.close()

    affected = backfill_common.report_reader_scopes(changes, script=SCRIPT)
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

    if no_run:
        exit_code = 1
        total = sum(len(g["ids"]) for g in no_run)
        print(f"{SCRIPT}: {total} row(s) left alone — no meta.run at all",
              file=sys.stderr)

    if missing_run_dir:
        exit_code = 1
        total = sum(len(g["ids"]) for g in missing_run_dir)
        print(f"{SCRIPT}: {len(missing_run_dir)} run(s), {total} row(s) "
              f"left alone — no run directory found", file=sys.stderr)
        for g in missing_run_dir:
            print(f"{SCRIPT}: missing run directory {g['run']!r}: "
                  f"{len(g['ids'])} row(s)", file=sys.stderr)

    if no_repo:
        exit_code = 1
        total = sum(len(g["ids"]) for g in no_repo)
        print(f"{SCRIPT}: {len(no_repo)} run(s), {total} row(s) left "
              f"alone — the run's meta.json names no usable repo",
              file=sys.stderr)
        for g in no_repo:
            print(f"{SCRIPT}: run with no repo {g['run']!r}: "
                  f"{len(g['ids'])} row(s)", file=sys.stderr)

    if unresolved:
        exit_code = 1
        total = sum(len(u["ids"]) for u in unresolved)
        print(f"{SCRIPT}: {len(unresolved)} repo(s), "
              f"{total} row(s) left alone — repo does not "
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
