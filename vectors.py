"""The vector index: KNN over it, the embeddings that fill it, and the writes
that keep it honest.

Contract §3's `GET /signals/search`, §5's "revising text invalidates its vectors",
and the embed every write route owes the index. **This is a lift of Orient's
semantics, not a redesign** — Phase 3 moves the vaults behind the protocol as a
data migration, so a difference here would be a change somebody has to argue for.

**`sqlite-vec` MUST be loaded on the connection.** A plain `sqlite3.connect`
reports `no such module: vec0` against the very same file: a vec0 virtual table is
invisible to a connection that did not load the extension, which is why alembic's
connection cannot see `library_vectors` while an app session can. That seam is
recorded in `platform/reference/keel-goal-baselines`, and ADR-0034 carries the
write side of it. It loads fine on a READ-ONLY connection, so the shadow's
guarantee is unaffected.

**Embedding the caller's QUERY is not a model on the checkout path.** ADR-0019
forbids a model deciding what a run receives; it does not forbid turning a
caller's own words into a vector so a deterministic index can be searched. Worth
stating rather than leaving for someone to relitigate, because the two look alike
from a dependency list.

**`library_vectors` and `signal_vectors` live in their own database file**
(memory-retention epic clause 7, issue_01M35R5GA1SKRG2CMQRF189NDE), outside
the hot write path `store.py` guards. `LIBRARY_VECTORS_DB` names it, with no
default — see `resolve_vectors_db_path` — and every connection this module
opens ATTACHes it (`_attach_vectors_db`). No query anywhere below changed:
neither vec0 table exists in `main` any more, so the unqualified names they
were always written with resolve straight through to the attached schema.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import struct
import sys
import threading
from pathlib import Path

import store

# vec0 stores float32; the index is BAAI/bge-small-en-v1.5 at 384 dimensions.
# A mismatch here does not error, it silently returns nonsense neighbours, so the
# dimension is asserted on every embed rather than trusted.
EMBED_DIM = 384
VEC_TABLE = "library_vectors"
# The Archives' own index, over signals rather than work items. Named beside
# `VEC_TABLE` because until `init_vectors.py` (issue_01M3JMHPN98Z3WD8GSSC841VM7)
# nothing else in this module named it — `migrate.py` carried the string
# literally, copied from the source database's own `sqlite_master`.
SIGNAL_VEC_TABLE = "signal_vectors"
VAULT_ARCHIVES = "archives"
VAULT_CHART_HOUSE = "chart_house"

# **THE CANONICAL DDL, and the only place either statement is written.**
# `migrate.py`'s cutover move never runs these — it copies the source
# database's own `sqlite_master.sql` verbatim — so this is where a *fresh*
# database gets the tables from. `init_vectors.py` runs both, and
# `tests/test_init_vectors.py` compares what they produce, byte for byte,
# against what a migrated database's `sqlite_master` holds.
#
# **`IF NOT EXISTS` is baked into the constant rather than assembled by the
# caller.** SQLite stores a virtual table's original CREATE statement in
# `sqlite_master.sql` with `IF NOT EXISTS` stripped out, so the constant runs
# idempotently AND still equals the plain DDL once it lands in the schema —
# there is no second, IF-NOT-EXISTS-free form to keep in sync with this one.
VEC_TABLE_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING "
    f"vec0(embedding float[{EMBED_DIM}], vault text, +item_id text)")
SIGNAL_VEC_TABLE_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {SIGNAL_VEC_TABLE} USING "
    f"vec0(embedding float[{EMBED_DIM}], +signal_id text)")

# Chunking geometry, carried across from Orient unchanged. bge-small attends to
# ~512 tokens, so ONE vector for a 16k-character transcript represents only its
# opening — Orient's first real transcript ingest proved content-specific queries
# miss. ~1,500 characters is 300–400 tokens, safely inside the window; the overlap
# keeps a sentence straddling a boundary findable from either side.
#
# **THESE TWO NUMBERS ARE PART OF THE INDEX'S GEOMETRY, not a tuning knob.** The
# adjacency threshold was calibrated against vectors chunked this way, so changing
# either silently changes what that threshold means for every item embedded
# afterwards while leaving the older rows alone.
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200

# **HOW MUCH OF A SWEEP MAY SIT UNDER ONE WRITE LOCK**, and this number is
# measured rather than picked. `embed_missing` used to commit its whole batch in a
# single transaction, which is fine while the sweep is unwired and becomes a
# convoy the moment it is scheduled: `connect_rw` sets `busy_timeout` to 5,000 ms,
# so every concurrent ingest and revision waits out the hold and then 500s.
#
# The worst case the batch can present is 500 rows of `MAX_ITEM_TEXT`, which
# chunks to 78 vectors each — 39,000 inserts, plus a SELECT and an UPDATE per row.
# Measured on this laptop against a 20,500-row table, that holds the lock for
# **2,324 ms**: inside the timeout, but at less than half of it, and the margin is
# gone on a slower disk or a larger batch. The failure this guards was reported as
# a multiple of the timeout, which the measurement does not support; the hold is
# real and the magnitude was not.
#
# 4,000 chunks is ~240 ms of hold at the rate above, twenty times inside the
# timeout, and a row is never split across transactions — a single row can exceed
# this on its own and still commits whole, because `write_if_current`'s
# check-and-write has to be atomic per row.
MAX_CHUNKS_PER_TXN = 4000

# **The KNN is OVERSAMPLED, and this factor is why a heavily-filtered query still
# fills a page.** Scope, product and the customer wall are applied to the JOINED
# rows, so a page's worth of vectors can reduce to nothing after filtering. Orient
# takes `limit * 5` into the search and the KNN itself over-fetches ×4 on top, to
# collapse an item's several chunk rows to its best one.
OVERSAMPLE = 5
CHUNK_OVERSAMPLE = 4

_model = None
_model_lock = threading.Lock()
_embedder = None


def set_embedder(fn) -> None:
    """Inject an embedder: fn(list[str]) -> iterable of EMBED_DIM-length float
    sequences. Pass None to restore the real path.

    A seam for tests, and the reason the suite does not download an ONNX model to
    assert that a scope filter works."""
    global _embedder
    _embedder = fn


def _fastembed(texts):
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                # LAZY, and double-checked under the lock. The model costs seconds
                # to load and is loaded once per process; importing it at module
                # scope would make every route in this service — including the
                # deterministic ones — wait for an ONNX runtime nothing else needs.
                from fastembed import TextEmbedding
                _model = TextEmbedding()   # BAAI/bge-small-en-v1.5, 384-dim, CPU
    return _model.embed(list(texts))


def embed_query(text: str) -> bytes:
    """One query as a packed float32 blob, dimension-checked.

    CPU-bound: a caller in an async context runs this in a threadpool, the same as
    ingest does, or it blocks the event loop for every other request."""
    fn = _embedder or _fastembed
    values = list(next(iter(fn([text]))))
    if len(values) != EMBED_DIM:
        raise ValueError(
            f"embedder returned {len(values)} dimensions, index holds {EMBED_DIM}; "
            f"a mismatch returns nonsense neighbours rather than an error")
    return struct.pack(f"{EMBED_DIM}f", *values)


def embed_texts(texts) -> list[bytes]:
    """Several texts as packed float32 blobs, each dimension-checked.

    ONE model call for the whole list, which is the reason a batch function exists
    beside `embed_query`: a chunked item and a sweep's whole page each want their
    text embedded together, and calling the model per chunk pays its per-call
    overhead once per chunk instead of once per batch.

    CPU-bound, and the caller is responsible for holding no SQLite write lock
    while it runs. That is not a style note — Orient measured eight concurrent
    posts returning in multiples of `busy_timeout`, six of them 500s, when the
    model call sat inside an open write transaction.
    """
    fn = _embedder or _fastembed
    out = []
    for vector in fn(list(texts)):
        values = list(vector)
        if len(values) != EMBED_DIM:
            raise ValueError(
                f"embedder returned {len(values)} dimensions, index holds "
                f"{EMBED_DIM}; a mismatch returns nonsense neighbours rather "
                f"than an error")
        out.append(struct.pack(f"{EMBED_DIM}f", *[float(x) for x in values]))
    return out


def chunks(text: str, size: int = CHUNK_CHARS,
           overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Text as overlapping windows, one vector each.

    Each window's end snaps back to the last whitespace inside it, so no chunk
    splits a word; the next window starts `overlap` characters before the previous
    end. Short text is one chunk, unchanged. A pathological run with no spaces
    keeps the hard cut rather than growing past the model's window.
    """
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + size // 2, end)
            if cut > start:
                end = cut
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in out if c]


