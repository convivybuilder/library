"""What the vector-index tests share: a real vec0 table and a fake embedder.

**The embedder is a seam, and a deterministic one is worth more than a stub.** A
stub returning one constant vector proves a write reached the index and nothing
else — every pair is at cosine 1.0, so it cannot tell a near-duplicate from an
unrelated item, which is exactly what adjacency is for. The fake here hashes a
text's words into the index's own dimensions and normalises, so two texts sharing
words score high, two sharing none score zero, and the numbers are reproducible
across runs. No ONNX model, no download, no network.

**The vec0 table itself is REAL rather than faked**, because the failures worth
catching live in the extension. `sqlite-vec` reports `no such module: vec0` on a
connection that did not load it, an auxiliary column behaves differently from a
metadata one in a `WHERE` clause, and a dimension mismatch returns nonsense
neighbours rather than raising. A mock table would pass every one of those.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import struct
import tempfile
from pathlib import Path

# `store` resolves its database path once, at import — and every fixture in this
# suite overrides `store.DB_PATH` before making a request, never before this
# import runs. `resolve_db` now refuses rather than guessing when neither
# `LIBRARY_DB` nor `ORIENT_CONTROL_DB` is set, which is correct in a real
# deployment and true of this shell too, so a placeholder has to stand in here —
# one that provably is not a real database, unlike the Orient default this used
# to fall back to silently.
os.environ.setdefault("LIBRARY_DB", "/nonexistent/test-placeholder.db")

# `blobs.resolve_blob_dir` runs at import too, and unlike `LIBRARY_DB` above it
# checks the path actually exists — a placeholder that is merely a plausible
# string would refuse collection outright. Every fixture in this suite that
# cares about blob content overrides `blobs.BLOB_DIR` before making a request,
# never before this import runs, so this only has to be a real, writable
# directory `import blobs` can pass its startup check against; it is never
# read from by name.
os.environ.setdefault("LIBRARY_BLOB_DIR", tempfile.mkdtemp(prefix="library-test-blobs-"))

# `vectors.resolve_vectors_db_path` runs at import too, and unlike `LIBRARY_DB`
# above it never checks existence — see its own docstring for why. A
# placeholder string is enough to satisfy it; every fixture in this suite that
# cares about the vector index overrides `vectors.VECTORS_DB_PATH` before
# making a request, through the `vector_index` factory below.
os.environ.setdefault("LIBRARY_VECTORS_DB", "/nonexistent/test-placeholder-vectors.db")

# `store.resolve_cold_db_path` is resolved LAZILY, inside `attach_cold`,
# never cached on a module attribute the way `DB_PATH` and `VECTORS_DB_PATH`
# are — see that function's own docstring for why. So there is no module
# attribute to monkeypatch here; `_default_cold_db` below sets the
# environment variable itself, through `monkeypatch.setenv`, which every
# fixture that wants a real cold-tier schema (`cold_db`) overrides again
# after it runs.
os.environ.setdefault("LIBRARY_COLD_DB", "/nonexistent/test-placeholder-cold.db")

# `profile_sync.KNOWLEDGE_ROOT` is read at import from the served checkout's
# variable. A developer shell that exports it would make every profile read in
# this suite reconcile the `profiles` table against a real knowledge checkout,
# deleting the rows the fixtures seeded. Dropped, not defaulted: the suite
# never reads a served checkout, and `profile_sync`'s reload tests set the
# module attribute themselves.
os.environ.pop("LIBRARY_KNOWLEDGE_ROOT", None)

# `identity.CLIENT_TOKENS` resolves once at import too, the same way
# `store.DB_PATH` does. Most fixtures across this suite override `identity.TOKENS`
# before making a request but never `identity.CLIENT_TOKENS`, so without this
# default every one of their requests falls through `resolve()` to the
# operator's real `~/.library/client-tokens.json` (issue_01M339339P1WA80Q218SSTYKMT).
# A fixture that wants a populated client store still overrides this explicitly,
# as several already do.
os.environ.setdefault("LIBRARY_CLIENT_TOKENS",
                      "/nonexistent/test-placeholder-client-tokens.json")

import pytest

import store
import vectors

_WORD = re.compile(r"[a-z0-9]+")


def fake_vector(text: str) -> list[float]:
    """One text as a unit vector: its words hashed into the index's dimensions.

    Deterministic across processes — `hash()` is salted per run, so this uses a
    stable digest instead. A text with no words maps to the zero vector, which is
    the honest answer: it is similar to nothing, including itself.
    """
    values = [0.0] * vectors.EMBED_DIM
    for word in _WORD.findall(text.lower()):
        # A stable digest rather than hash(), whose seed changes per process.
        digest = sum((i + 1) * ord(c) for i, c in enumerate(word))
        values[digest % vectors.EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values] if norm else values


@pytest.fixture(autouse=True)
def _never_the_real_model(monkeypatch):
    """The fake embedder, installed for EVERY test in the suite.

    **Autouse, because the failure it prevents is silent and expensive.** Every
    write route now embeds after its commit, so a test that files an item without
    thinking about vectors reaches `_fastembed`, which downloads and loads an ONNX
    model. `_embed_after_commit` swallows the failure, so the test still passes —
    it just took several seconds and needed the network to do it, and nothing in
    the output says why. Installing the seam once for the whole suite means no
    future test can reintroduce that by omission.

    A test wanting different embedding behaviour overrides `vectors._embedder`
    itself; monkeypatch applies in order, so the later setting wins.
    """
    monkeypatch.setattr(
        vectors, "_embedder", lambda texts: [fake_vector(t) for t in texts])


@pytest.fixture(autouse=True)
def _default_vectors_db(tmp_path_factory, monkeypatch):
    """A real, empty vectors database for every test by default.

    **Autouse, for the same class of reason `_never_the_real_model` is.**
    `vectors.connect_rw` is not only the vector-write path: `_do_checkout`
    runs its adjacency briefing KNN on the very same connection
    (`app._do_checkout`'s own comment — "the briefing runs a KNN over `vec0`
    on this same connection once the lock has committed"), so `/checkout` and
    every route through it need a real, attachable vectors file even when a
    test never touches a vector on purpose. Without this, only the test
    modules that happen to call `vector_index` would have one, and every
    other route exercising `_do_checkout` would fail on the placeholder
    `conftest.py` sets for `LIBRARY_VECTORS_DB` before collection.

    A test that wants seeded rows calls `vector_index` below, whose own
    `monkeypatch.setattr` on `vectors.VECTORS_DB_PATH` runs after this one and
    so takes over the module attribute this fixture set.
    """
    sqlite_vec = pytest.importorskip("sqlite_vec")
    path = tmp_path_factory.mktemp("default-vectors") / "vectors.db"
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(vectors.VEC_TABLE_DDL)
    conn.execute(vectors.SIGNAL_VEC_TABLE_DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", path)


@pytest.fixture
def vector_index(monkeypatch):
    """A factory: call it with a MAIN database path to add the vec0 index,
    IN ITS OWN FILE, and point `vectors.VECTORS_DB_PATH` at it — the same
    split `vectors.connect` / `connect_rw` ATTACH in production.

    A factory rather than a fixture over the database, because every test module
    here builds its own schema for the tables it needs — there is no shared one to
    hang this off.

    **The vectors file sits beside the one passed in, `<name>.vectors`,** so a
    caller can still recover it from the main path alone if it needs to —
    `vectors_for` below does exactly that, ignoring its own `db_path`
    parameter in favour of `vectors.VECTORS_DB_PATH`, which is what every
    caller of this fixture is really asking about.

    **`db_path` is created, empty, if nothing is there yet.** The old,
    single-file design made this true as a side effect of building the vec0
    table directly inside it; several callers still pass a `db_path` they
    never build a schema in themselves and rely on `vectors.connect(db_path)`
    being able to open it at all.
    """
    def build(db_path):
        sqlite_vec = pytest.importorskip("sqlite_vec")
        sqlite3.connect(db_path).close()
        vectors_path = Path(f"{db_path}.vectors")
        conn = sqlite3.connect(vectors_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(vectors.VEC_TABLE_DDL)
        conn.execute(vectors.SIGNAL_VEC_TABLE_DDL)
        conn.commit()
        conn.close()
        monkeypatch.setattr(vectors, "VECTORS_DB_PATH", vectors_path)
        return vectors_path
    return build


@pytest.fixture(autouse=True)
def _default_cold_db(tmp_path_factory, monkeypatch):
    """A real cold-tier file for every test by default, holding an EMPTY
    `signals` table shaped like `store.TWO_TIER_SIGNAL_COLUMNS`.

    **Autouse, for the same reason `_default_vectors_db` is.** Several
    signal read routes attach `cold` on every call now, not only once a hot
    miss is found (`GET /signals`, `/signals/facets`, `/signals/pr-state`),
    so every test that exercises one of them — the vast majority of this
    suite's app tests, which never touch the cold tier on purpose — needs a
    real, correctly-shaped file at `LIBRARY_COLD_DB`. A bare empty file
    (no `signals` table) would let `attach_cold` succeed and then fail the
    very next statement with `no such table: cold.signals`.

    A test that wants cold ROWS calls `cold_db` below, whose own
    `monkeypatch.setenv` runs after this one and so takes over the variable
    this fixture set.
    """
    path = tmp_path_factory.mktemp("default-cold") / "cold.db"
    cols = ", ".join(f"{c} TEXT" for c in store.TWO_TIER_SIGNAL_COLUMNS)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE signals ({cols})")
    conn.commit()
    conn.close()
    monkeypatch.setenv("LIBRARY_COLD_DB", str(path))


@pytest.fixture
def cold_db(monkeypatch):
    """A factory: call it with the `CREATE TABLE signals (...)` script a
    test module's own main database uses, to build an identically-shaped
    `signals` table in a SEPARATE file, and point `LIBRARY_COLD_DB` at it —
    the real split `store.attach_cold` ATTACHes in production, where
    `init_cold.py` replays `main`'s own schema verbatim rather than this
    suite's hand-written copy of it.

    A factory rather than a fixture over the database, because every test
    module that needs cold rows builds its own reduced `signals` schema for
    the columns it exercises — the same reason `vector_index` above is one.
    """
    def build(schema_sql: str):
        path = Path(tempfile.mkdtemp(prefix="library-test-cold-")) / "cold.db"
        conn = sqlite3.connect(path)
        conn.executescript(schema_sql)
        conn.commit()
        conn.close()
        monkeypatch.setenv("LIBRARY_COLD_DB", str(path))
        return path
    return build


def vectors_for(db_path, item_id: str) -> list[bytes]:
    """One item's stored vectors, nearest the raw bytes — what a test asserts on
    when it wants to know whether the index moved.

    `db_path` is accepted for every call site's convenience but not read —
    the vectors live at `vectors.VECTORS_DB_PATH`, which `vector_index`
    already pointed there for this test."""
    del db_path
    sqlite_vec = pytest.importorskip("sqlite_vec")
    conn = sqlite3.connect(vectors.VECTORS_DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        return [r[0] for r in conn.execute(
            f"SELECT embedding FROM {vectors.VEC_TABLE} WHERE item_id = ?",
            (item_id,)).fetchall()]
    finally:
        conn.close()


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{vectors.EMBED_DIM}f", blob))


@pytest.fixture
def ledger_tables():
    """A factory: call it with a database path to add the ledger tables.

    Needed by every module that exercises checkout, because admission now reads
    a ledger — `_refuse_on_capped_ledger` queries `ledgers` on every claim.

    **A minimal shape, and deliberately not a copy of the migration's.** The
    constraints that matter — the partial unique index, the day-shape CHECK, the
    unique `dedup_key` — are tested in `test_ledger_store.py` against a database
    built by running the real migration. Repeating them here would be a second
    copy to keep in step, and a fixture that drifts is worse than one that is
    obviously partial. What these tests need is a table the query can read.
    """
    def build(db_path):
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ledgers (
                id TEXT PRIMARY KEY, scope_kind TEXT NOT NULL,
                scope_id TEXT NOT NULL, warn_usd_micro INTEGER,
                soft_cap_usd_micro INTEGER, hard_cap_usd_micro INTEGER,
                per_run_cap_usd_micro INTEGER,
                state TEXT NOT NULL DEFAULT 'live', granted_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ledgers_one_live_per_scope
                ON ledgers (scope_kind, scope_id) WHERE state = 'live';
            CREATE TABLE IF NOT EXISTS ledger_days (
                ledger_id TEXT NOT NULL, day TEXT NOT NULL,
                spent_usd_micro INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, PRIMARY KEY (ledger_id, day));
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id TEXT PRIMARY KEY, ledger_id TEXT NOT NULL, day TEXT NOT NULL,
                usd_micro INTEGER NOT NULL, at TEXT NOT NULL,
                ingested_at TEXT NOT NULL, source TEXT NOT NULL,
                product TEXT NOT NULL, run_id TEXT, agent TEXT, model TEXT,
                item_id TEXT, person TEXT, team TEXT, reported_by TEXT NOT NULL,
                dedup_key TEXT UNIQUE, voided_at TEXT, voided_by TEXT,
                void_reason TEXT);
        """)
        conn.commit()
        conn.close()
    return build
