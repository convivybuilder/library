"""`split_vectors.py`: the one-time data migration that moves
`library_vectors` and `signal_vectors` out of the main vault database
(memory-retention epic clause 7, issue_01M35R5GA1SKRG2CMQRF189NDE).

**Search identical across the split is the acceptance test, not the row
counts** — the same reasoning `test_migrate.py` states for the Orient
cutover's own `verify`. A shadow-table copy reproduces every count exactly
and answers a KNN with nonsense; this file proves `vectors.nearest` returns
byte-identical distances, in the same order, against the source before the
split and the vectors file after it.
"""
from __future__ import annotations

import sqlite3
import struct

import pytest

import split_vectors
import vectors

sqlite_vec = pytest.importorskip("sqlite_vec")

# Captured before any test's `monkeypatch.setattr(split_vectors,
# "_service_running", ...)` runs, so the real-probe tests below can call the
# actual implementation even though the autouse `_no_live_service` fixture
# has overwritten `split_vectors._service_running` by the time they run.
_real_service_running = split_vectors._service_running


@pytest.fixture(autouse=True)
def _no_live_service(monkeypatch):
    """`split_vectors._service_running()` says `(False, ...)` for every test
    by default, whatever this machine's own launchd job is actually doing.

    **Autouse, and load-bearing on THIS machine specifically**: the real
    Library service is a launchd job on this developer's own box, listening
    on the exact port `guard()` now probes, so every test below that calls
    `export_vectors`/`verify`/`drop_and_vacuum` without this would trip the
    new service-running refusal against a service that has nothing to do
    with the test — not a false positive the guard should ever produce in
    production, where a scratch run sharing a machine with the live service
    is the case `--i-mean-it` exists for, but a real one here. Tests that
    want the refusal itself override this back to `(True, ...)`.
    """
    monkeypatch.setattr(split_vectors, "_service_running",
                         lambda: (False, "nothing is listening (test default)"))


def _vec(dominant: int) -> bytes:
    """A packed float32 vector with one coordinate set, the rest zero — real
    dimensionality (`vectors.EMBED_DIM`), since `split_vectors` creates its
    destination tables with the canonical, fixed-width DDL rather than a
    scratch dimension."""
    values = [0.0] * vectors.EMBED_DIM
    values[dominant % vectors.EMBED_DIM] = 1.0
    return struct.pack(f"{vectors.EMBED_DIM}f", *values)


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@pytest.fixture
def source(tmp_path):
    """A miniature vault database: both vec0 tables, in their real shapes,
    one item chunked into several vectors sharing an `item_id` (the shape a
    comparison keyed on `item_id` alone would collapse — `migrate.py`'s own
    fixture states why), and the ordinary tables `store.db_uri`/`vectors.
    connect` still need to exist alongside them."""
    path = tmp_path / "library.db"
    conn = _conn(path)
    conn.execute("CREATE TABLE issue_items (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE signals (id TEXT PRIMARY KEY)")
    conn.execute(vectors.VEC_TABLE_DDL)
    conn.execute(vectors.SIGNAL_VEC_TABLE_DDL)
    conn.executemany(
        "INSERT INTO library_vectors(embedding, vault, item_id) VALUES (?,?,?)",
        [(_vec(i), "chart_house", f"item-{i}") for i in range(12)]
        + [(_vec(100 + c), "chart_house", "item-chunked") for c in range(3)])
    conn.executemany(
        "INSERT INTO signal_vectors(embedding, signal_id) VALUES (?,?)",
        [(_vec(i + 50), f"sig-{i}") for i in range(9)])
    conn.commit()
    conn.close()
    return path


# --- the guard ---------------------------------------------------------


def test_the_live_write_enabled_database_is_refused_as_a_source(
        tmp_path, monkeypatch, source):
    monkeypatch.setenv("LIBRARY_WRITES_ENABLED", "1")
    monkeypatch.setenv("LIBRARY_DB", str(source))
    with pytest.raises(split_vectors.RefusedError, match="IS the live LIBRARY_DB"):
        split_vectors.export_vectors(source, tmp_path / "vectors.db")


