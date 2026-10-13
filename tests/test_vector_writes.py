"""The writable half of the vector index: the handle, the embed, the sweep.

Three properties carry the real risk, and most of this file is them.

**The shadow's refusal has to hold here too.** `vectors.connect_rw` is a second
door onto a writable vault handle, and the whole read-only shadow rests on there
being exactly one. It opens through `store.connect_rw`, so the refusal is the same
one every other write path hits — asserted rather than assumed, because a
`sqlite3.connect` added here would look identical and quietly bypass it.

**A best-effort embed is only safe because the sweep exists.** A failed embed must
leave a durable row rather than costing the capture, which is right only if
something later notices what it left behind. So the failure path and the sweep are
tested as one pair.

**The model never runs inside the write lock.** That ordering is not style: Orient
measured eight concurrent posts returning in multiples of `busy_timeout`, six of
them 500s, when the model call sat inside the open write transaction. What a test
can hold is the observable shape of it — the row is committed and readable before
the embed lands, and an embed that throws leaves the row behind.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import conftest
import identity
import projects
import protocol
import store
import vectors

AGENT = "agent-token-vivy"

REGISTRY = {"_scopes": {"platform": {}}, "vivy": {"aliases": [], "repos": []}}


@pytest.fixture
def db(tmp_path, monkeypatch, vector_index):
    """Both embedded vaults, and the vec0 index over them."""
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT);
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            from_state TEXT, to_state TEXT, at TEXT);
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE corrections_queues (
            id TEXT PRIMARY KEY, product TEXT UNIQUE, created_at TEXT);
    """)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, type, state, "
        "created_at) VALUES (?,?,?,?,'bug','open',?)",
        [("item_old", "vivy", "The older item", "about leases",
          "2026-08-01 09:00:00.000000"),
         ("item_new", "vivy", "The newer item", "about vectors",
          "2026-08-01 10:00:00.000000")])
    conn.executemany(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at) VALUES (?,?,'note',?,'internal',?)",
        [("sig_one", "vivy", "a captured thing", "2026-08-01 09:30:00.000000")])
    conn.commit()
    conn.close()
    vector_index(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]}}))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    # PINNED, so an edit to the real registry cannot break these tests.
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h():
    return {"Authorization": f"Bearer {AGENT}",
            protocol.HEADER: str(protocol.CURRENT)}