def text_for_signal(row) -> str:
    """An Archives record's embedding text: its body, or a thin kind/stream
    string when the body is empty, because an empty target indexes noise."""
    body = (row["body"] or "").strip()
    return body or f"{row['kind']} {row['stream'] or ''}".strip()


def text_for_item(row) -> str:
    """A work item's embedding text: title AND body, title first.

    A signal is all body, but an item's title is the part a person searches for
    and is often the only populated field — an item filed with a precise title and
    an empty body is normal, and embedding the body alone would index nothing at
    all for exactly the items adjacency most needs to find. The title leads
    because bge-small weights the opening of a chunk most heavily, and for a short
    item that opening is the whole signal.
    """
    title = (row["title"] or "").strip()
    body = (row["body"] or "").strip()
    return f"{title}\n\n{body}".strip() if body else title


# **WHICH ARCHIVES SIGNAL KINDS ARE EMBEDDED, kind by kind** —
# library/decisions/0001-what-the-archives-keep-embed-and-move-cold, as amended
# by convivy/knowledge#787. A kind not listed here defaults to not embedded,
# per that decision's own Defaults section, so this is an allow-list rather
# than a deny-list. Plain data, so the discard sweep, the cold-tier mover and
# the one-time index reduction can import it rather than re-deriving it from
# the decision's prose.
#
# **`transcript-turn` is embedded because the operator ruled so on 2026-10-05.** A
# retrieval measurement showed that removing turns from this set lost three of
# thirty fixed questions their right answer, all three asking what the operator said in
# a session. Turns stay hot for 30 days (`HOT_WINDOW_DAYS`), not the 180 the
# other embedded kinds get.
#
# **`review_record` is embedded because the Reviews search vault has no
# vector index of its own** — it rides the Archives KNN through its linked
# signal (`app._search_review_page`), so a `review_record` signal left out
# of this set is unfindable through `GET /search`'s `vault=reviews` no
# matter what `review_record_backfill.py` linked (convivy/library#177,
# knowledge#692). `review_verdict`, `review_comment` and `review-completed`
# stay not embedded.
EMBEDDED_SIGNAL_KINDS = frozenset({
    "pr_comment", "issue_comment",
    "finding", "observation", "correction", "incident", "defect",
    "infrastructure-gap", "review-finding-refuted",
    "decision", "needs-decision", "retired-decision", "proposal",
    "proposal_decision",
    "document", "transcript", "transcript-turn",
    "note", "reference", "briefing", "brainstorm",
    "memory",
    "digest", "overnight-digest", "release_note",
    "review_record",
})

# **THE KINDS THE RETENTION DECISION KEEPS BUT DOES NOT EMBED** — library/
# decisions/0001-what-the-archives-keep-embed-and-move-cold's "Kept, not
# embedded" table, as amended by convivy/knowledge#787. Each is looked up by
# filter (kind, product, actor, refs, time), never by what it says, so none
# belongs in `EMBEDDED_SIGNAL_KINDS`. Named here, beside that set and
# `DISCARDED_SIGNAL_KINDS` below, so `discard_sweep.py`, the cold tier and the
# storage report share one copy of the decision's kind lists rather than each
# deriving its own.
KEPT_NOT_EMBEDDED_SIGNAL_KINDS = frozenset({
    "pr_state", "model_resolution", "changelog",
    "alert", "ops-alert", "deploy_failure", "capability-restored",
    "session-follow-leak-recovered", "test-result",
    "review_verdict", "review_comment", "review-completed",
    "notification", "deletion-record",
})

