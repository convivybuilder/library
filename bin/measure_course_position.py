#!/usr/bin/env python3.11
"""measure_course_position — the read-time cost of GET /courses/{id}'s
`position` computation (issue_01M12NE0TT1HA3WXGW4EATE8YT clause 5), against a
real database.

`position` is computed at read time, not stored, so a later decision to
materialize it instead has to cite a measured number rather than an
estimate. This script is that measurement: it loads every live Course the
way the route does — the entries' own rows and the leases rooted on them —
and runs `lease.course_position` itself, so the number reflects the actual
code path rather than a re-description of it.

**RUN AGAINST A COPY OF THE LIVE DATABASE, NEVER THE LIVE PATH ITSELF.** This
script only reads, but a copy is what makes rerunning it safe while the
service is up.

Usage: bin/measure_course_position.py PATH/TO/a-copy-of-library.db [--repeat N]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# `store` resolves `LIBRARY_DB` at import — the same startup check every
# route runs under — and this script carries no service configuration at
# all, only the path given on its own command line. A placeholder that is
# provably not a real database satisfies that check without being read from
# by name, the same seam `tests/conftest.py` uses for the same reason; the
# read-only connection this script actually measures against is opened
# directly below, against the path given on the command line.
os.environ.setdefault(
    "LIBRARY_DB", "/nonexistent/measure-course-position-placeholder.db")

# `bin` sits one level below the repo root, where `store` and `lease` live —
# `pytest.ini`'s own `pythonpath = . bin` solves the same problem for the
# test run; a script invoked directly has no pytest.ini to read it from, so
# it adds the root itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lease
import store

DEFAULT_REPEAT = 200


def _connect_ro(path: str) -> sqlite3.Connection:
    """A read-only connection to `path`, over `store.db_uri` — the same
    percent-encoded URI the service itself opens, so `mode=ro` cannot be
    dropped by a path holding its own `?`."""
    conn = sqlite3.connect(store.db_uri(path, readonly=True), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _course_items(conn: sqlite3.Connection, entries: list[str]) -> dict:
    """The entries' own rows, the same batched shape `app.py`'s `get_course`
    fetches them in — unscoped, since this script has no caller identity to
    scope against, which changes nothing about the query this measures."""
    if not entries:
        return {}
    placeholders = ",".join("?" * len(entries))
    return {r["id"]: r for r in conn.execute(
        f"SELECT {store.ITEM_COLUMNS} FROM issue_items WHERE id IN "
        f"({placeholders})", entries)}


def measure(conn: sqlite3.Connection, *, repeat: int) -> tuple[int, int, float]:
    """`(entries, repetitions, per_call_ms)` over every live Course in
    `conn`: the entry count summed across all of them, `repeat`, and the mean
    per-call cost of `position`'s computation for one Course, averaged across
    every live Course and every repetition.
    """
    courses = conn.execute(
        "SELECT id, entries FROM courses WHERE status = ?",
        (store.COURSE_LIVE,)).fetchall()
    parsed = [(c["id"], store._json_or(c["entries"], [])) for c in courses]
    total_entries = sum(len(entries) for _course_id, entries in parsed)

    start = time.perf_counter()
    for _ in range(repeat):
        for _course_id, entries in parsed:
            items = _course_items(conn, entries)
            entry_leases = lease.live_lease_rows(conn, entries) if entries else {}
            lease.course_position(entries, items, entry_leases)
    elapsed = time.perf_counter() - start

    calls = repeat * len(parsed)
    per_call_ms = (elapsed / calls) * 1000 if calls else 0.0
    return total_entries, repeat, per_call_ms


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Time GET /courses/{id}'s `position` computation "
                     "against a real database, read-only.")
    ap.add_argument("db_path", help="a COPY of the live database — never "
                                     "the live path itself")
    ap.add_argument("--repeat", type=int, default=DEFAULT_REPEAT,
                    help=f"repetitions over every live Course (default "
                         f"{DEFAULT_REPEAT})")
    args = ap.parse_args()

    conn = _connect_ro(args.db_path)
    try:
        entries, repetitions, per_call_ms = measure(conn, repeat=args.repeat)
    finally:
        conn.close()
    print(f"entries={entries} repetitions={repetitions} "
          f"per_call_ms={per_call_ms:.4f}")


if __name__ == "__main__":
    main()