def test_writes_disabled_still_refuses_library_db_as_a_source(tmp_path, monkeypatch,
                                                               source):
    """`LIBRARY_DB` alone, with NO `LIBRARY_WRITES_ENABLED` exported at all, is
    the exact shape of the deploy runbook's own documented `split_vectors.py`
    invocation — a migration's shell has no reason to export a service-runtime
    flag — and that invocation must refuse just as hard as the write-enabled
    case (security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 1: the
    prior guard ran no comparison at all here, so this exact command ran
    unguarded against the live database)."""
    monkeypatch.delenv("LIBRARY_WRITES_ENABLED", raising=False)
    monkeypatch.setenv("LIBRARY_DB", str(source))
    with pytest.raises(split_vectors.RefusedError, match="IS the live LIBRARY_DB"):
        split_vectors.export_vectors(source, tmp_path / "vectors.db")


def test_i_mean_it_overrides_the_guard(tmp_path, monkeypatch, source):
    monkeypatch.setenv("LIBRARY_WRITES_ENABLED", "1")
    monkeypatch.setenv("LIBRARY_DB", str(source))
    split_vectors.export_vectors(source, tmp_path / "vectors.db", allow_live=True)


def test_a_running_service_is_refused_even_with_no_env_naming_the_source(
        tmp_path, monkeypatch, source):
    """The guard's second door: no `LIBRARY_DB` in the environment says
    anything about `source`, but the service is up anyway — the shape of an
    operator who skipped or raced "stop the service" before running the
    documented migration command."""
    monkeypatch.delenv("LIBRARY_DB", raising=False)
    monkeypatch.delenv("LIBRARY_WRITES_ENABLED", raising=False)
    monkeypatch.setattr(split_vectors, "_service_running",
                         lambda: (True, "it answered 200 at http://test/health"))
    with pytest.raises(split_vectors.RefusedError, match="could not confirm"):
        split_vectors.export_vectors(source, tmp_path / "vectors.db")


def test_i_mean_it_overrides_the_running_service_refusal_too(
        tmp_path, monkeypatch, source):
    monkeypatch.delenv("LIBRARY_DB", raising=False)
    monkeypatch.setattr(split_vectors, "_service_running",
                         lambda: (True, "it answered 200 at http://test/health"))
    split_vectors.export_vectors(source, tmp_path / "vectors.db", allow_live=True)


# --- _service_running: the real HTTP probe, not a mock --------------------
#
# Everything above monkeypatches `_service_running` to test `guard()`'s own
# logic in isolation. These hit the real implementation — round 1 shipped it
# with zero coverage of the actual `urlopen`/exception-handling path, which is
# exactly where the round-2 fail-open bug lived (reviewer + security-reviewer,
# issue_01M35R5GA1SKRG2CMQRF189NDE round 2).