def _stamp(db, table, item_id):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(f"SELECT embedded_at FROM {table} WHERE id = ?",
                           (item_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# --- the shadow gate --------------------------------------------------------------


def test_a_writable_vector_handle_is_refused_while_the_shadow_is_running(
        db, monkeypatch):
    """The one control the whole shadow rests on. A second `sqlite3.connect` here
    would look identical to this function and bypass it silently, which is why the
    refusal is asserted on the vector door specifically rather than trusted to
    `store`."""
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        vectors.connect_rw()


def test_the_writable_handle_has_vec0_loaded(db):
    """Without the extension a writable connection opens fine and fails at the
    vector write — `no such module: vec0`, mid-transaction, on the write path.
    That is the seam platform/reference/keel-goal-baselines records, and ADR-0034
    carries the write side of it."""
    conn = vectors.connect_rw()
    try:
        assert conn.execute(
            f"SELECT COUNT(*) FROM {vectors.VEC_TABLE}").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_writable_handle_cannot_load_another_extension(db):
    """Loading is switched off again after `vec0`, so nothing later in the process
    can pull an arbitrary extension in through this handle."""
    conn = vectors.connect_rw()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.load_extension("anything")
    finally:
        conn.close()


# --- chunking ---------------------------------------------------------------------


def test_short_text_is_one_chunk(db):
    assert vectors.chunks("a short body") == ["a short body"]


def test_a_long_body_becomes_several_overlapping_chunks(db):
    """bge-small attends to ~512 tokens, so one vector for a long body represents
    only its opening — Orient's first real transcript ingest proved
    content-specific queries miss."""
    body = " ".join(f"word{n}" for n in range(2000))
    parts = vectors.chunks(body)
    assert len(parts) > 1
    assert all(len(p) <= vectors.CHUNK_CHARS for p in parts)
    # Overlapping, so a sentence straddling a boundary is findable from either
    # side: the second chunk starts inside what the first one covered.
    assert parts[1].split()[0] in parts[0]


def test_a_chunk_ends_on_a_whole_word(db):
    """The window's end snaps back to the last whitespace inside it.

    **Its START may land mid-word, and that is the overlap doing its job** rather
    than an oversight: the next window begins a fixed number of characters before
    the previous end, so a sentence straddling the boundary is covered from both
    sides. Carried across from Orient unchanged, since the index's geometry is a
    data migration rather than a redesign."""
    body = " ".join(f"word{n}" for n in range(2000))
    for part in vectors.chunks(body):
        assert part.split()[-1].startswith("word")


def test_text_with_nothing_in_it_is_no_chunks(db):
    assert vectors.chunks("   ") == []


# --- the embedding itself ---------------------------------------------------------


def test_a_dimension_mismatch_is_an_error_rather_than_bad_neighbours(
        db, monkeypatch):
    """vec0 does not error on a wrong-width vector, it silently returns nonsense
    neighbours, so the dimension is asserted on every embed rather than trusted."""
    monkeypatch.setattr(vectors, "_embedder", lambda texts: [[0.0] * 128])
    with pytest.raises(ValueError, match="dimensions"):
        vectors.embed_texts(["anything"])


def test_a_batch_is_one_model_call(db, monkeypatch):
    calls = []

    def counting(texts):
        calls.append(list(texts))
        return [conftest.fake_vector(t) for t in texts]

    monkeypatch.setattr(vectors, "_embedder", counting)
    vectors.embed_texts(["one", "two", "three"])
    assert len(calls) == 1 and len(calls[0]) == 3


def test_an_item_embeds_its_title_as_well_as_its_body(db):
    """An item filed with a precise title and an empty body is normal, so
    embedding the body alone would index nothing for exactly the items adjacency
    most needs to find."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM issue_items WHERE id='item_old'").fetchone()
    conn.close()
    assert vectors.text_for_item(row) == "The older item\n\nabout leases"


def test_a_signal_with_an_empty_body_falls_back_to_its_kind(db):
    """An empty embedding target indexes noise."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE signals SET body='', stream='inbox' WHERE id='sig_one'")
    conn.commit()
    row = conn.execute("SELECT * FROM signals WHERE id='sig_one'").fetchone()
    conn.close()
    assert vectors.text_for_signal(row) == "note inbox"


def test_an_unknown_vault_is_refused_by_name(db):
    with pytest.raises(ValueError, match="unknown vault"):
        vectors.embed_item_by_id("reference_room", "item_old")


# --- embed, re-embed, invalidate --------------------------------------------------


def test_embedding_a_row_writes_its_vectors_and_stamps_it(db):
    assert _stamp(db, "issue_items", "item_old") is None
    stamp = vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "item_old")
    assert stamp is not None
    assert _stamp(db, "issue_items", "item_old") == stamp
    stored = conftest.vectors_for(db, "item_old")
    assert len(stored) == 1
    assert conftest.unpack(stored[0]) == pytest.approx(
        conftest.fake_vector("The older item\n\nabout leases"))


def test_re_embedding_an_embedded_row_writes_nothing_and_returns_its_stamp(db):
    """Idempotent on `embedded_at`. Returning the row's real stamp rather than
    None is what stops a caller's response disagreeing with the stored row."""
    first = vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "item_old")
    again = vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "item_old")
    assert again == first
    assert len(conftest.vectors_for(db, "item_old")) == 1


def test_a_missing_row_returns_none_and_that_means_gone(db):
    """None means one thing rather than two: an already-embedded row returns its
    stamp, so a caller can tell 'deleted' from 'nothing to do'."""
    assert vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "no_such") is None


def test_invalidating_drops_the_vectors_and_clears_the_stamp(db):
    vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "item_old")
    conn = vectors.connect_rw()
    try:
        vectors.invalidate_item(conn, vectors.VAULT_CHART_HOUSE, "item_old")
        conn.commit()
    finally:
        conn.close()
    assert conftest.vectors_for(db, "item_old") == []
    assert _stamp(db, "issue_items", "item_old") is None