# **THE KINDS THE RETENTION DECISION DISCARDS** — that document's own
# Discarded section, which names none today: "No kind is discarded today.
# Each kind is either still read or holds some text that exists nowhere
# else." `discard_sweep.py` imports this set rather than keeping its own
# copy, so on today's data it removes nothing and a later revision of the
# decision needs no change here.
DISCARDED_SIGNAL_KINDS = frozenset()

# **HOW LONG EACH KIND STAYS HOT, IN DAYS, BEFORE `cold_tier.py` MOVES IT** —
# library/decisions/0001-what-the-archives-keep-embed-and-move-cold's own
# values, copied here rather than re-derived, beside the kind sets above so
# `cold_tier.py`, the discard sweep and the storage report share one copy.
# `None` means a kind never moves. A kind outside this dict gets
# `DEFAULT_HOT_WINDOW_DAYS` instead (the decision's Defaults section: an
# unnamed kind is "cold", i.e. has no hot window of its own).
HOT_WINDOW_DAYS: dict[str, int | None] = {
    **{kind: 180 for kind in EMBEDDED_SIGNAL_KINDS},
    "review_verdict": 180,
    "review_comment": 180,
    "review-completed": 180,
    "changelog": 90,
    "alert": 90,
    "ops-alert": 90,
    "deploy_failure": 90,
    "capability-restored": 90,
    "session-follow-leak-recovered": 90,
    "test-result": 90,
    # Must stay after the 180 spread above, which now covers this kind too:
    # the later key is the one that wins. Pinned by test_cold_tier.py.
    "transcript-turn": 30,
    "pr_state": 30,
    "model_resolution": 30,
    "notification": 0,
    "deletion-record": None,
}

# **THE WINDOW AN UNRULED KIND GETS** — the decision's own Defaults section
# says an unnamed kind is "cold" outright, i.e. has no hot window at all, so
# it moves on its very next sweep. Named rather than inlined as `0` so a
# future revision that wants new kinds to sit hot for a grace period while
# they wait for a rule changes one constant (plan's "Where the item is
# wrong" §6).
DEFAULT_HOT_WINDOW_DAYS = 0


def _archives_signal_embeddable(row) -> bool:
    return row["kind"] in EMBEDDED_SIGNAL_KINDS


def _always_embeddable(row) -> bool:
    return True


# What a vault contributes to the shared pipeline: which table holds its rows,
# what text stands for one, which column means "oldest first" to a sweep, and
# which rows are embedded at all. Everything else — chunking, the dimension
# check, the three-step lock discipline, the best-effort contract — is common.
#
# **The Reference Room is deliberately absent.** The vault is real, but the KB
# lives in a separate FTS5 store and moving it into this index is later-phase
# work. Registering an adapter pointing at nothing would make the index look wider
# than it is.
_VAULT_ADAPTERS = {
    VAULT_ARCHIVES: ("signals", text_for_signal, "ingested_at",
                      _archives_signal_embeddable),
    VAULT_CHART_HOUSE: ("issue_items", text_for_item, "created_at",
                         _always_embeddable),
}

# The vaults this index embeds, sorted so a sweep's order is deterministic.
EMBEDDED_VAULTS = tuple(sorted(_VAULT_ADAPTERS))


def _adapter(vault: str):
    try:
        return _VAULT_ADAPTERS[vault]
    except KeyError:
        known = ", ".join(EMBEDDED_VAULTS)
        raise ValueError(f"unknown vault {vault!r} (embedded: {known})") from None


class MisconfiguredVectorsDB(RuntimeError):
    """`LIBRARY_VECTORS_DB` is unset, so this process cannot say where the
    vector index lives.

    **Mirrors `store.MisconfiguredDatabase` and `blobs.MisconfiguredBlobStore`**
    (memory-retention epic clause 7, issue_01M35R5GA1SKRG2CMQRF189NDE): the
    vector index's location is configuration, not a constant, raised at import
    so a misconfigured deploy refuses to start rather than reporting healthy
    while semantic search silently has nothing to attach.
    """


def resolve_vectors_db_path(env=None) -> Path:
    """The file `library_vectors` and `signal_vectors` live in.

    **No default**, for the reason `store.resolve_db` and
    `blobs.resolve_blob_dir` have none: a fallback path is indistinguishable
    from the real file once the two happen to share a name.

    **Existence is NOT checked here, unlike `blobs.resolve_blob_dir`'s
    directory.** `init_vectors.py` is the one caller allowed to create this
    file, and on a fresh install nothing has written it yet — requiring it to
    exist at every `import vectors` would make that tool unable to import the
    module it exists to prepare a file for. `connect` and `connect_rw` check
    existence at open time instead, the same split `store.resolve_db` /
    `store.connect` already use for `LIBRARY_DB`.
    """
    env = os.environ if env is None else env
    raw = env.get("LIBRARY_VECTORS_DB")
    if not raw:
        raise MisconfiguredVectorsDB(
            "LIBRARY_VECTORS_DB is not set. The vector index's location is "
            "configuration, not a constant, and there is no default this "
            "can fall back to. Set LIBRARY_VECTORS_DB to the vector index's "
            "own database file — init_vectors.py creates it on a fresh "
            "install.")
    return Path(raw).expanduser()


VECTORS_DB_PATH = resolve_vectors_db_path()