def _bind_closed_port() -> int:
    """A port nothing is listening on: bind, learn the OS-assigned port, then
    close — the bind proves the port was free a moment ago, and closing
    leaves it refused rather than merely unused."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_service_running_reads_a_closed_port_as_not_running(monkeypatch):
    port = _bind_closed_port()
    monkeypatch.setattr(split_vectors, "_health_url",
                         lambda: f"http://127.0.0.1:{port}/health")
    running, detail = _real_service_running()
    assert running is False
    assert "refused" in detail


def test_service_running_reads_a_hanging_socket_as_refused(monkeypatch):
    """A socket that accepts the connection and never answers — the shape of
    a service that is up but slow to respond, the exact case the round-2
    finding named."""
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept_and_hang():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            import time
            time.sleep(2.0)
        finally:
            conn.close()

    t = threading.Thread(target=_accept_and_hang, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(split_vectors, "_health_url",
                             lambda: f"http://127.0.0.1:{port}/health")
        running, detail = _real_service_running()
        assert running is True
        assert detail
    finally:
        srv.close()
        t.join(timeout=3.0)


def _serve_one_status(status: int):
    """A real local HTTP server answering every request with `status`.
    Returns `(port, shutdown)`."""
    import http.server
    import socketserver
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib's own method name
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):  # silence per-request stderr noise
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        srv.shutdown()
        thread.join(timeout=3.0)
        srv.server_close()

    return port, shutdown


def test_service_running_reads_a_500_response_as_refused(monkeypatch):
    port, shutdown = _serve_one_status(500)
    try:
        monkeypatch.setattr(split_vectors, "_health_url",
                             lambda: f"http://127.0.0.1:{port}/health")
        running, detail = _real_service_running()
        assert running is True
        assert detail
    finally:
        shutdown()


def test_service_running_reads_a_200_response_as_refused(monkeypatch):
    port, shutdown = _serve_one_status(200)
    try:
        monkeypatch.setattr(split_vectors, "_health_url",
                             lambda: f"http://127.0.0.1:{port}/health")
        running, detail = _real_service_running()
        assert running is True
        assert "200" in detail
    finally:
        shutdown()


# --- export_vectors ------------------------------------------------------


def test_export_leaves_the_source_untouched(tmp_path, source):
    before = source.read_bytes()
    split_vectors.export_vectors(source, tmp_path / "vectors.db")
    assert source.read_bytes() == before


def test_export_copies_every_row(tmp_path, source):
    counts = split_vectors.export_vectors(source, tmp_path / "vectors.db")
    assert counts == {"library_vectors": 15, "signal_vectors": 9}


def test_export_refuses_an_existing_destination(tmp_path, source):
    dest = tmp_path / "vectors.db"
    dest.write_bytes(b"already here")
    with pytest.raises(FileExistsError):
        split_vectors.export_vectors(source, dest)


def test_export_refuses_a_source_with_neither_vec0_table(tmp_path):
    empty = tmp_path / "library.db"
    sqlite3.connect(empty).close()
    with pytest.raises(ValueError, match="already split, or never had them"):
        split_vectors.export_vectors(empty, tmp_path / "vectors.db")


def test_export_refuses_a_source_holding_only_one_vec0_table(tmp_path, source):
    """The shape a crash between `drop_and_vacuum`'s two `DROP TABLE`
    statements used to leave behind: one vec0 table gone, the other still
    present. Exporting whatever remains would silently move half the
    vectors and report success on the natural "just rerun it" recovery from
    that exact crash (reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 1)."""
    conn = _conn(source)
    conn.execute("DROP TABLE signal_vectors")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="a partial split"):
        split_vectors.export_vectors(source, tmp_path / "vectors.db")


# --- the acceptance test: search is identical across the split ----------


def test_semantic_search_is_identical_across_the_split(tmp_path, monkeypatch,
                                                        source):
    """The clause 7 done-when, directly: the same query against the source
    (vec0 still inside the vault file) and against the split vectors file
    (ATTACHed the way `vectors.connect` attaches it in production) returns
    the same ids in the same order at the same distances."""
    dest = tmp_path / "vectors.db"
    query = _vec(2)

    before_conn = _conn(source)
    try:
        before = vectors.nearest(before_conn, query, k=5, vault="chart_house")
    finally:
        before_conn.close()
    assert before  # not a vacuous pass

    split_vectors.export_vectors(source, dest)
    assert split_vectors.verify(source, dest) == []

    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", dest)
    after_conn = vectors.connect(source)
    try:
        after = vectors.nearest(after_conn, query, k=5, vault="chart_house")
    finally:
        after_conn.close()

    assert after == before


def test_signal_vectors_knn_is_identical_across_the_split(tmp_path, monkeypatch,
                                                           source):
    """`signal_vectors` has no `vault` column — `vectors.nearest` always
    queries `library_vectors` (the current code path for `GET
    /signals/search`, per `vectors.VEC_TABLE`) — so this queries it directly,
    the same query shape `split_vectors.verify`'s own KNN spot-check uses."""
    dest = tmp_path / "vectors.db"
    query = _vec(51)
    knn = ("SELECT signal_id, distance FROM signal_vectors "
           "WHERE embedding MATCH ? AND k = ? ORDER BY distance")

    before_conn = _conn(source)
    try:
        before = [tuple(r) for r in before_conn.execute(knn, (query, 5))]
    finally:
        before_conn.close()
    assert before  # not a vacuous pass

    split_vectors.export_vectors(source, dest)
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", dest)
    after_conn = vectors.connect(source)
    try:
        after = [tuple(r) for r in after_conn.execute(knn, (query, 5))]
    finally:
        after_conn.close()

    assert after == before


# --- verify --------------------------------------------------------------


def test_verify_catches_a_vector_that_did_not_survive(tmp_path, source):
    """Planted rather than trusting the good path works — `migrate.py`'s own
    tests take the same approach for the same reason: a shadow-table copy
    reproduces every count and answers a KNN with nonsense, so a check that
    never sees a broken migration proves nothing about catching one."""
    dest = tmp_path / "vectors.db"
    split_vectors.export_vectors(source, dest)

    conn = _conn(dest)
    conn.execute("UPDATE library_vectors SET item_id = 'corrupted' "
                 "WHERE rowid = 1")
    conn.commit()
    conn.close()

    problems = split_vectors.verify(source, dest)
    assert any("differ from the source" in p for p in problems)