def test_invalidating_one_item_leaves_its_neighbours_alone(db):
    vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "item_old")
    vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "item_new")
    vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_one")
    conn = vectors.connect_rw()
    try:
        vectors.invalidate_item(conn, vectors.VAULT_CHART_HOUSE, "item_old")
        conn.commit()
    finally:
        conn.close()
    assert conftest.vectors_for(db, "item_old") == []
    assert len(conftest.vectors_for(db, "item_new")) == 1
    assert len(conftest.vectors_for(db, "sig_one")) == 1


def test_a_long_body_stores_one_vector_per_chunk(db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE issue_items SET body = ? WHERE id='item_old'",
                 (" ".join(f"word{n}" for n in range(2000)),))
    conn.commit()
    conn.close()
    vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "item_old")
    assert len(conftest.vectors_for(db, "item_old")) > 1


# --- kind-gated embedding, library/decisions/0001 ----------------------------------


def _signal(db, signal_id, kind, body="some words about it"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at) VALUES (?,?,?,?,'internal',?)",
        (signal_id, "vivy", kind, body, "2026-08-01 11:00:00.000000"))
    conn.commit()
    conn.close()


def test_a_transcript_turn_is_embedded(db):
    """`transcript-turn` is embedded per the operator's 2026-10-05 ruling: a turn gets
    real vectors, not a zero-vector stamp."""
    _signal(db, "sig_turn", "transcript-turn", body="what the operator said about the pool")
    stamp = vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_turn")
    assert stamp is not None
    stored = conftest.vectors_for(db, "sig_turn")
    assert len(stored) == 1
    assert conftest.unpack(stored[0]) == pytest.approx(
        conftest.fake_vector("what the operator said about the pool"))


def test_a_not_embedded_kind_is_stamped_with_no_vectors(db):
    """`alert` is one of the decision's named not-embedded kinds."""
    _signal(db, "sig_alert", "alert")
    stamp = vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_alert")
    assert stamp is not None, "stamped, so the sweep does not reselect it forever"
    assert conftest.vectors_for(db, "sig_alert") == []


def test_a_not_embedded_kind_never_reaches_the_model(db, monkeypatch):
    calls = []
    monkeypatch.setattr(vectors, "_embedder", lambda texts: calls.append(list(texts))
                         or [conftest.fake_vector(t) for t in texts])
    _signal(db, "sig_alert", "alert")
    vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_alert")
    assert calls == [], "a kind the decision marks not embedded costs no model call"


def test_an_unlisted_kind_defaults_to_not_embedded(db):
    """The decision's own Defaults section: a kind it does not name is kept, not
    embedded, and cold."""
    _signal(db, "sig_new", "some-new-kind")
    stamp = vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_new")
    assert stamp is not None
    assert conftest.vectors_for(db, "sig_new") == []


def test_an_embedded_kind_still_gets_vectors(db):
    _signal(db, "sig_finding", "finding", body="the pool leaked a connection")
    stamp = vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_finding")
    assert stamp is not None
    stored = conftest.vectors_for(db, "sig_finding")
    assert len(stored) == 1
    assert conftest.unpack(stored[0]) == pytest.approx(
        conftest.fake_vector("the pool leaked a connection"))


def test_the_sweep_skips_a_not_embedded_kind_but_still_stamps_it(db):
    _signal(db, "sig_alert", "alert")
    assert vectors.embed_missing(vault=vectors.VAULT_ARCHIVES) == 2  # sig_one, sig_alert
    assert _stamp(db, "signals", "sig_alert") is not None
    assert conftest.vectors_for(db, "sig_alert") == []
    assert len(conftest.vectors_for(db, "sig_one")) == 1
    # Stamped, so a second sweep does not reselect it.
    assert vectors.embed_missing(vault=vectors.VAULT_ARCHIVES) == 0


# --- the sweep --------------------------------------------------------------------