# `store.ORIENT_FINGERPRINT_SQL` cannot run here unqualified: SQLite resolves
# a bare `sqlite_master` against `main` only, and the vec0 tables live in
# `vecdb` once attached (see `_attach_vectors_db`). Same table list
# (`store.ORIENT_ONLY_TABLES`), same shape of query, qualified to the schema
# this module actually attaches — kept beside that function rather than
# folded into `store.ORIENT_FINGERPRINT_SQL` itself, which several other
# callers (`init_vectors.py`, `alembic/env.py`, `store._refuse_orients_handle`)
# run unqualified against `main` and would have to be re-plumbed for a schema
# argument they don't need.
_ORIENT_FINGERPRINT_SQL_VECDB = (
    "SELECT name FROM vecdb.sqlite_master WHERE type='table' AND name IN "
    f"({','.join('?' * len(store.ORIENT_ONLY_TABLES))}) LIMIT 1")


def _attach_vectors_db(conn: sqlite3.Connection, *, readonly: bool) -> None:
    """ATTACH the vector index's own file onto `conn`, under the name every
    unqualified `{VEC_TABLE}` / `{SIGNAL_VEC_TABLE}` reference in this module
    already uses.

    **No query in this file needed a schema-qualified name change.** SQLite
    resolves an unqualified table name against `main` first, then each
    attached database in ATTACH order; neither vec0 table is created in
    `main` any more, so the lookup falls straight through to this attached
    schema.

    **Raises `FileNotFoundError` rather than attaching an absent file SQLite
    would otherwise create on the spot** — the same refusal `store.connect` /
    `connect_rw` give for a missing `LIBRARY_DB`, and the reason clause 7
    asks for: a missing file must not come up looking like an empty, working
    index.

    **Refused when `LIBRARY_VECTORS_DB` names Orient's control database, or a
    copy of it, through the same two checks `store.resolve_db` chains for
    `LIBRARY_DB` (`store._refuse_orients_database`) — but run as two separate
    steps here rather than through that one call, because the conclusive half
    has to ask the connection this function is about to hand back, not a
    second one.** `store._refuse_orients_path` runs first, before the
    `ATTACH`: it is a path/inode comparison with no query and nothing to
    race, the same cheap pre-check `store.resolve_db` runs at import.
    Immediately after the `ATTACH` and before any write to the attached
    schema, `_ORIENT_FINGERPRINT_SQL_VECDB` runs on `conn` itself — the
    schema fingerprint, asked through the handle this function is about to
    return rather than a throwaway probe connection to `VECTORS_DB_PATH`
    opened and closed first. A probe-then-attach ordering validates a file
    that a symlink swap could replace in the gap between the probe's close
    and the `ATTACH`; asking the connection that already holds the attach has
    no such gap (reviewer + security-reviewer, round 4) — the same reasoning
    `store.connect_rw`'s docstring gives for asking `_refuse_orients_handle`
    of the open handle rather than re-running `_refuse_orients_path` a second
    time before the open. Orient's control database still holds vec0 tables
    from before the cutover, so a `LIBRARY_VECTORS_DB` naming it would
    otherwise attach silently and take every write this module makes, the
    second-writer failure ADR-0020 exists to prevent. Both checks run on
    every attach — every `connect()` and `connect_rw()` — rather than once at
    import, because `resolve_vectors_db_path` resolves before the file has to
    exist and cannot fingerprint a schema that is not there yet.

    **A busy attached schema is contention, not a misconfigured database**,
    the same distinction `store._refuse_orients_handle` draws for `LIBRARY_DB`
    (reviewer, convivy/library#112): `SQLITE_BUSY` reading the fingerprint
    passes through uncaught rather than being reported as an unidentifiable
    file, since it says nothing about which file this is.

    **`store._refuse_orients_handle` itself is not called.** It queries
    `sqlite_master` unqualified, which SQLite resolves against `main` only —
    an ATTACHed schema needs `<name>.sqlite_master`, which that function has
    no schema argument to ask for. `_ORIENT_FINGERPRINT_SQL_VECDB` above is
    the qualified equivalent, run the same way: through the handle, not a
    separate connection.

    `readonly` picks the attach form: a URI with `mode=ro` for a connection
    already opened with SQLite's URI flag (`connect`, below — ATTACH parses
    its own argument as a URI only when the connection that runs it was
    itself opened that way), or a plain path for `connect_rw`'s handle, which
    is not URI-opened and has no `mode=ro` to ask for regardless.
    """
    if not VECTORS_DB_PATH.is_file():
        conn.close()
        raise FileNotFoundError(f"no vectors database at {VECTORS_DB_PATH}")
    try:
        store._refuse_orients_path(VECTORS_DB_PATH, os.environ)
    except BaseException:
        conn.close()
        raise
    if readonly:
        conn.execute("ATTACH DATABASE ? AS vecdb",
                     (store.db_uri(VECTORS_DB_PATH, readonly=True),))
    else:
        conn.execute("ATTACH DATABASE ? AS vecdb", (str(VECTORS_DB_PATH),))
    try:
        found = conn.execute(_ORIENT_FINGERPRINT_SQL_VECDB,
                              store.ORIENT_ONLY_TABLES).fetchone()
    except sqlite3.Error as exc:
        conn.close()
        if (isinstance(exc, sqlite3.OperationalError)
                and exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY):
            raise
        raise store.MisconfiguredDatabase(
            f"the vectors database at {VECTORS_DB_PATH} could not be "
            f"identified after attaching it ({exc}). Refusing the "
            f"connection rather than using a file this cannot vouch "
            f"for.") from exc
    if found is not None:
        conn.execute("DETACH DATABASE vecdb")
        conn.close()
        raise store.MisconfiguredDatabase(
            f"{VECTORS_DB_PATH} holds {found[0]!r} — a table that stays "
            f"with Orient, so this file is Orient's control database or a "
            f"copy of it. The vector index owns its own file; point "
            f"LIBRARY_VECTORS_DB at it.")
    if not readonly:
        # `PRAGMA journal_mode` with no schema name touches `main` only, so
        # the attached file needs this call too or it never leaves whatever
        # mode it was created in — unlike `main`, which `store.connect_rw`
        # already switched before this function ran. Run only after the
        # fingerprint check clears, so a hit on Orient's file is refused
        # before this ever writes to it.
        store.switch_to_wal(conn, schema="vecdb")


