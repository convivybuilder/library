"""Migrate the decision plane's rows into the Archives as signals, preserving
their `dec_` ids so a citation by id keeps resolving.

`issue_01M0ASMCB4F68PF3NJG7BV7K6C`. The plane — six routes, five capabilities,
the `decision_items`/`decision_comments`/`decision_events` tables — is retired:
The operator, 2026-08-18, "Let's get rid of the feature entirely." What must NOT be
lost is the 120 rows themselves, cited by id from four KB documents and from
issue threads. This is the one-time tool that carries them across before the
tables that held them are dropped.

**One signal per decision, not one per row across three tables.** A decision's
comments and events are folded into its own signal's `meta` rather than
becoming signals of their own — the plane is retired, nothing will ever add
to either table again, so there is no ongoing append to model and no reason
to multiply 120 rows into hundreds. `meta.comments` and `meta.events` carry
the full thread and transition history verbatim.

**The id is preserved, not regenerated.** `store.insert_signal` accepts
whatever `row["id"]` the caller sets — every real caller today is a route
that mints a fresh `sig_` ULID before calling it, but nothing in the function
requires that shape. Preserving `dec_<ulid>` is what lets `GET
/signals/{id}` keep answering for a citation written before this ran, and it
is also what makes this script idempotent by construction: a second run's
inserts collide on the primary key exactly where the first run's did not,
which is checked for and skipped rather than relied upon as an accident of
`sqlite3.IntegrityError`.

**This never touches Orient's database, by construction rather than by a
special check here.** It reads and writes through `store.connect()` /
`store.insert_signal`, the same accessors every other write in this service
uses — the Orient-fingerprint refusal and the write-cutover gate both apply
for free, the same way they do to every route in `app.py`.
"""
from __future__ import annotations

import argparse
import json
import sys

import store

SOURCE = "decision-plane-retirement"
KIND = "retired-decision"
MIGRATED_BY = "migration:retire_decisions"


def _load_decisions(conn) -> list:
    return conn.execute(
        "SELECT id, product, kind, title, body, refs, state, decided_by, "
        "decided_at, directive, snooze_until, once_key, created_by, "
        "created_at, run_id, outcome_note, blocked FROM decision_items "
        "ORDER BY created_at").fetchall()


def _comments_for(conn, decision_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT actor, body, at FROM decision_comments WHERE item_id = ? "
        "ORDER BY id", (decision_id,)).fetchall()
    return [dict(r) for r in rows]


def _events_for(conn, decision_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT actor, from_state, to_state, directive, at FROM "
        "decision_events WHERE item_id = ? ORDER BY id",
        (decision_id,)).fetchall()
    return [dict(r) for r in rows]


def _already_migrated(conn, decision_id: str) -> bool:
    return conn.execute("SELECT 1 FROM signals WHERE id = ?",
                        (decision_id,)).fetchone() is not None


def build_row(dec, comments: list[dict], events: list[dict]) -> dict:
    """One decision, as a signal row ready for `store.insert_signal`."""
    body = dec["title"]
    if dec["body"]:
        body = f"{dec['title']}\n\n{dec['body']}"
    meta = {
        "decision_kind": dec["kind"],
        "state": dec["state"],
        "decided_by": dec["decided_by"],
        "decided_at": dec["decided_at"],
        "directive": dec["directive"],
        "snooze_until": dec["snooze_until"],
        "once_key": dec["once_key"],
        "outcome_note": dec["outcome_note"],
        "blocked": bool(dec["blocked"]),
        "run_id": dec["run_id"],
        "comments": comments,
        "events": events,
    }
    return {
        "id": dec["id"],
        "source": SOURCE,
        "kind": KIND,
        "actor": dec["created_by"],
        "ingested_by": MIGRATED_BY,
        # NULL: a migrated decision predates the column, and inventing a person
        # from `created_by` would claim an attestation that never happened.
        "acting_human": None,
        "product": dec["product"],
        "stream": None,
        "body": store.clip(body, store.MAX_TEXT),
        "refs": json.dumps(store._json_or(dec["refs"], {})),
        "meta": json.dumps(meta),
        # Fixed rather than derived per row: every decision is the operator's or an
        # agent's own call, never a customer's words, so none of the 120
        # qualify as customer-class content.
        "privacy_class": "internal",
        "dedup_key": None,
        "produced_at": store.parse_timestamp(dec["created_at"]),
    }


def run(*, dry_run: bool) -> int:
    conn = store.connect() if dry_run else store.connect_rw()
    try:
        decisions = _load_decisions(conn)
        print(f"retire_decisions: {len(decisions)} decision(s) on file",
              file=sys.stderr)
        migrated, skipped = 0, 0
        for dec in decisions:
            if _already_migrated(conn, dec["id"]):
                skipped += 1
                continue
            comments = _comments_for(conn, dec["id"])
            events = _events_for(conn, dec["id"])
            row = build_row(dec, comments, events)
            if dry_run:
                print(f"retire_decisions: would migrate {dec['id']} "
                      f"({len(comments)} comment(s), {len(events)} event(s))")
                migrated += 1
                continue
            status, out = store.insert_signal(row, lambda _ex: False)
            if status not in (200, 201):
                raise RuntimeError(
                    f"retire_decisions: {dec['id']} refused unexpectedly: "
                    f"{status} {out}")
            migrated += 1
        print(f"retire_decisions: {migrated} migrated, {skipped} already "
              f"present", file=sys.stderr)
        return 0
    finally:
        conn.close()


def verify() -> int:
    """Every decision id now resolves as a signal. Read-only; run after a
    real (non-dry-run) pass."""
    conn = store.connect()
    try:
        missing = conn.execute(
            "SELECT id FROM decision_items WHERE id NOT IN "
            "(SELECT id FROM signals)").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM decision_items").fetchone()[0]
        if missing:
            print(f"retire_decisions: VERIFY FAILED — {len(missing)} of "
                  f"{total} decisions have no matching signal: "
                  f"{[r['id'] for r in missing]}", file=sys.stderr)
            return 1
        print(f"retire_decisions: verified — all {total} decisions resolve "
              f"as signals", file=sys.stderr)
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would migrate without writing")
    parser.add_argument("--verify", action="store_true",
                        help="check every decision id resolves as a signal, "
                             "read-only")
    args = parser.parse_args(argv)
    if args.verify:
        return verify()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