def test_the_sweep_embeds_every_vault_by_default(db):
    """One call keeps the whole Library drained, so adding a vault does not mean
    remembering to add a sweeper."""
    assert vectors.embed_missing() == 3          # two items and one signal
    assert vectors.embed_missing() == 0          # nothing left to do
    assert len(conftest.vectors_for(db, "item_old")) == 1
    assert len(conftest.vectors_for(db, "sig_one")) == 1


def test_the_sweep_can_be_narrowed_to_one_vault(db):
    assert vectors.embed_missing(vault=vectors.VAULT_ARCHIVES) == 1
    assert _stamp(db, "issue_items", "item_old") is None


def test_the_sweep_takes_the_oldest_first(db):
    """A backlog is drained in the order it accumulated, so a batch limit does not
    starve the oldest rows forever."""
    assert vectors.embed_missing(limit=1, vault=vectors.VAULT_CHART_HOUSE) == 1
    assert _stamp(db, "issue_items", "item_old") is not None
    assert _stamp(db, "issue_items", "item_new") is None


def _counting_connect_rw(monkeypatch):
    """Wrap `connect_rw` so a test can count the sweep's transactions.

    Counting `BEGIN IMMEDIATE` rather than `commit()` because the bound is about
    how long the LOCK is held, and the lock is taken by that statement.
    """
    real = vectors.connect_rw
    begins = []

    def counting():
        # `set_trace_callback` rather than wrapping `execute`, which is read-only
        # on a sqlite3.Connection.
        conn = real()
        conn.set_trace_callback(
            lambda sql: begins.append(sql)
            if sql.strip().upper().startswith("BEGIN") else None)
        return conn

    monkeypatch.setattr(vectors, "connect_rw", counting)
    return begins


def _fill(db, n, words):
    """`n` un-embedded chart-house rows, each chunking to more than one vector."""
    body = " ".join(f"word{i}" for i in range(words))
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, type, state, "
        "created_at) VALUES (?,?,?,?,'bug','open',?)",
        [(f"bulk_{i:04d}", "vivy", f"bulk item {i}", body,
          f"2026-08-02 09:{i // 60:02d}:{i % 60:02d}.000000") for i in range(n)])
    conn.commit()
    conn.close()


def test_the_sweep_bounds_how_long_it_holds_the_write_lock(db, monkeypatch):
    """**THE WHOLE BATCH USED TO COMMIT IN ONE TRANSACTION**, which is fine while
    the sweep is unwired and becomes a convoy the moment it is scheduled: the
    worst case measured 2,324 ms against a 5,000 ms `busy_timeout`, and every
    concurrent ingest and revision waits that out before it 500s.

    Asserted as "more than one transaction over a batch past the bound" rather
    than as a duration, because a wall-clock assertion is a flaky test on a busy
    machine and says nothing on a fast one.
    """
    monkeypatch.setattr(vectors, "MAX_CHUNKS_PER_TXN", 4)
    _fill(db, 12, words=600)                       # ~3 chunks each, 36 total
    begins = _counting_connect_rw(monkeypatch)
    assert vectors.embed_missing(vault=vectors.VAULT_CHART_HOUSE) == 14
    assert len(begins) > 1, "the batch still commits as one transaction"


def test_no_row_is_split_across_two_transactions(db, monkeypatch):
    """**A row bigger than the bound commits whole rather than in pieces.**
    `write_if_current` checks the row and writes its vectors in one act, so
    splitting a row would put its check in one transaction and part of its write
    in another — reopening exactly the window that check exists to close.
    """
    monkeypatch.setattr(vectors, "MAX_CHUNKS_PER_TXN", 2)
    _fill(db, 1, words=4000)                       # one row, well past the bound
    begins = _counting_connect_rw(monkeypatch)
    assert vectors.embed_missing(vault=vectors.VAULT_CHART_HOUSE) == 3
    assert len(conftest.vectors_for(db, "bulk_0000")) > 2, (
        "the oversized row should hold all of its vectors")
    assert _stamp(db, "issue_items", "bulk_0000") is not None
    # Two transactions: the fixture's two one-chunk rows fill the first exactly,
    # and the oversized row — which alone exceeds the bound — commits whole in the
    # second rather than being cut at the bound.
    assert len(begins) == 2