def connect_rw() -> sqlite3.Connection:
    """A WRITABLE connection with `vec0` loaded and the vectors database
    ATTACHed.

    **The shadow's refusal is still the only door**, because this opens its handle
    through `store.connect_rw` rather than beside it: a caller reaching the vector
    index during the read-only shadow gets `WritesDisabled` from the same check
    every other write path hits. Adding a second `sqlite3.connect` here would have
    quietly given the vector surface a way around the control the whole shadow
    rests on.

    **The extension load is the failure this function exists to make loud.** A
    writable handle without `sqlite-vec` opens fine and fails at the vector write —
    `no such module: vec0`, mid-transaction, on the write path rather than at open.
    That is the seam `platform/reference/keel-goal-baselines` records and ADR-0034
    carries the write side of. Loading here means a build without the extension
    refuses at open, where the caller can still do something about it.

    Extension loading is switched off again immediately, so nothing later in the
    process can pull in an arbitrary extension through this handle.

    **`insert_vectors` writes into this attached database and stamps
    `embedded_at` on `main` in the SAME transaction, and a commit spanning an
    attached database is not guaranteed atomic across a host crash once
    either side is in WAL mode** — SQLite documents that a multi-database
    transaction commits each file independently when WAL is involved, unlike
    the single-file, single-fsync guarantee this had before the split. A
    crash in that narrow window can leave vectors written with no stamp, or a
    stamp with no vectors. Both are the same shape of gap `insert_vectors`
    already tolerates for other reasons — an un-embedded row the sweep
    revisits, or a stamped row `available: false` reads as having nothing to
    compare — rather than a new class of corruption, and no test in this
    suite can force the crash to assert it directly.
    """
    import sqlite_vec
    conn = store.connect_rw()
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    try:
        _attach_vectors_db(conn, readonly=False)
    except BaseException:
        conn.close()
        raise
    return conn


def insert_vectors(conn, vault: str, item_id: str, blobs) -> str:
    """Write one item's chunk vectors and stamp `embedded_at`, INSIDE the caller's
    transaction. Returns the stamp it wrote.

    The single write path into the index, which is what keeps "a row's vectors and
    its stamp agree" a property of one function rather than of every call site.

    **A row with no embeddable text is stamped anyway, holding zero vectors.** The
    alternative is a sweep that re-reads the same empty row on every pass forever.
    That row then reads as `available: false` to adjacency — "this item has no
    embedding, so nothing was compared" — which is the true answer rather than a
    convenient one. Unreachable through the write routes on an EMPTY-TEXT ground,
    since every one of them requires a non-empty title or kind — reachable there
    only for a legacy row. A signal whose KIND the retention decision marks not
    embedded reaches this the same way, through the ordinary write routes,
    deliberately: `embed_item_by_id` and `embed_missing` pass no blobs for one
    rather than running it through the model at all.
    """
    name, _, _, _ = _adapter(vault)
    for blob in blobs:
        conn.execute(
            f"INSERT INTO {VEC_TABLE}(embedding, vault, item_id) VALUES (?,?,?)",
            (blob, vault, item_id))
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
    conn.execute(f"UPDATE {name} SET embedded_at = ? WHERE id = ?",
                 (now, item_id))
    return now


def invalidate_item(conn, vault: str, item_id: str) -> None:
    """Drop one item's vectors and clear its stamp, so the next embed re-runs.

    **THE TEXT AND ITS VECTORS MUST NOT DISAGREE.** Embedding is idempotent on
    `embedded_at`, which is right for a row whose text never changes and wrong the
    moment one does: a revised item keeps the vectors of the text it no longer
    holds, and every later read — adjacency, semantic search, the briefing itself —
    scores the old words. Nothing announces that, which is what makes it worth a
    named function rather than two lines at a call site.

    **Call it INSIDE the transaction that writes the new text.** The delete and the
    revision then land together, so there is no window in which the row's stamp
    claims vectors describing something else. Re-embedding belongs AFTER the
    commit, for the reason ingest embeds after its own: the model is CPU-bound and
    would hold SQLite's single write lock for its whole run.
    """
    name, _, _, _ = _adapter(vault)
    conn.execute(f"DELETE FROM {VEC_TABLE} WHERE vault = ? AND item_id = ?",
                 (vault, item_id))
    conn.execute(f"UPDATE {name} SET embedded_at = NULL WHERE id = ?", (item_id,))


def write_if_current(conn, vault: str, item_id: str, text: str, blobs):
    """Write vectors ONLY if they still describe the row's current text and
    nothing else has stamped it.

    Returns `(wrote, stamp)`. `wrote` says whether THIS call laid down vectors;
    `stamp` is the row's `embedded_at` as it now stands, which is another writer's
    when this call declined because the row was already embedded, and None when
    the row is gone or its text moved. Two values rather than one because the
    callers want different halves — a sweep counts what it wrote, an ingest embed
    reports the row's stamp whoever set it — and collapsing them is how "already
    embedded" comes to be counted as work done.

    **THIS IS THE WHOLE CONCURRENCY CONTROL, and both halves of the check matter
    for a different failure.**

    `embedded_at IS NULL` is the duplicate guard: without it two embeds of one row
    each lay down a full set of vectors, and the item then holds two.

    The TEXT comparison is the staleness guard, and it is the one a stamp check
    alone cannot give. Two revisions of the same item overlap like this: A commits
    text X1 and starts its model call; B commits X2 while that call is in flight
    and starts its own. Both cleared `embedded_at`, so both pass a stamp check.
    Whichever reaches the write first wins — and if that is A, the row is stamped
    holding vectors for X1 while its text says X2, permanently, with every later
    search and briefing scoring words the item no longer contains and nothing
    announcing it. Comparing the text this call embedded against the row's current
    text is what makes the loser stand down instead.

    **DECLINING LEAVES `embedded_at` NULL, which is the right outcome rather than
    a failure.** The revision that superseded this one has its own embed in flight,
    and `embed_missing` is behind that. An un-embedded row is visible and gets
    fixed; a wrongly-stamped one is invisible forever.

    **The caller must already hold an IMMEDIATE transaction.** pysqlite opens no
    transaction for a bare SELECT, so a read followed by a write is two separate
    statements with a real gap between them — checking inside a deferred
    transaction would leave exactly the window this exists to close.
    """
    name, text_of, _, _ = _adapter(vault)
    row = conn.execute(f"SELECT * FROM {name} WHERE id = ?",
                       (item_id,)).fetchone()
    if row is None:
        return False, None
    if row["embedded_at"] is not None:
        return False, row["embedded_at"]
    if text_of(row) != text:
        return False, None
    return True, insert_vectors(conn, vault, item_id, blobs)