def test_verify_catches_a_missing_row(tmp_path, source):
    dest = tmp_path / "vectors.db"
    split_vectors.export_vectors(source, dest)

    conn = _conn(dest)
    conn.execute("DELETE FROM signal_vectors WHERE rowid = 1")
    conn.commit()
    conn.close()

    problems = split_vectors.verify(source, dest)
    assert any("count mismatch signal_vectors" in p for p in problems)


# --- drop_and_vacuum -------------------------------------------------------


def test_drop_and_vacuum_removes_the_vec0_tables_and_shrinks_the_file(
        tmp_path, source):
    dest = tmp_path / "vectors.db"
    split_vectors.export_vectors(source, dest)
    assert split_vectors.verify(source, dest) == []

    before, after = split_vectors.drop_and_vacuum(source)
    assert after < before

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert not tables & {"library_vectors", "signal_vectors"}
    # The shadow tables vec0 owns are gone too, not merely the virtual table
    # itself — otherwise the split would leave orphaned storage behind.
    assert not any(t.startswith("library_vectors_") or
                   t.startswith("signal_vectors_") for t in tables)


def test_drop_and_vacuum_leaves_the_ordinary_tables_alone(tmp_path, source):
    dest = tmp_path / "vectors.db"
    split_vectors.export_vectors(source, dest)
    split_vectors.drop_and_vacuum(source)

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND "
            "name IN ('issue_items', 'signals')").fetchone()[0] == 2
    finally:
        conn.close()


def test_drop_and_vacuum_is_atomic_across_both_drops(tmp_path, source, monkeypatch):
    """A failure between the two `DROP TABLE` statements must roll back BOTH,
    not leave one gone and the other present — the crash window a prior
    version left open by letting each `DROP TABLE` commit itself independently
    (reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 1). A real crash cannot
    be forced from inside the same process, so this injects a failure between
    the two drops and confirms both tables survive — the tell that a prior
    version would fail this exact assertion, since its first `DROP TABLE` was
    already committed by the time the second one raised."""
    dest = tmp_path / "vectors.db"
    split_vectors.export_vectors(source, dest)
    assert split_vectors.verify(source, dest) == []

    class _BoomOnSecondDrop:
        """A thin proxy — `sqlite3.Connection` is a C type and refuses a
        direct `conn.execute = ...` override (`AttributeError: ... is
        read-only`), so this forwards everything except `execute` to the
        real connection underneath."""

        def __init__(self, real):
            object.__setattr__(self, "_real", real)
            object.__setattr__(self, "_drops", 0)

        def execute(self, sql, *args):
            if sql.startswith("DROP TABLE"):
                object.__setattr__(self, "_drops", self._drops + 1)
                if self._drops == 2:
                    raise sqlite3.OperationalError("simulated crash between drops")
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

    real_connect = split_vectors.connect
    monkeypatch.setattr(
        split_vectors, "connect",
        lambda path, *, readonly: _BoomOnSecondDrop(real_connect(path, readonly=readonly)))

    with pytest.raises(sqlite3.OperationalError, match="simulated crash"):
        split_vectors.drop_and_vacuum(source)

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"library_vectors", "signal_vectors"} <= tables


# --- main, end to end -----------------------------------------------------


def test_main_runs_the_whole_sequence_and_reports_pass(tmp_path, source, capsys):
    dest = tmp_path / "vectors.db"
    rc = split_vectors.main([str(source), str(dest)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert dest.is_file()

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "library_vectors" not in tables


def test_main_fails_and_leaves_the_source_alone_when_verify_finds_a_problem(
        tmp_path, source, monkeypatch):
    """If `verify` ever reported a problem, `main` must stop before
    `drop_and_vacuum` runs — simulated here since a real corruption is hard
    to plant between `export` and `verify` in one process."""
    dest = tmp_path / "vectors.db"
    monkeypatch.setattr(split_vectors, "verify", lambda *a, **k: ["planted problem"])
    before = source.read_bytes()

    rc = split_vectors.main([str(source), str(dest)])

    assert rc == 1
    assert source.read_bytes() == before