def test_a_row_with_no_text_does_not_let_the_next_one_escape_the_bound(
        db, monkeypatch):
    """**THE BOUND HELD FOR EVERY ROW EXCEPT THE ONE AFTER AN EMPTY ONE**
    (reviewer, PR #21). The commit check was guarded on `pending` being truthy,
    to avoid committing an empty transaction ahead of an oversized first row. A
    zero-chunk row leaves `pending` at 0 — `insert_vectors` stamps it holding no
    vectors, which it documents as reachable for a legacy row — so the row after
    it was merged into the open transaction with no bound check at all, however
    large.

    None of the other tests here plant an empty row, which is exactly why the gap
    survived them.

    **The empty row and the oversized one have to be ADJACENT in sweep order.**
    A first draft of this test left the fixture's one-chunk row between them,
    which put `pending` back above zero before the big row was reached — so it
    passed against the planted bug and proved nothing. The sweep takes oldest
    first, so the dates below are what put the two rows together.
    """
    monkeypatch.setattr(vectors, "MAX_CHUNKS_PER_TXN", 4)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE issue_items SET title = '', body = '', "
                 "created_at = '2026-07-01 00:00:00.000000' WHERE id='item_old'")
    conn.execute("UPDATE issue_items SET created_at = '2026-09-01 00:00:00.000000' "
                 "WHERE id='item_new'")
    conn.commit()
    conn.close()
    _fill(db, 1, words=2000)                       # ~9 chunks, well past the bound
    begins = _counting_connect_rw(monkeypatch)
    vectors.embed_missing(vault=vectors.VAULT_CHART_HOUSE)
    assert conftest.vectors_for(db, "item_old") == [], (
        "the empty row should be stamped holding no vectors")
    assert _stamp(db, "issue_items", "item_old") is not None
    # Swept empty → big → small. The big row must open its own transaction
    # instead of riding along in the empty row's, which is three in total; the
    # guarded version gave two, merging the big row into the first.
    assert len(begins) == 3


def test_a_batch_that_dies_partway_keeps_the_groups_it_committed(db, monkeypatch):
    """**THE ONE BEHAVIOUR THAT CHANGED, asserted rather than assumed.** The
    single transaction rolled the whole batch back on a failure; bounded groups
    keep what they committed. Both are correct because the sweep is resumable —
    an un-embedded row is simply still un-embedded — and this version does
    strictly less work twice.
    """
    monkeypatch.setattr(vectors, "MAX_CHUNKS_PER_TXN", 4)
    _fill(db, 12, words=600)
    real_insert = vectors.insert_vectors
    calls = []

    def failing(conn, vault, item_id, blobs):
        calls.append(item_id)
        if len(calls) > 6:
            raise RuntimeError("the disk went away mid-batch")
        return real_insert(conn, vault, item_id, blobs)

    monkeypatch.setattr(vectors, "insert_vectors", failing)
    with pytest.raises(RuntimeError):
        vectors.embed_missing(vault=vectors.VAULT_CHART_HOUSE)
    stamped = [c for c in calls[:6]
               if _stamp(db, "issue_items", c) is not None]
    assert stamped, "every committed group was rolled back with the failing one"