def embed_item_by_id(vault: str, item_id: str) -> str | None:
    """Embed one ALREADY-COMMITTED row, holding no write lock while the model runs.

    Returns the row's `embedded_at` stamp when there is one to report — this
    call's, or a concurrent sweep's or revision's. **None means the row has no
    stamp**, which happens two ways: the row is gone, or another writer revised its
    text while this call's model was running, so what this call embedded is no
    longer what the row says and `write_if_current` stood down. Neither is an error
    and neither leaves a wrong answer in the index — the revision that superseded
    this one has its own embed in flight, with `embed_missing` behind it.

    (Orient's version returned None for "gone" and for "already embedded" alike,
    and its ingest caller could not tell them apart: a sweep embedding the row
    between the two reads left the 201 response saying `embedded_at: null` while
    the stored row had a stamp. Reporting the real stamp is what stops a response
    disagreeing with the row, and that half is kept.)

    **THE THREE STEPS ARE THE WHOLE POINT.** Read what the model needs and close
    the transaction; run the model holding nothing; open a second, short
    transaction for the vectors. Both transactions are millisecond-scale however
    slow the model is. Embedding inside the caller's own open write transaction —
    the item's INSERT — is what produced Orient's convoy: eight concurrent posts
    returning in multiples of `busy_timeout`, six of them 500s.

    **The write step takes the lock BEFORE it reads.** `BEGIN IMMEDIATE` is what
    makes `write_if_current`'s check-then-write one atomic step; a deferred
    transaction would leave the read outside it, which is the gap that lets two
    callers both pass the check. This route has more than one caller per vault —
    ingest, the sweep, and every revision — so that gap is reachable rather than
    theoretical.

    **A row whose kind the retention decision marks not embedded never reaches
    the model.** `embeddable(row)` false stamps the row holding zero vectors,
    the same outcome `insert_vectors` already gives a row with no text —
    without it, `embed_missing` would reselect the same never-embedded row on
    every sweep forever, since nothing would ever set its stamp.
    """
    name, text_of, _, embeddable = _adapter(vault)
    conn = connect_rw()
    try:
        row = conn.execute(f"SELECT * FROM {name} WHERE id = ?",
                           (item_id,)).fetchone()
        if row is None:
            return None
        if row["embedded_at"] is not None:
            return row["embedded_at"]       # already embedded — nothing to do
        text = text_of(row)
        skip = not embeddable(row)
    finally:
        conn.close()
    blobs = [] if skip else embed_texts(chunks(text))  # no connection, no lock
    conn = connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _, stamp = write_if_current(conn, vault, item_id, text, blobs)
        conn.commit()
        return stamp
    finally:
        conn.close()


def embed_missing(limit: int = 500, vault: str | None = None) -> int:
    """Backfill one batch of un-embedded rows, oldest first, batching every chunk
    into one model call. Returns how many rows THIS call embedded — loop until 0
    to drain.

    **The best-effort embed is only safe because this exists.** A failed embed
    leaves a durable row with `embedded_at` NULL, which is the right trade when a
    capture is usually the only copy — but only if something later notices what it
    left behind. Without a sweep, "best effort" means "silently dropped".

    `vault=None` sweeps every embedded vault, one batch each, and returns the
    total, so adding a vault does not mean remembering to add a sweeper.

    Three steps again, but NOT for `embed_item_by_id`'s reason — a SELECT opens no
    write transaction. The reason here is `write_if_current`: this batch's model
    call is the longest window in the system, and a row can be embedded OR revised
    during it. Writing the batch blind would lay a second set of vectors over a row
    that already has them, or stamp a row with vectors of the text it held before
    the revision. Each row is checked against what this call actually embedded, so
    a row that moved underneath the batch is skipped and swept next time.

    **THE WRITE PHASE COMMITS IN SEVERAL TRANSACTIONS, bounded by
    `MAX_CHUNKS_PER_TXN`.** One transaction for the whole batch would hold the
    single write lock for as long as the batch takes — 2,324 ms measured at the
    worst case, against a 5,000 ms `busy_timeout` — and every concurrent ingest
    and revision waits that out. Committing in bounded groups is what keeps a
    scheduled sweep from convoying the write path.

    A row is never split across two transactions, so `write_if_current`'s
    check-and-write stays atomic per row and every guarantee above is unchanged.
    What DOES change is the failure mode: a batch that dies partway now leaves the
    groups it already committed, where the single transaction rolled all of them
    back. Both are correct, because the sweep is resumable by construction — an
    un-embedded row is simply still un-embedded, and the next pass finds it. The
    partial version does strictly less work twice.
    """
    if vault is None:
        return sum(embed_missing(limit, v) for v in EMBEDDED_VAULTS)
    name, text_of, order_col, embeddable = _adapter(vault)
    conn = connect_rw()
    try:
        rows = conn.execute(
            f"SELECT * FROM {name} WHERE embedded_at IS NULL "
            f"ORDER BY {order_col} ASC LIMIT ?", (int(limit),)).fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        texts = [text_of(r) for r in rows]
        # A row whose kind the retention decision marks not embedded gets zero
        # chunks, the same "nothing to send the model" shape a row with no text
        # already takes — it is stamped holding no vectors, below, rather than
        # reselected by every sweep forever.
        per_item = [chunks(t) if embeddable(r) else []
                    for r, t in zip(rows, texts)]
    finally:
        conn.close()
    flat = embed_texts([c for item in per_item for c in item])  # one call, no txn
    conn = connect_rw()
    try:
        embedded = 0
        cursor = 0
        pending = 0
        in_txn = False
        for item_id, text, item_chunks in zip(ids, texts, per_item):
            blobs = flat[cursor:cursor + len(item_chunks)]
            cursor += len(item_chunks)
            # Commit BEFORE this row rather than after, so the row that would
            # cross the bound starts the next transaction whole. A row larger
            # than the bound on its own still commits whole, because `in_txn` is
            # false on the first row and the check cannot split a row anyway.
            #
            # **THE CHECK IS UNCONDITIONAL ON `pending`, and an earlier version
            # was not** (reviewer, PR #21). It read `in_txn and pending and ...`,
            # meaning a row that followed a ZERO-CHUNK row — an item with no
            # embeddable text, which `insert_vectors` documents as reachable for a
            # legacy row — left `pending` at 0 and escaped the bound entirely,
            # however large it was. Committing a transaction that holds nothing
            # but a zero-vector stamp is harmless, so the guard bought nothing and
            # cost the property the constant exists to state.
            if in_txn and pending + len(item_chunks) > MAX_CHUNKS_PER_TXN:
                conn.commit()
                in_txn, pending = False, 0
            if not in_txn:
                # The lock before the reads, for `write_if_current`'s reason:
                # pysqlite opens no transaction for a SELECT, so a deferred one
                # would leave every check in this loop outside the write it
                # guards.
                conn.execute("BEGIN IMMEDIATE")
                in_txn = True
            wrote, _ = write_if_current(conn, vault, item_id, text, blobs)
            embedded += 1 if wrote else 0
            pending += len(item_chunks)
        if in_txn:
            conn.commit()
        return embedded
    finally:
        conn.close()


