"""One-off ledger history correction (issue_01M5H05DW8E50K02YB1GFNP4M4,
CQ-233): re-dates and re-lanes the Bosun's batch-posted spend entries to
each run's true finish day and lane, and backfills the runs its old batch
writer never posted at all, for 2026-10-08 through 2026-10-21 — the window
of the Bosun's own run file (the scope was widened from 2026-10-14 to
2026-10-08 by the steward, issue comment 5648, which also replaced every
count 5646 gave).

**Dedup-key contract, agreed with the Bosun (comment 5646, under the
steward's override for this item alone).** A corrected re-post of an
already-posted run uses `<run_id>:redated`; its voided original keeps the
bare run_id, and no voided row is ever deleted. A backfill of a
never-posted run uses the bare run_id — the same key the Bosun's old
batch, its new finish-time writer and its sweeper all use. Nothing
double-counts because `store.record_spend`'s dedup pre-check matches a
voided row too, and answers it as an idempotent replay without moving a
total — so a later bare-run_id post of a touched run, from the Bosun's
sweeper or anywhere else, is a no-op.

**Writes only through store functions, never raw SQL against the vault
(ADR-0020).** A correction is `store.void_spend_entry` (the function
behind `POST /ledger-entries/{id}/void`) followed by one
`store.record_spend` under the corrected dedup key; a backfill is one
`store.record_spend` under the bare run_id.

**Posted is split from unposted by joining on run_id against
`ledger_entries`, never by the run file's own `batch_eligible` hint** —
the hint names which runs the Bosun's old batch WOULD pick up, not which
it already posted.

**Every product goes through the same check `POST /ledger-entries`
applies.** `_refuse_unbookable_product` below reproduces `app.py`'s
function of the same name rather than importing it: `app.py` is this
service's ASGI entrypoint, and nothing else in this repository's one-off
backfills imports it for a check (`review_record_backfill.py` duplicated
its own stamp-parsing regex for the same reason). A refused product is
reported and never booked — neither voided nor re-posted, so a run whose
product cannot be resolved is left exactly as it was found.

**The 1,036 no-lane entries booked 2026-10-08 with no run record behind
them are reported, never touched** (comment 5648): not voided, not
re-posted, never assigned a lane. They are known-unverifiable history —
about $2,881 of that day's gateway figure is unattributed.

**A run whose only entry is voided is planned as a backfill under the
bare run_id**, because `plan` looks for an unvoided entry and finds none.
`store.record_spend` answers that post as an idempotent replay of the
voided row, so the run stays with no live entry and the dry run still
counts it among the backfills. The only way that state arises is a crash
between a correction's void and its re-post, which are two separately
committing store calls; a human finishes such a run by hand with one
`store.record_spend` under `<run_id>:redated`. The rehearsal on a copy of
the live vault on 2026-10-22 found no run in that state, so nothing here
detects it.

**Modes.** The default is a dry run that writes nothing. It prints, per
product and day, the before and after totals for both the gateway
(enforceable) and subscription lanes; lists every gateway-lane entry that
moves; lists the no-lane 2026-10-08-to-2026-10-12 entries the Bosun's
records show as subscription runs; lists every refused product; and
reports the counts and dollar totals of corrections, backfills, entries
already correct and entries left alone. `--apply` performs the writes. A
second `--apply` changes nothing: every write lands under a dedup key
that, by then, already exists.

**Writes stay under the vault's five-second busy timeout.** `--batch-size`
bounds how many write units land before this script pauses for a second,
giving a waiting writer several of SQLite's own ~100ms busy retries — the
same insurance `backfill_common.BATCH_SLEEP_SECONDS` keeps, not a response
to anything observed on this script in particular.

Tests use a synthetic registry, a synthetic run-records file and
synthetic ledger entries, never the live vault — this script must never
run against the live vault at `LIBRARY_DB` or any copy of it from
inside a test. The rehearsal on a copy of the live vault is a separate,
later step this test suite does not perform.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import projects
import store

ISSUE_ID = "issue_01M5H05DW8E50K02YB1GFNP4M4"

# `agentctl` streams fleet spend (store.LEDGER_SOURCES); this corrects
# entries the Bosun's fleet-spend writer posted under that source, and
# posts under it too, so a corrected or backfilled row's provenance reads
# the same as every other entry that writer has ever produced.
SOURCE = "agentctl"

SCRIPT_ACTOR = "ledger_history_correction"
REDATED_SUFFIX = ":redated"

# The day the Bosun's writer first ran. Fixed to this one date because
# that is what comment 5648 measured — 1,036 no-lane entries booked this
# day with no run record behind them at all — not a window this script
# scans.
UNATTRIBUTED_DAY = "2026-10-08"

# The window comment 5648 names for the no-lane entries the Bosun's
# records show as subscription runs, for the dry-run's own named list.
NO_LANE_WINDOW_START = "2026-10-08"
NO_LANE_WINDOW_END = "2026-10-12"

# How many write units (a backfill's one `record_spend`, or a
# correction's `void_spend_entry` plus `record_spend`) land before this
# script pauses — see the module docstring.
DEFAULT_BATCH_SIZE = 200
BATCH_SLEEP_SECONDS = 1.0


def _usd_micro(usd: float) -> int:
    return round(usd * 1_000_000)


def _refuse_unbookable_product(product: str) -> tuple[str | None, str | None]:
    """`(refusal_reason, resolved_product)` for a ledger product — the same
    check `POST /ledger-entries` applies through `app.py`'s function of the
    same name, reproduced here rather than imported (see the module
    docstring). `resolved_product` is None whenever `refusal_reason` is
    set."""
    private = projects.private_repos()
    crew_names = projects.private_crew_names()
    if private is None or crew_names is None:
        return "the project registry could not be read", None
    resolved = projects.resolve(product)
    if not resolved:
        return f"{product!r} is not a registered product", None
    names = {product.casefold(), resolved.strip().casefold()}
    if names & (private | crew_names):
        return f"{product!r} resolves to a private project or crew", None
    registered = store.black_box_registration_crews()
    if names & registered:
        return f"{product!r} is a registered black box", None
    return None, resolved


def _load_run_records(paths: list[Path]) -> dict[str, dict]:
    """Every run record across `paths`, keyed by `run_id`.

    The Bosun's gateway-lane file is a reported SUBSET of its main run
    file (every gateway-lane run the main file carries, nothing else).
    Loading both and requiring agreement on `day`, `lane`, `product` and
    `usd` for any run_id both name is cheap insurance against the two
    ever drifting — this script's correctness depends on there being one
    agreed set of facts per run."""
    records: dict[str, dict] = {}
    for path in paths:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                run_id = row["run_id"]
                if run_id in records:
                    prior = records[run_id]
                    for field in ("day", "lane", "product", "usd"):
                        if prior[field] != row[field]:
                            raise ValueError(
                                f"run {run_id!r} disagrees between input "
                                f"files on {field!r}: {prior[field]!r} vs "
                                f"{row[field]!r}")
                    continue
                records[run_id] = row
    return records


def plan(conn, records: dict) -> dict:
    """Sort every run record into a backfill, a correction, an already-
    correct no-op, or a refused product — read-only, so dry-run and
    `--apply` can never disagree about which rows qualify."""
    corrections: list[dict] = []
    backfills: list[dict] = []
    refused: list[dict] = []
    already_correct: list[dict] = []
    for run_id in sorted(records):
        record = records[run_id]
        active = conn.execute(
            "SELECT * FROM ledger_entries WHERE run_id = ? "
            "AND voided_at IS NULL", (run_id,)).fetchone()
        if active is None:
            reason, resolved = _refuse_unbookable_product(record["product"])
            if reason:
                refused.append({
                    "run_id": run_id, "product": record["product"],
                    "reason": reason, "usd": record["usd"], "kind": "backfill"})
                continue
            backfills.append({
                "run_id": run_id, "day": record["day"], "lane": record["lane"],
                "product": record["product"], "resolved_product": resolved,
                "agent": record.get("agent"), "model": record.get("model"),
                "item_id": record.get("item_id"), "usd": record["usd"]})
            continue
        if active["day"] == record["day"] and active["lane"] == record["lane"]:
            already_correct.append({"run_id": run_id, "entry_id": active["id"]})
            continue
        reason, resolved = _refuse_unbookable_product(record["product"])
        if reason:
            refused.append({
                "run_id": run_id, "product": record["product"],
                "reason": reason, "usd": record["usd"], "kind": "correction"})
            continue
        corrections.append({
            "run_id": run_id, "entry_id": active["id"],
            "old_day": active["day"], "old_lane": active["lane"],
            "new_day": record["day"], "new_lane": record["lane"],
            "product": record["product"], "resolved_product": resolved,
            "agent": record.get("agent"), "model": record.get("model"),
            "item_id": record.get("item_id"), "usd": record["usd"]})
    return {"corrections": corrections, "backfills": backfills,
            "refused": refused, "already_correct": already_correct}


def left_alone(conn, records: dict) -> tuple[int, int]:
    """`(count, total_usd_micro)` of unvoided, no-lane entries booked on
    `UNATTRIBUTED_DAY` whose run_id the Bosun's records do not name at
    all — known-unverifiable history, reported and never touched (issue
    comment 5648)."""
    rows = conn.execute(
        "SELECT run_id, usd_micro FROM ledger_entries WHERE day = ? "
        "AND lane IS NULL AND voided_at IS NULL",
        (UNATTRIBUTED_DAY,)).fetchall()
    count = 0
    total_micro = 0
    for row in rows:
        if row["run_id"] not in records:
            count += 1
            total_micro += row["usd_micro"]
    return count, total_micro


def _lane_deltas(plan_result: dict) -> dict[tuple, tuple[int, int]]:
    """`{(product, day): (gateway_delta_micro, subscription_delta_micro)}`
    for every (product, day) a planned write touches — a correction
    touches both its old day (removal) and its new day (addition), a
    backfill touches only its one day. `openai-subscription` never
    appears in this script's inputs, so it is not one of the two lanes
    this report tracks."""
    events: list[tuple[str, str, str | None, int]] = []
    for c in plan_result["corrections"]:
        usd_micro = _usd_micro(c["usd"])
        events.append((c["resolved_product"], c["old_day"], c["old_lane"],
                       -usd_micro))
        events.append((c["resolved_product"], c["new_day"], c["new_lane"],
                       usd_micro))
    for b in plan_result["backfills"]:
        usd_micro = _usd_micro(b["usd"])
        events.append((b["resolved_product"], b["day"], b["lane"], usd_micro))
    deltas: dict[tuple, tuple[int, int]] = {}
    for product, day, lane, signed_micro in events:
        key = (product, day)
        gateway_micro, subscription_micro = deltas.get(key, (0, 0))
        if lane == "subscription":
            subscription_micro += signed_micro
        elif lane is None or lane == "gateway":
            gateway_micro += signed_micro
        deltas[key] = (gateway_micro, subscription_micro)
    return deltas


def totals_report(conn, plan_result: dict) -> dict:
    """`{(product, day): (gateway_before, subscription_before,
    gateway_after, subscription_after)}` in micro-dollars, for every
    (product, day) a planned write touches. Read-only: `live_ledger`
    never creates a ledger row, so this is safe to call in dry-run
    mode."""
    deltas = _lane_deltas(plan_result)
    report = {}
    for key in sorted(deltas):
        product, day = key
        ledger = store.live_ledger(conn, "project", product)
        if ledger is None:
            gateway_before = subscription_before = 0
        else:
            gateway_before = store.ledger_total(conn, ledger["id"], day)
            subscription_before = store.ledger_lane_total(
                conn, ledger["id"], day, "subscription")
        gateway_delta, subscription_delta = deltas[key]
        report[key] = (gateway_before, subscription_before,
                       gateway_before + gateway_delta,
                       subscription_before + subscription_delta)
    return report


def apply_plan(plan_result: dict, *, batch_size: int, sleep=time.sleep) -> None:
    """Perform every planned correction and backfill, through
    `store.void_spend_entry` and `store.record_spend` alone, pausing every
    `batch_size` write units (see the module docstring)."""
    ledger_cache: dict[str, dict] = {}

    def get_ledger(product: str) -> dict:
        if product not in ledger_cache:
            read_conn = store.connect()
            try:
                ledger = store.live_ledger(read_conn, "project", product)
            finally:
                read_conn.close()
            if ledger is None:
                ledger = store.ensure_live_ledger("project", product)
            ledger_cache[product] = ledger
        return ledger_cache[product]

    ops = ([("correction", c) for c in plan_result["corrections"]]
           + [("backfill", b) for b in plan_result["backfills"]])
    for i, (kind, item) in enumerate(ops, 1):
        product = item["resolved_product"]
        ledger = get_ledger(product)
        usd_micro = _usd_micro(item["usd"])
        if kind == "correction":
            store.void_spend_entry(
                item["entry_id"], SCRIPT_ACTOR,
                f"{SCRIPT_ACTOR} ({ISSUE_ID}): day {item['old_day']!r}->"
                f"{item['new_day']!r}, lane {item['old_lane']!r}->"
                f"{item['new_lane']!r}")
            dedup_key = f"{item['run_id']}{REDATED_SUFFIX}"
            day, lane = item["new_day"], item["new_lane"]
        else:
            dedup_key = item["run_id"]
            day, lane = item["day"], item["lane"]
        store.record_spend({
            "ledger_id": ledger["id"], "day": day, "usd_micro": usd_micro,
            "source": SOURCE, "lane": lane, "product": product,
            "reported_by": SCRIPT_ACTOR, "run_id": item["run_id"],
            "agent": item.get("agent"), "model": item.get("model"),
            "item_id": item.get("item_id"), "dedup_key": dedup_key})
        if i % batch_size == 0:
            print(f"ledger_history_correction: processed {i}/{len(ops)}",
                  file=sys.stderr)
            if i < len(ops):
                sleep(BATCH_SLEEP_SECONDS)


def _print_dry_run_report(plan_result: dict, report: dict,
                          left_alone_count: int, left_alone_micro: int) -> None:
    print("ledger_history_correction: per product/day totals "
          "(before -> after):")
    for (product, day), (gb, sb, ga, sa) in report.items():
        print(f"  {product} {day}: gateway ${gb / 1_000_000:.2f} -> "
              f"${ga / 1_000_000:.2f}, subscription ${sb / 1_000_000:.2f} "
              f"-> ${sa / 1_000_000:.2f}")

    gateway_movers = []
    for c in plan_result["corrections"]:
        if c["old_lane"] == "gateway" or c["new_lane"] == "gateway":
            gateway_movers.append(
                f"  run {c['run_id']} ({c['product']}): corrected "
                f"{c['old_day']}/{c['old_lane']} -> {c['new_day']}/"
                f"{c['new_lane']} (${c['usd']:.2f})")
    for b in plan_result["backfills"]:
        if b["lane"] == "gateway":
            gateway_movers.append(
                f"  run {b['run_id']} ({b['product']}): backfilled "
                f"{b['day']}/{b['lane']} (${b['usd']:.2f})")
    print(f"ledger_history_correction: {len(gateway_movers)} gateway-lane "
          f"entries move:")
    for line in gateway_movers:
        print(line)

    no_lane_window = [
        c for c in plan_result["corrections"]
        if c["old_lane"] is None and c["new_lane"] == "subscription"
        and NO_LANE_WINDOW_START <= c["old_day"] <= NO_LANE_WINDOW_END]
    print(f"ledger_history_correction: {len(no_lane_window)} no-lane "
          f"{NO_LANE_WINDOW_START}-to-{NO_LANE_WINDOW_END} entries the "
          f"Bosun's records show as subscription runs:")
    for c in no_lane_window:
        print(f"  run {c['run_id']} ({c['product']}): {c['old_day']} "
              f"(${c['usd']:.2f})")

    print(f"ledger_history_correction: {len(plan_result['refused'])} "
          f"refused product(s):")
    for r in plan_result["refused"]:
        print(f"  run {r['run_id']} ({r['kind']}): product "
              f"{r['product']!r} refused: {r['reason']}")

    print(f"ledger_history_correction: left alone {left_alone_count} "
          f"no-lane {UNATTRIBUTED_DAY} entries with no run record behind "
          f"them (${left_alone_micro / 1_000_000:.2f}, known-unverifiable "
          f"history)")


def run(*, db_path: Path, runs_paths: list[Path], write: bool, batch_size: int,
        sleep=time.sleep) -> int:
    # Checked up front, before any read: `store.record_spend` and
    # `store.void_spend_entry` would hit this same refusal on the first
    # write anyway (both go through `store.connect_rw()`, the one door
    # every write in this service is refused at), but raising
    # `store.WritesDisabled` as an uncaught traceback out of a one-off
    # operator script is a worse failure mode than the same refusal every
    # other write path here already prints cleanly
    # (`review_record_backfill.py`'s own check was the same shape).
    if write and not store.WRITES_ENABLED:
        print("ledger_history_correction: LIBRARY_WRITES_ENABLED is not "
              "set to '1'. --apply asks to write, and this goes through "
              "store.connect_rw(), the same door every other write path "
              "in this service is refused at. Set it and run it again.",
              file=sys.stderr)
        return 2

    store.DB_PATH = db_path
    if not projects.available():
        print("ledger_history_correction: the crew registry is "
              "unreadable — refusing rather than resolving every "
              "product to itself (projects.resolve fails open when the "
              "registry cannot be read, which here would silently "
              "mis-scope every booking)", file=sys.stderr)
        return 2

    records = _load_run_records(runs_paths)
    conn = store.connect()
    try:
        plan_result = plan(conn, records)
        left_alone_count, left_alone_micro = left_alone(conn, records)
        report = totals_report(conn, plan_result) if not write else None
    finally:
        conn.close()

    if write:
        apply_plan(plan_result, batch_size=batch_size, sleep=sleep)
    else:
        _print_dry_run_report(plan_result, report, left_alone_count,
                              left_alone_micro)

    corrections = plan_result["corrections"]
    backfills = plan_result["backfills"]
    refused = plan_result["refused"]
    correction_total = sum(c["usd"] for c in corrections)
    backfill_total = sum(b["usd"] for b in backfills)
    print(f"ledger_history_correction: found {len(records)} run record(s); "
          f"corrections {len(corrections)} (${correction_total:.2f}), "
          f"backfills {len(backfills)} (${backfill_total:.2f}), "
          f"already correct {len(plan_result['already_correct'])}, "
          f"refused {len(refused)}, left alone {left_alone_count} "
          f"(${left_alone_micro / 1_000_000:.2f})", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "db_path", type=Path,
        help="the vault database to read and, with --apply, write")
    parser.add_argument(
        "runs_file", type=Path,
        help="the Bosun's run records (runs-since-2026-10-08.jsonl)")
    parser.add_argument(
        "gateway_runs_file", type=Path,
        help="the Bosun's gateway-lane run records, cross-checked against "
             "runs_file")
    parser.add_argument(
        "--apply", action="store_true", dest="write",
        help="perform the writes; without it this only reports what "
             "would happen")
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=f"how many write units land before this script pauses, "
             f"under the vault's busy timeout (default {DEFAULT_BATCH_SIZE})")
    args = parser.parse_args(argv)
    return run(db_path=args.db_path,
               runs_paths=[args.runs_file, args.gateway_runs_file],
               write=args.write, batch_size=args.batch_size)


if __name__ == "__main__":
    sys.exit(main())