def test_a_slower_embed_does_not_stamp_the_row_with_superseded_text(db):
    """The staleness race, and the one a stamp check alone cannot catch.

    Two revisions overlap: A commits text X1 and starts its model call, B commits
    X2 while that call is in flight. Both cleared `embedded_at`, so both pass an
    "is it stamped" check — and if A reaches the write first, the row ends up
    stamped holding vectors for text it no longer contains, permanently, with
    every later search and briefing scoring the wrong words.

    Driven directly rather than through the route, because what has to be
    reproduced is the INTERLEAVING: A's read, then B's whole revision, then A's
    write."""
    # A reads the text it is about to embed.
    conn = vectors.connect_rw()
    try:
        row = conn.execute(
            "SELECT * FROM issue_items WHERE id='item_old'").fetchone()
        text_a = vectors.text_for_item(row)
    finally:
        conn.close()
    blobs_a = vectors.embed_texts(vectors.chunks(text_a))

    # B's revision lands while A's model is still running.
    conn = vectors.connect_rw()
    try:
        conn.execute("UPDATE issue_items SET body='wholly different words' "
                     "WHERE id='item_old'")
        vectors.invalidate_item(conn, vectors.VAULT_CHART_HOUSE, "item_old")
        conn.commit()
    finally:
        conn.close()

    # A now tries to write. It must stand down: what it embedded is not what the
    # row says any more.
    conn = vectors.connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        wrote, stamp = vectors.write_if_current(
            conn, vectors.VAULT_CHART_HOUSE, "item_old", text_a, blobs_a)
        conn.commit()
    finally:
        conn.close()
    assert wrote is False and stamp is None
    assert conftest.vectors_for(db, "item_old") == []
    # AND THE ROW IS LEFT UN-EMBEDDED RATHER THAN WRONGLY STAMPED, which is what
    # lets the sweep put it right.
    assert _stamp(db, "issue_items", "item_old") is None
    assert vectors.embed_missing(vault=vectors.VAULT_CHART_HOUSE) >= 1
    assert conftest.unpack(conftest.vectors_for(db, "item_old")[0]) == \
        pytest.approx(conftest.fake_vector(
            "The older item\n\nwholly different words"))


def test_two_overlapping_embeds_do_not_write_two_sets_of_vectors(db):
    """The duplicate half of the same race: both callers read `embedded_at` NULL
    for the same unchanged text, so both would lay down a full set and the item
    would hold two. `BEGIN IMMEDIATE` plus the stamp check makes the second one a
    no-op."""
    conn = vectors.connect_rw()
    try:
        row = conn.execute(
            "SELECT * FROM issue_items WHERE id='item_old'").fetchone()
        text = vectors.text_for_item(row)
    finally:
        conn.close()
    blobs = vectors.embed_texts(vectors.chunks(text))

    stamps = []
    for _ in range(2):
        conn = vectors.connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            stamps.append(vectors.write_if_current(
                conn, vectors.VAULT_CHART_HOUSE, "item_old", text, blobs))
            conn.commit()
        finally:
            conn.close()
    assert stamps[0][0] is True and stamps[1][0] is False
    assert stamps[1][1] == stamps[0][1]       # the second reports the first's
    assert len(conftest.vectors_for(db, "item_old")) == 1


def test_the_sweep_skips_a_row_embedded_since_the_batch_was_read(db,
                                                                monkeypatch):
    """The re-check in the write step, and the race it is for: ingest embeds after
    its commit, so a row can become embedded during the batch's model call — the
    longest window in the system. Writing the batch blind would lay a second set
    of vectors over a row that already has them."""
    real = vectors.embed_texts
    raced = []

    def embed_then_race(texts):
        out = real(texts)
        if not raced:
            # The concurrent ingest embed, landing while the model runs. Guarded,
            # because the racing call goes through this same seam.
            raced.append(True)
            vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, "item_old")
        return out

    monkeypatch.setattr(vectors, "embed_texts", embed_then_race)
    assert vectors.embed_missing(vault=vectors.VAULT_CHART_HOUSE) == 1
    assert len(conftest.vectors_for(db, "item_old")) == 1


# --- the routes -------------------------------------------------------------------


def test_a_created_signal_is_embedded_and_is_therefore_searchable(client, db):
    """`POST /signals` used to commit the row and stop, so the Library's own
    semantic search silently missed everything the Library itself captured — worse
    than an outright failure, because the search keeps answering."""
    r = client.post("/signals", json={
        "source": "github", "kind": "note", "actor": "someone",
        "product": "vivy", "body": "a thing happened"}, headers=_h())
    assert r.status_code == 201
    sid = r.json()["id"]
    assert len(conftest.vectors_for(db, sid)) == 1
    assert _stamp(db, "signals", sid) is not None