def connect(db_path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """A read-only connection with `vec0` loaded and the vectors database
    ATTACHed, read-only, alongside it.

    `db_path` is the MAIN vault database — `signals`, `issue_items` and the
    rest — not the vector index; `GET /signals/search` runs its whole join on
    the one connection this returns, `nearest()`'s KNN and the follow-up
    `SELECT * FROM signals` together, which is why both files have to be
    reachable from a single handle rather than two.

    Extension loading is enabled only for the load itself and switched off again,
    so nothing later in the process can pull in an arbitrary extension through
    this handle.

    **The URI comes from `store.db_uri` because interpolating the path raw
    defeats `mode=ro`** — SQLite splits the URI on its first `?`, so a path
    containing one opens a different file, read-write. Found in `migrate.py` by a
    security reviewer (convivy/library#22) and fixed there; this was the second
    site and `store.connect` was the third.

    **`timeout` bounds how long `sqlite3.connect` waits behind a writer's lock,
    and 5.0 is Python's own default** — passing no argument reproduces today's
    behaviour exactly. `store.connect_rw` sets `PRAGMA journal_mode=WAL` on every
    database it writes to (issue_01M2KW35JX19GVG9Z13R10HKNZ), and journal mode is
    a property of the file rather than the connection, so once that switch has
    been applied a reader here is never forced to wait behind an ordinary
    writer's commit — only behind a writer's own EXCLUSIVE lock (a checkpoint, a
    `VACUUM`), which this timeout still bounds. A caller on a short budget —
    `/health`'s readiness check, in particular — should still pass a smaller
    one; `GET /signals/search` leaves it at the default on purpose, because a
    shorter timeout there would trade correctness (a real, if slow, answer) for
    speed on a route this change is not meant to touch.

    **`mode=ro` reads a WAL database correctly in every state, confirmed
    empirically** (`tests/test_wal_journal_mode.py`): with a writer connected,
    with no writer connected and `-wal`/`-shm` present, and with no writer
    connected and those files absent — the same three states `store.connect`
    was checked against, and no code change was needed here either."""
    import sqlite_vec
    conn = sqlite3.connect(store.db_uri(db_path, readonly=True), uri=True,
                            timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    try:
        _attach_vectors_db(conn, readonly=True)
    except BaseException:
        conn.close()
        raise
    # Opened by hand rather than through `store.connect()`, because this also
    # loads `vec0` and attaches the vectors database — so it needs the same
    # registered SQL functions `store.connect()`/`connect_rw()` apply, or
    # `GET /signals/search` and the Archives half of `GET /search` (the two
    # routes that read `signals` through this connection) would run the
    # handoff wall against an unregistered function (library#185 round 3).
    store._apply_connection_functions(conn)
    return conn


def nearest(conn, query_vector: bytes, k: int, vault: str) -> list[tuple[str, float]]:
    """`[(item_id, distance)]`, nearest first, one entry per item.

    **TAKES AN ALREADY-EMBEDDED VECTOR, and the split is not stylistic.** The
    expensive half is `embed_query`, which a caller runs in a threadpool; the KNN
    itself is a fast indexed lookup. Passing the query text here instead would
    force the whole thing into the threadpool — and a SQLite connection cannot
    cross threads, so the connection would have to be opened inside it too. That
    is the bug this signature exists to make unrepresentable.

    **Pure retrieval. Scope, product and privacy filtering are the caller's job on
    the joined rows** — this returns ids and distances and knows nothing about who
    is asking. Keeping the split here is what stops the filter being forgotten in
    one of two places.

    **The vault narrows the KNN ITSELF rather than filtering its results**, which
    is why `vault` is a vec0 metadata column. Filtering afterwards would pay for
    the Archives' 44,000 vectors on every Chart House query.

    A chunked item holds several vectors, so the raw KNN is over-fetched and then
    collapsed to each item's best-matching chunk."""
    # THE TIEBREAKER HAS TO GO IN THE COLLAPSE BELOW, BECAUSE THE SQL REFUSES ONE.
    # Every other ordered query in this service ends its ORDER BY in the primary
    # key, so a tie is broken by the query rather than by whatever SQLite happens
    # to do. This one cannot join them:
    #
    #     sqlite3.OperationalError: Only a single 'ORDER BY distance' clause is
    #     allowed on vec0 KNN queries
    #
    # In practice the KNN produces no ties to break — 200 rows over 45,790
    # vectors came back with every distance distinct, float distances being what
    # they are — so this is closing a guarantee rather than fixing an observed
    # fault. The sort key below is `(distance, item_id)` so that if two ever do
    # tie, the answer is still the same answer twice.
    rows = conn.execute(
        f"SELECT item_id, distance FROM {VEC_TABLE} "
        f"WHERE embedding MATCH ? AND vault = ? "
        f"ORDER BY distance LIMIT ?",
        (query_vector, vault, int(k) * CHUNK_OVERSAMPLE)).fetchall()
    best: dict[str, float] = {}
    for item_id, distance in rows:      # arrive nearest-first
        if item_id not in best:
            best[item_id] = float(distance)
    return sorted(best.items(), key=lambda kv: (kv[1], kv[0]))[:int(k)]


def _open_or_reason(open_fn):
    """Try `open_fn()`, translating a failure into the wording `available()`
    reports for the same cause, whichever door it came through.

    **Shared, so the write-path degrade in `connect_rw_if_available()` names a
    missing or unreadable index in the exact words a caller would already see
    from `available()` / `/health`, rather than a second, independently-worded
    guess at the same fact** (issue_01M35R5GA1SKRG2CMQRF189NDE, round 1).

    **The returned reason is a fixed, bounded string — no path, no exception
    text** (security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 2). This
    reason reaches an authenticated caller's JSON response through
    `connect_rw_if_available()` (`/checkout`, `/claim`, `/revise`), and for
    revision it goes further still — `_briefing_note` writes it into
    `issue_comments` as a permanent record, not a transient response. The old
    wording interpolated the exception verbatim, which for the common missing-
    file case was `_attach_vectors_db`'s own message naming
    `VECTORS_DB_PATH` — the server's absolute filesystem path — landing in a
    durable comment any future holder of that item could read. `/health`'s own
    docstring already draws this line for `vectors.available()`'s callers
    (convivy/library#32: a filesystem path in a response is exactly what that
    route was hardened against); this closes the same gap here. The full
    exception, path included, still goes to the service log.
    """
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return None, "sqlite-vec is not installed, so the vector index cannot be read"
    try:
        return open_fn(), None
    except Exception as exc:  # noqa: BLE001 — any failure here means "unavailable"
        print(f"library: vector index unavailable — {type(exc).__name__}: {exc}",
              file=sys.stderr)
        if isinstance(exc, FileNotFoundError):
            return None, "the vector index file is missing or could not be opened"
        return None, "the vector index could not be opened"


def available(db_path=None, *, timeout: float = 5.0) -> tuple[bool, str]:
    """Whether semantic search can serve, and why not when it cannot.

    **A route that 500s because an extension is missing tells the caller nothing
    it can act on.** This turns that into a refusal naming the cause, and lets the
    startup line say up front whether this build can answer a search at all.

    **The fallback is `store.DB_PATH` rather than `ORIENT_CONTROL_DB`, and the
    difference is the whole of P4.** Reading the environment here was a second
    place that decided which database this process serves, so after the cutover a
    caller passing no path would have probed Orient's file and reported on an
    index this service no longer owns. There is one resolution of that question
    and it lives in `store`.

    `timeout` passes straight through to `connect`; see its docstring for why a
    readiness check wants a short one and a real search does not.
    """
    path = db_path or store.DB_PATH
    conn, reason = _open_or_reason(lambda: connect(path, timeout=timeout))
    if conn is None:
        return False, reason
    try:
        conn.execute(f"SELECT COUNT(*) FROM {VEC_TABLE} LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        return False, f"the vector index is unreadable ({exc})"
    finally:
        conn.close()
    return True, "ready"


def connect_rw_if_available() -> tuple[sqlite3.Connection | None, str | None]:
    """Best-effort `connect_rw()`: `(conn, None)` once the vector index attaches,
    or `(None, reason)` — in `available()`'s own wording — when it cannot.

    **For a caller whose own work does not depend on the vector index**, which
    today is `_do_checkout`, `_do_revision` and `_revision_briefing`: the
    adjacency briefing they run over the returned connection is advice, and a
    missing or unattachable `LIBRARY_VECTORS_DB` file must not take checkout,
    claim or revision down with it (reviewer + security-reviewer,
    issue_01M35R5GA1SKRG2CMQRF189NDE round 1). A caller with `None` back should
    fall back to a plain `store.connect_rw()` and skip the vector-backed part of
    its own work, reporting `reason` wherever it would otherwise have reported
    the briefing.

    **`connect_rw()` itself stays fail-loud, deliberately** — `embed_item_by_id`,
    `embed_missing` and the `__main__` sweep are not a caller with other work to
    fall back to; an unavailable index there is the whole job, not a side
    feature, and `_embed_after_commit` already catches that failure at its own
    call site the same best-effort way.
    """
    return _open_or_reason(connect_rw)


if __name__ == "__main__":
    # The drain, runnable by hand: `python vectors.py`. A repair path and the
    # backfill after a bulk insert, the same as Orient's.
    #
    # **NOT WIRED TO A SCHEDULE YET, AND THAT IS DELIBERATE.** `connect_rw`
    # refuses for the whole read-only shadow, so a background sweeper started
    # today would be a loop refusing on every tick — noise that looks like a
    # broken sweeper rather than a service behaving as configured. It belongs with
    # the write cutover, when Orient stops sweeping the same rows.
    total = 0
    while True:
        done = embed_missing()
        total += done
        if done == 0:
            break
        print(f"embedded {total} so far…")
    print(f"done: {total} rows embedded")
