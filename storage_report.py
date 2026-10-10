"""storage_report — per-tier size and growth, and per-kind vector-index
composition, projected for the scale the memory-retention epic exists to
survive (clauses 10-11, issue_01M35R5XRDJBWZFJ87NTGJVTF5, built on the cold
tier of issue_01M35R5QKX7QDCM822K3J18SWX and the kind rules of
library/decisions/0001-what-the-archives-keep-embed-and-move-cold).

**No byte threshold is stated anywhere this module can read from.** The
decision and the cold-tier build plan (library/specs/archives-cold-tier.md)
state only AGE windows — how long a signal stays hot before it moves —
never a size at which a tier is full. So `threshold_bytes` is a caller-
supplied argument, never a constant this module invents, and every tier's
`threshold_date` is `None` until one is given.

**Sizes are measured through `dbstat`, not a whole file's size on disk, and
only over the exact object names `sqlite_master` names.** `library.db` also
holds the Chart House (work items, courses, leases), and `vectors.db` holds
`signal_vectors` beside `library_vectors` — dead data
(`reduce_vector_index.py`'s own docstring: "nothing in this service still
queries `signal_vectors`"). `dbstat` reports the exact page bytes a table
and its own shadow objects occupy, so neither neighbour inflates a tier's
measured size. A vec0 virtual table's shadow tables and their autoindexes
are all named with the table's own name as a substring
(`library_vectors_chunks`, `sqlite_autoindex_library_vectors_info_1`, ...),
and `signal_vectors`'s own objects never match `%library_vectors%`, which is
what keeps the two apart with a single `LIKE` — **run against
`sqlite_master`, which costs no data pages, never against `dbstat` itself**,
whose own `name` column takes no pushdown on a `LIKE` (reviewer +
security-reviewer, PR #203 round 1: an unfiltered `dbstat('main')` walks
every page of the whole schema, `signals` included, on `library.db`'s own
event loop). The matched names are then passed to `dbstat` as an `IN (...)`
list, which SQLite does push down to an equality seek per name.

**`tier_sizes` is cached for `TIER_SIZES_CACHE_TTL_SECONDS`.** The dbstat
walk's cost tracks total file size, not the row count anyone is asking
about, and the answer does not depend on who is asking — only on how much
data exists, which a few seconds of staleness does not meaningfully change.
The same one-fetch-per-burst shape `runs.snapshot` uses for the same reason.
Call `reset_tier_sizes_cache()` to force a fresh read (tests do, between an
insert and the next measurement).

**The growth rate is linear, projected from today's ingestion, and does not
model the cold-tier mover's outflow from hot.** A signal lands hot and
later moves to `cold.db` once its kind's hot window passes (`cold_tier.py`).
This module does not subtract that outflow when projecting the hot tier's
own growth, so the hot-tier projection is an upper bound on its real
steady-state size, never an under-estimate. The cold tier and the vector
index carry no equivalent outflow today — the retention decision's
Discarded section names no kind, so nothing is swept — and their
projections are not biased the same way.

**`wide` controls whether tier sizes and growth reach the caller at all.**
`tier_sizes` measures physical bytes in three shared database files — they
are not signal content and cannot be scoped per product in the first place
(one file, every product's rows in it). `report`'s `where`/`params` already
scope `by_kind`, `rows_ingested` and `bytes_ingested` to what the caller may
read, the same way `GET /signals/facets` scopes `total` and `by_kind` — but
a `tiers` block built the same way as before scoping was added would still
hand a single-product caller the platform-wide byte totals and the
platform-wide project count, including customer-class and private-crew
data (security-reviewer, PR #203 round 1). `wide=False` withholds the whole
`tiers` block and `measurement.current_project_count` instead of scoping
them, because there is no narrower figure to compute — a physical fact
about a shared file has no per-product share. `wide=True` is the default so
existing direct callers of this module are unaffected; `GET /signals/storage`
decides `wide` itself, and holding `"*"` in the caller's products is one of
three conditions that route checks, not the whole test — see
`app._storage_report_wide`.

**The vectors tier's growth ratio is computed from two UNSCOPED totals,
deliberately, even under a scoped `where` — unscoped by customer class and
by the handoff wall too, not only by product.** `bytes_per_index_byte`
divides `tier_sizes`'s platform-wide index bytes by the embedded content
ever ingested — also platform-wide, ignoring `where` entirely — because the
numerator is a physical fact with no product scope to match. Scoping only
the denominator (dividing the whole index by one product's own slice of
embedded bytes) inflates the ratio for every scoped caller by roughly
(platform total ÷ that caller's own total) — reviewer, PR #203 round 1,
worked example: a 10x overstatement for a product holding a tenth of all
embedded content. The window's own embedded bytes (`bytes_embedded_window`,
scoped to the caller, exactly like `bytes_ingested`) are what the ratio is
then applied to, which is why that term keeps its scope while the ratio
itself does not.

**A customer-class or handoff-walled row can land in that denominator, so
`report` is never called with `wide=True` for a caller who could not read
every such row anyway** (security-reviewer, PR #203 round 2: a `"*"`-scoped
caller without `signal.customer`, or one a handoff wall still restricts,
would otherwise receive a figure derived from rows it cannot read through
any other route). That gate lives in `app._storage_report_wide`, not here —
this module computes the platform-wide figure the same way regardless of
who asked, once asked to; deciding who may ask is the route's job, the same
split `wide` itself already draws between "compute" and "whether to show it
to this caller".

**`bytes_embedded_total`, the ratio's denominator, is served from the same
`TIER_SIZES_CACHE_TTL_SECONDS` window `tier_sizes` uses, through
`_embedded_bytes_total`, keyed per `source`** — it is an unbounded, all-time
scan of every embedded-kind row across both tiers, reading `body` and `meta`
on `kind`, a column with no index, and the answer does not depend on who is
asking any more than `tier_sizes`'s does (reviewer, PR #203 round 2
non-blocking: uncached and unscoped, it ran in full on every platform-wide
request). Keying on `source` rather than time alone means a future caller
passing a differently-scoped `source` gets its own measurement instead of
whatever an earlier caller's `source` computed (security-reviewer, PR #203
round 3 suggestion) — today's sole caller always passes the same unscoped
`store.signals_source()`, so this never grows past one entry yet.
`reset_embedded_total_cache()` forces a fresh read, the same contract
`reset_tier_sizes_cache()` makes.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
import time

import store
import vectors

# How long a `tier_sizes` measurement is served from cache before the next
# caller pays for a fresh `dbstat` walk — see the module docstring.
TIER_SIZES_CACHE_TTL_SECONDS = 60.0

TIER_HOT = "hot"
TIER_COLD = "cold"
TIER_VECTORS = "vectors"
TIERS = (TIER_HOT, TIER_COLD, TIER_VECTORS)

# How far back `_ingestion` looks to measure today's rate. Long enough that a
# quiet day does not read as a stalled system, short enough that the rate
# reflects current load rather than a quarter of it — the same trade-off
# `librarian_metrics.DEFAULT_WINDOW_DAYS` makes for the same reason.
DEFAULT_GROWTH_WINDOW_DAYS = 30

# The epic's own scale: twenty concurrent projects, five years
# (issue_01M2F52JSSTEW7EM9TMB342J15, "the design holds at the scale it is
# for").
PROJECT_COUNT_AT_SCALE = 20
PROJECTION_HORIZON_YEARS = 5
DAYS_PER_YEAR = 365


def _matching_names(conn, schema: str, name_like: str) -> list[str]:
    """Every object name in `schema` (a table, its own indexes, or a vec0
    virtual table's shadow tables and their autoindexes) containing
    `name_like` — read from `sqlite_master`, which costs no data pages, so
    this can run ahead of `dbstat` without itself being the full-schema walk
    `dbstat`'s own `LIKE` used to be.

    **An unattached `schema` answers no names, matching `dbstat`'s own
    silent-empty read of a schema name it does not recognize** — unlike
    `dbstat`, a schema-qualified `sqlite_master` raises rather than reading
    empty when `schema` is not attached, and `tier_sizes`'s own contract is
    that attaching is the caller's job, not this function's: a caller that
    attached only `cold` and not `vecdb` still gets real hot/cold sizes
    (`test_hot_and_cold_tier_sizes_grow_with_their_own_rows`), the same as
    it did with the plain `dbstat` scan before this function existed.
    """
    try:
        rows = conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE name LIKE ?",
            (f"%{name_like}%",)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def _schema_bytes(conn, schema: str, name_like: str) -> int:
    """Physical bytes `dbstat` attributes to every object in `schema` whose
    name contains `name_like` — a table, its own indexes, and, for a vec0
    virtual table, its shadow tables and their autoindexes.

    **Queries `dbstat` with `name IN (...)`, an equality constraint SQLite
    pushes down to a per-name seek, never `name LIKE`** — `dbstat` gives a
    `LIKE` filter on `name` no pushdown at all, so it walks every page of
    every object in `schema` before filtering (reviewer + security-reviewer,
    PR #203 round 1). The exact names are found first, from `sqlite_master`.
    """
    names = _matching_names(conn, schema, name_like)
    if not names:
        return 0
    placeholders = ",".join("?" * len(names))
    row = conn.execute(
        f"SELECT SUM(pgsize) FROM dbstat(?) WHERE name IN ({placeholders})",
        (schema, *names)).fetchone()
    return row[0] or 0


_tier_sizes_lock = threading.Lock()
_tier_sizes_cache: tuple[float, dict[str, int]] | None = None


def reset_tier_sizes_cache() -> None:
    """Drop the cached measurement. For tests, and for a caller that just
    changed the data `tier_sizes` would otherwise still report stale."""
    global _tier_sizes_cache
    with _tier_sizes_lock:
        _tier_sizes_cache = None


def tier_sizes(conn, *, now: float | None = None) -> dict[str, int]:
    """Current size in bytes of each tier: the hot `signals` table in
    `main`, the cold `signals` table in the attached `cold` schema, and
    `library_vectors` alone (never `signal_vectors`) in the attached
    `vecdb` schema.

    `cold` and `vecdb` must already be attached on `conn` — this function
    does not attach either, so a caller that could not attach one reports
    the failure itself rather than this function returning a silent zero
    for a tier that is merely unreachable.

    **Served from cache inside `TIER_SIZES_CACHE_TTL_SECONDS`** — one
    `dbstat` walk per burst rather than one per request, the same one-fetch
    shape `runs.snapshot` uses, for the same reason (the answer does not
    depend on who asks). `reset_tier_sizes_cache()` forces a fresh read.
    """
    global _tier_sizes_cache
    now = time.monotonic() if now is None else now
    with _tier_sizes_lock:
        if (_tier_sizes_cache is not None
                and (now - _tier_sizes_cache[0]) < TIER_SIZES_CACHE_TTL_SECONDS):
            return _tier_sizes_cache[1]
        fresh = {
            TIER_HOT: _schema_bytes(conn, "main", "signals"),
            TIER_COLD: _schema_bytes(conn, store.COLD_SCHEMA, "signals"),
            TIER_VECTORS: _schema_bytes(conn, "vecdb", vectors.VEC_TABLE),
        }
        _tier_sizes_cache = (now, fresh)
        return fresh


def _content_bytes(conn, source: str, where: str, params: list,
                   since: str | None) -> tuple[int, int]:
    """(rows, content bytes) matching `where` over `source`
    (`store.signals_source()`, aliased `signals`, with `cold` already
    attached), optionally restricted to `ingested_at >= since` — `since`
    `None` answers the all-time total.

    **Counted from the moment of ingestion, in whichever tier currently
    holds the row.** A signal lands hot and some kinds move cold the same
    day (`vectors.DEFAULT_HOT_WINDOW_DAYS` is 0, and `notification`'s own
    window is 0 days too), so a query against `main.signals` alone would
    already miss same-day ingestion once a mover pass has run. The two-tier
    source counts it either way.

    Bytes are `length(body) + length(meta)`, the same logical measure
    `cold_tier.py`'s own batch sizing uses — not file bytes, which `dbstat`
    already answers for the tier's current total in `tier_sizes`.

    **`where` must already be a complete clause, `WHERE ...` included** —
    `GET /signals/facets`'s own convention (its `where` starts `"WHERE 1 =
    1"` before any scope or wall is appended), so this function can always
    append `AND ingested_at >= ?` rather than deciding whether `where` is
    empty.
    """
    extra = " AND ingested_at >= ?" if since is not None else ""
    bind = [*params, since] if since is not None else list(params)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, "
        f"COALESCE(SUM(length(body) + length(meta)), 0) AS bytes "
        f"FROM {source} AS signals {where}{extra}", bind).fetchone()
    return row["n"], row["bytes"]


_embedded_total_lock = threading.Lock()
# Keyed on `source`, the one other value this measurement varies over
# (`vectors.EMBEDDED_SIGNAL_KINDS`, the only other filter, is a module
# constant, not a caller-supplied one) — today's sole call site
# (`report`) always passes the same unscoped `store.signals_source()`, so
# this cache only ever grows one entry, but keying on `source` means a
# future caller passing a differently-scoped one gets its own measurement
# rather than silently inheriting whichever source first populated the
# cache (security-reviewer, PR #203 round 3 suggestion).
_embedded_total_cache: dict[str, tuple[float, int]] = {}


def reset_embedded_total_cache() -> None:
    """Drop every cached measurement. For tests, and for a caller that just
    changed the data `_embedded_bytes_total` would otherwise still report
    stale — the same contract `reset_tier_sizes_cache` makes for
    `tier_sizes`."""
    global _embedded_total_cache
    with _embedded_total_lock:
        _embedded_total_cache = {}


def _embedded_bytes_total(conn, source: str, *,
                          now: float | None = None) -> int:
    """All-time embedded content bytes across every product, customer-class
    and handoff-walled rows included — the vectors-growth ratio's unscoped
    denominator (see the module docstring's `bytes_per_index_byte`
    paragraph).

    **Served from `TIER_SIZES_CACHE_TTL_SECONDS`, the same window
    `tier_sizes` uses, and for the same reason**: an unbounded scan of every
    embedded-kind row across both tiers, reading `body` and `meta` on `kind`
    — a column with no index — and the answer does not depend on who is
    asking, only on how much has been embedded (reviewer, PR #203 round 2
    non-blocking). `reset_embedded_total_cache()` forces a fresh read.

    **Cached per `source`**, not on time alone, so a caller passing a
    differently-scoped `source` than an earlier caller gets its own
    measurement rather than whatever the first caller's `source` happened
    to compute (security-reviewer, PR #203 round 3 suggestion).
    """
    global _embedded_total_cache
    now = time.monotonic() if now is None else now
    with _embedded_total_lock:
        cached = _embedded_total_cache.get(source)
        if cached is not None and (now - cached[0]) < TIER_SIZES_CACHE_TTL_SECONDS:
            return cached[1]
        where = (f"WHERE kind IN "
                f"({','.join('?' * len(vectors.EMBEDDED_SIGNAL_KINDS))})")
        params = list(vectors.EMBEDDED_SIGNAL_KINDS)
        _, total = _content_bytes(conn, source, where, params, since=None)
        _embedded_total_cache[source] = (now, total)
        return total


def _vector_kind_counts(conn, source: str, where: str,
                        params: list) -> dict[str, int]:
    """`library_vectors` row counts by the embedding signal's own kind, over
    `vectors.VAULT_ARCHIVES` (the Chart House's vault holds work items, not
    signals, so no other vault belongs here) and the two-tier `source`, so a
    cold signal's vectors — left in place by the cold-tier mover
    (archives-cold-tier.md §2: "vectors stay in vectors.db") — still count
    under their kind.
    """
    rows = conn.execute(
        f"SELECT signals.kind AS kind, COUNT(*) AS n "
        f"FROM {source} AS signals "
        f"JOIN {vectors.VEC_TABLE} lv ON lv.item_id = signals.id "
        f"{where} AND lv.vault = ? "
        f"GROUP BY signals.kind", [*params, vectors.VAULT_ARCHIVES]).fetchall()
    return {r["kind"]: r["n"] for r in rows}


def kind_shares(conn, source: str, where: str, params: list) -> list[list]:
    """Every kind present in `library_vectors`, with its row count and its
    share of the index, as `[[kind, count, share], ...]` descending by
    count — `reduce_vector_index.py`'s own `_kind_report` shape, extended to
    the two-tier source now that a signal's vectors can belong to a cold
    row."""
    counts = _vector_kind_counts(conn, source, where, params)
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[kind, n, round(n / total, 4) if total else None]
            for kind, n in ordered]


def _project_forward(size_bytes: int, bytes_per_day_at_scale: int, *,
                     now: _dt.datetime,
                     threshold_bytes: int | None) -> dict:
    """One tier's growth report: its projected size at the epic's own scale
    and horizon, and the date it crosses `threshold_bytes` — `None` when no
    threshold was given, or when the projected growth never reaches one
    that was.
    """
    horizon_days = PROJECTION_HORIZON_YEARS * DAYS_PER_YEAR
    projected_size = size_bytes + bytes_per_day_at_scale * horizon_days
    threshold_date = None
    if threshold_bytes is not None:
        if size_bytes >= threshold_bytes:
            threshold_date = now.date().isoformat()
        elif bytes_per_day_at_scale > 0:
            days = (threshold_bytes - size_bytes) / bytes_per_day_at_scale
            threshold_date = (now + _dt.timedelta(days=days)).date().isoformat()
        # else: growth is zero or negative at this scale — never reached,
        # and `threshold_date` stays `None` rather than reporting a date
        # arithmetic would otherwise place in the past or never.
    return {
        "available": True,
        "reason": None,
        "size_bytes": size_bytes,
        "growth_bytes_per_day": bytes_per_day_at_scale,
        "projected_size_bytes_at_horizon": round(projected_size),
        "threshold_bytes": threshold_bytes,
        "threshold_date": threshold_date,
    }


# The fallback reason `_withheld_tier`/`report` use when no caller passes a
# more specific one — see `app._storage_report_wide`, which names the exact
# one of the three gates below that stopped THIS caller, and is what
# `GET /signals/storage` actually hands `report`'s `withheld_reason`. This
# constant states the rule itself for the one remaining caller of this
# module that withholds without naming which gate — there is none today,
# but `report(wide=False)` without `withheld_reason` must not describe a
# narrower rule than the one it enforces.
_TIERS_WITHHELD_REASON = (
    "size and growth are physical facts about library.db, cold.db and "
    "vectors.db, each one file shared by every product, with no per-product "
    "share to report instead of the platform-wide total; only an identity "
    "scoped to every product (\"*\"), holding signal.customer, and not "
    "currently excluded by a handoff wall sees this block — see "
    "GET /signals/storage's docstring for all three conditions")


def _withheld_tier(threshold_bytes: int | None, reason: str) -> dict:
    """One tier's report when the caller does not get the wide block —
    every other field here comes from the platform-wide physical fact
    `report` just chose not to compute at all for this caller (see
    `tier_sizes` and the module docstring), so none of it is returned.
    `threshold_bytes` alone survives: it is the caller's own query
    parameter, echoed back, never a platform fact. `reason` is the rule
    `report` was given for this specific withholding — see its own
    `withheld_reason` parameter.
    """
    return {
        "available": False,
        "reason": reason,
        "size_bytes": None,
        "growth_bytes_per_day": None,
        "projected_size_bytes_at_horizon": None,
        "threshold_bytes": threshold_bytes,
        "threshold_date": None,
    }


_NOTE_GROWTH_MODEL = (
    "cold and vector-index growth are not swept by anything "
    "live today (the retention decision's Discarded section "
    "names no kind), so their projections track ingestion "
    "directly; the hot tier's does too, which is an upper "
    "bound on its real size since the cold-tier mover's "
    "outflow is not modelled here. No byte threshold is "
    "stated in the memory-retention epic or in the cold-tier "
    "build plan for any tier — pass threshold_bytes to "
    "compute a crossing date against one of your own.")


def report(conn, *, source: str, where: str, params: list,
          current_project_count: int, window_days: int,
          threshold_bytes: dict[str, int] | None = None,
          now: _dt.datetime | None = None, wide: bool = True,
          withheld_reason: str | None = None) -> dict:
    """The full report: every tier's current size and growth, projected to
    `PROJECT_COUNT_AT_SCALE` concurrent projects over
    `PROJECTION_HORIZON_YEARS`, and `library_vectors`'s composition by kind.

    `current_project_count` is the number of projects the ingestion below
    is already divided among — the per-project rate is today's ingestion
    rate divided by this count, then multiplied back up to
    `PROJECT_COUNT_AT_SCALE`. A count of zero makes a per-project rate
    undefined, so every tier's growth is reported as zero rather than by a
    division this caller cannot have performed truthfully.

    `threshold_bytes` is an optional `{tier: bytes}` mapping — a caller-
    supplied number, never a constant this module holds (see the module
    docstring). A tier absent from it gets `threshold_date: None`.

    `wide` withholds `tiers` and `measurement.current_project_count` when
    `False` — see the module docstring's `wide` paragraph for why a scoped
    caller gets those withheld rather than a per-product figure that does
    not exist, and for why the vectors-growth ratio this computes when
    `True` is unscoped by customer class and the handoff wall too, not only
    by product — which is why `wide` must mean the caller can read every
    row these figures touch, not merely that it holds `"*"` in its products.
    Defaults to `True` so an existing direct caller of this module keeps
    seeing everything; `GET /signals/storage` decides `wide` itself,
    through `app._storage_report_wide`.

    `withheld_reason` is the text each withheld tier's `"reason"` and the
    top-level `"note"` carry when `wide` is `False` — it names the actual
    gate that stopped this caller (`_storage_report_wide` already checks
    the three gates in order and knows which one it stopped on, so the
    caller can pass the specific one at no extra cost). Ignored when `wide`
    is `True`. Falls back to `_TIERS_WITHHELD_REASON`, which states the
    rule without naming which part of it applied, for a caller that passes
    `wide=False` without one.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    since = (now - _dt.timedelta(days=window_days)).isoformat(" ")
    threshold_bytes = threshold_bytes or {}

    rows_ingested, bytes_ingested = _content_bytes(
        conn, source, where, params, since)

    if wide:
        sizes = tier_sizes(conn)

        embedded_where = where + (
            f" AND kind IN ({','.join('?' * len(vectors.EMBEDDED_SIGNAL_KINDS))})")
        embedded_params = [*params, *vectors.EMBEDDED_SIGNAL_KINDS]
        _, bytes_embedded_window = _content_bytes(
            conn, source, embedded_where, embedded_params, since)
        # All-time AND UNSCOPED, matching `sizes[TIER_VECTORS]`'s own scope —
        # it is a physical fact about the whole shared index file, with no
        # per-product share, so the ratio's other term cannot be scoped to
        # `where` either without comparing two different populations.
        # Scoping only this term divided the whole index by one product's
        # own slice of embedded bytes, overstating the ratio by roughly
        # (platform total ÷ that product's own total) for every scoped
        # caller (reviewer, PR #203 round 1). Cached — see
        # `_embedded_bytes_total` and the module docstring.
        bytes_embedded_total = _embedded_bytes_total(conn, source)

        if current_project_count > 0:
            # The vector index does not grow with every ingested byte, only
            # with every EMBEDDED signal's own vectors, and a vector's own
            # bytes have no fixed relationship to its source text's bytes
            # (chunking, dimension, quantization). So the window's embedded
            # bytes — the caller's own, same scope as `bytes_ingested` — are
            # converted to index bytes through the measured unscoped ratio
            # before being scaled to the per-project rate, rather than
            # assumed to track source bytes 1:1.
            bytes_per_index_byte = (sizes[TIER_VECTORS] / bytes_embedded_total
                                    if bytes_embedded_total else 0.0)
            vectors_bytes_window = bytes_embedded_window * bytes_per_index_byte

            per_project_scale = PROJECT_COUNT_AT_SCALE / current_project_count
            # Everything not discarded eventually reaches cold (every kind's
            # hot window is finite except `deletion-record`'s, and an
            # unruled kind's default window is 0 days), so cold's growth
            # tracks total ingestion directly. The hot tier's growth also
            # tracks total ingestion here — an upper bound, not an exact
            # figure; see the module docstring.
            total_rate_at_scale = round(
                bytes_ingested / window_days * per_project_scale)
            hot_rate_at_scale = total_rate_at_scale
            cold_rate_at_scale = total_rate_at_scale
            vectors_rate_at_scale = round(
                vectors_bytes_window / window_days * per_project_scale)
        else:
            hot_rate_at_scale = 0
            vectors_rate_at_scale = 0
            cold_rate_at_scale = 0

        tiers = {
            TIER_HOT: _project_forward(
                sizes[TIER_HOT], hot_rate_at_scale, now=now,
                threshold_bytes=threshold_bytes.get(TIER_HOT)),
            TIER_COLD: _project_forward(
                sizes[TIER_COLD], cold_rate_at_scale, now=now,
                threshold_bytes=threshold_bytes.get(TIER_COLD)),
            TIER_VECTORS: _project_forward(
                sizes[TIER_VECTORS], vectors_rate_at_scale, now=now,
                threshold_bytes=threshold_bytes.get(TIER_VECTORS)),
        }
        reported_project_count = current_project_count
    else:
        reason = withheld_reason or _TIERS_WITHHELD_REASON
        tiers = {tier: _withheld_tier(threshold_bytes.get(tier), reason)
                for tier in TIERS}
        reported_project_count = None

    return {
        "tiers": tiers,
        "by_kind": kind_shares(conn, source, where, params),
        "measurement": {
            "window_days": window_days,
            "rows_ingested": rows_ingested,
            "bytes_ingested": bytes_ingested,
            "current_project_count": reported_project_count,
            "projected_project_count": PROJECT_COUNT_AT_SCALE,
            "projection_horizon_years": PROJECTION_HORIZON_YEARS,
        },
        "note": _NOTE_GROWTH_MODEL + (
            "" if wide else " tiers: " + reason + "."),
    }