def test_a_created_item_is_embedded(client, db):
    """Adjacency reads item vectors, so an item filed through the Library and not
    embedded is one the briefing can never find."""
    r = client.post("/items", json={
        "product": "vivy", "title": "A newly filed item",
        "body": "with a body", "severity": "medium"}, headers=_h())
    assert r.status_code == 201, r.json()
    assert len(conftest.vectors_for(db, r.json()["id"])) == 1


def test_an_idempotent_reply_does_not_re_embed_somebody_elses_row(client, db):
    """A 200 answers with a row this call did not create."""
    first = client.post("/items", json={
        "product": "vivy", "title": "Filed once", "severity": "medium",
        "once_key": "k"}, headers=_h())
    assert first.status_code == 201
    stamp = _stamp(db, "issue_items", first.json()["id"])
    again = client.post("/items", json={
        "product": "vivy", "title": "Filed once", "severity": "medium",
        "once_key": "k"}, headers=_h())
    assert again.status_code == 200
    assert len(conftest.vectors_for(db, first.json()["id"])) == 1
    assert _stamp(db, "issue_items", first.json()["id"]) == stamp


def test_an_embedding_failure_does_not_cost_the_capture(client, db,
                                                        monkeypatch):
    """A capture is usually the only copy. What a failure leaves behind is a
    durable row with `embedded_at` NULL — and the sweep is what makes that
    best-effort rather than silently dropped."""
    def broken(_texts):
        raise RuntimeError("the model is not here")

    monkeypatch.setattr(vectors, "_embedder", broken)
    r = client.post("/signals", json={
        "source": "github", "kind": "note", "actor": "someone",
        "product": "vivy", "body": "captured while the model was down"},
        headers=_h())
    assert r.status_code == 201
    sid = r.json()["id"]
    assert conftest.vectors_for(db, sid) == []
    assert _stamp(db, "signals", sid) is None

    # And the sweep picks it up, which is the half that makes the above safe.
    monkeypatch.setattr(
        vectors, "_embedder", lambda texts: [conftest.fake_vector(t)
                                             for t in texts])
    assert vectors.embed_missing(vault=vectors.VAULT_ARCHIVES) >= 1
    assert len(conftest.vectors_for(db, sid)) == 1


def test_the_row_is_committed_before_the_embed_runs(client, db):
    """The ordering the whole design rests on: the model must never run while the
    write lock is held. Observable as the row being readable from a SEPARATE
    connection by the time the embedder is called — which it could not be if the
    embed sat inside the row's own open transaction."""
    seen = {}

    def embed_and_look(texts):
        conn = sqlite3.connect(db)
        try:
            seen["rows"] = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE body = ?",
                ("a thing happened",)).fetchone()[0]
        finally:
            conn.close()
        return [conftest.fake_vector(t) for t in texts]

    libapp.vectors.set_embedder(embed_and_look)
    try:
        r = client.post("/signals", json={
            "source": "github", "kind": "note", "actor": "someone",
            "product": "vivy", "body": "a thing happened"}, headers=_h())
    finally:
        libapp.vectors.set_embedder(None)
    assert r.status_code == 201
    assert seen["rows"] == 1


# --- a failed vectors attach does not leak the store handle -------------------

def _spy_on_connections(monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy)
    return opened


def _refuse_attach(conn, *, readonly):
    raise RuntimeError("attach refused")


def _assert_every_handle_closed(opened):
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_a_read_handle_is_closed_when_the_vectors_attach_fails(db, monkeypatch):
    opened = _spy_on_connections(monkeypatch)
    monkeypatch.setattr(vectors, "_attach_vectors_db", _refuse_attach)
    with pytest.raises(RuntimeError, match="attach refused"):
        vectors.connect(db)
    _assert_every_handle_closed(opened)


def test_a_write_handle_is_closed_when_the_vectors_attach_fails(db, monkeypatch):
    opened = _spy_on_connections(monkeypatch)
    monkeypatch.setattr(vectors, "_attach_vectors_db", _refuse_attach)
    with pytest.raises(RuntimeError, match="attach refused"):
        vectors.connect_rw()
    _assert_every_handle_closed(opened)
