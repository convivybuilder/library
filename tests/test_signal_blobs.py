"""A signal's optional `content`: written as a blob, read back by ref.

**The security property under test throughout is that `meta.blob_ref` is
server-assigned.** `GET /signals/{id}/blob` authorizes on the row it is
asked about, not on whatever row first wrote the blob — so a caller that
could set `blob_ref` directly could point its own (readable) row at a ref it
read off someone else's `meta` and read that content back through a weaker
gate. Refusing a caller-supplied `blob_ref` is what closes that, and most of
this file is proving it stays closed from every angle: creation, read scope,
and the customer-class wall.
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import blobs
import identity
import projects
import protocol
import store

AGENT = "agent-token-vivy"
OTHER = "agent-token-constance"
SYSTEM = "system-token"


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE cold_dedup_keys (
            dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        OTHER: {"name": "constance-agent", "products": ["constance"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))
    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"_meta": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _body(**over):
    out = {"source": "claude-code", "kind": "transcript-turn", "actor": "operator",
           "product": "vivy", "body": "a preview of the turn"}
    out.update(over)
    return out


# --- write: content becomes a blob, meta gets the ref ---------------------------


def test_content_is_stored_as_a_blob_and_referenced_in_meta(client):
    r = client.post("/signals", json=_body(content="the exact original text"),
                    headers=_h())
    assert r.status_code == 201
    out = r.json()
    ref = out["meta"]["blob_ref"]
    assert out["meta"]["blob_bytes"] == len("the exact original text".encode())
    assert blobs.read_blob(ref) == b"the exact original text"


def test_no_content_means_no_blob_ref(client):
    r = client.post("/signals", json=_body(), headers=_h())
    assert r.status_code == 201
    assert "blob_ref" not in r.json()["meta"]


def test_a_caller_supplied_blob_ref_is_refused(client):
    """The security boundary this whole file is about: `blob_ref` may only
    ever be assigned by the server from `content` in the same call."""
    r = client.post(
        "/signals",
        json=_body(meta={"blob_ref": "a" * 64}),
        headers=_h())
    assert r.status_code == 400
    assert "blob_ref" in r.json()["error"]


def test_non_string_content_is_refused(client):
    r = client.post("/signals", json=_body(content=12345), headers=_h())
    assert r.status_code == 400
    assert "content" in r.json()["error"]


def test_content_with_a_lone_surrogate_is_refused_cleanly(client):
    """A JSON `\\uXXXX` escape has no obligation to pair a surrogate — it
    decodes to a valid Python str that then cannot be encoded to UTF-8
    (reviewer, PR #40). Sent as raw wire bytes rather than through
    `json=`, which serializes client-side with Python's own `json.dumps`
    and fails there instead of exercising the route at all — the escape
    sequence below is plain ASCII on the wire, exactly what a real client
    would send, and only becomes an unpaired surrogate once the SERVER
    decodes it. This must refuse with a clean 400 rather than crash on the
    encode."""
    # Slice off exactly the closing brace, not every trailing one — `_body()`
    # is flat today, but `rstrip("}")` would silently misbuild this the day
    # it grows a nested object (reviewer, PR #40).
    body = json.dumps(_body())[:-1].encode() + b',"content":"\\ud800"}'
    r = client.post("/signals", content=body,
                    headers={**_h(), "Content-Type": "application/json"})
    assert r.status_code == 400
    assert "content" in r.json()["error"]


def test_oversized_content_is_refused(client, monkeypatch):
    monkeypatch.setattr(store, "MAX_BLOB_BYTES", 10)
    r = client.post("/signals", json=_body(content="x" * 11), headers=_h())
    assert r.status_code == 400
    assert "content" in r.json()["error"]


def test_writes_disabled_refuses_the_blob_write_the_same_way(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post("/signals", json=_body(content="anything"), headers=_h())
    assert r.status_code == 503
    assert "write path" in r.json()["error"]


def test_resending_the_same_content_is_idempotent_at_the_blob_layer(client):
    """A resumed capture retrying a turn it already sent must not fail or
    double-write — content-addressing makes the second write a no-op."""
    r1 = client.post("/signals", json=_body(content="same text",
                                            dedup_key="k1"), headers=_h())
    r2 = client.post("/signals", json=_body(content="same text",
                                            dedup_key="k2"), headers=_h())
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["meta"]["blob_ref"] == r2.json()["meta"]["blob_ref"]


# --- read: GET /signals/{id}/blob ------------------------------------------------


def test_the_blob_reads_back_exactly(client):
    created = client.post(
        "/signals", json=_body(content="line one\nline two"),
        headers=_h()).json()
    r = client.get(f"/signals/{created['id']}/blob", headers=_h())
    assert r.status_code == 200
    assert r.text == "line one\nline two"


def test_a_signal_with_no_blob_refuses_cleanly(client):
    created = client.post("/signals", json=_body(), headers=_h()).json()
    r = client.get(f"/signals/{created['id']}/blob", headers=_h())
    assert r.status_code == 404
    assert "no blob" in r.json()["error"]


def test_a_missing_signal_id_is_not_found(client):
    r = client.get("/signals/sig_nope/blob", headers=_h())
    assert r.status_code == 404


def test_the_blob_route_refuses_any_parameter(client):
    created = client.post(
        "/signals", json=_body(content="line one"), headers=_h()).json()
    r = client.get(f"/signals/{created['id']}/blob?raw=true", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["raw"]
    assert r.json()["supported"] == []


def test_the_blob_route_respects_product_scope(client):
    """The read authorizes on the ROW, so a caller out of the row's product
    scope is refused even though the blob file itself exists and is
    perfectly readable by ref."""
    created = client.post("/signals", json=_body(content="vivy's own text"),
                          headers=_h(AGENT)).json()
    r = client.get(f"/signals/{created['id']}/blob", headers=_h(OTHER))
    assert r.status_code == 404


def test_the_blob_route_respects_the_customer_class_wall(client):
    # Only `system` holds `signal.create_customer`, so the row has to be
    # created under that identity.
    created = client.post(
        "/signals",
        json=_body(content="customer text", privacy_class="customer"),
        headers=_h(SYSTEM)).json()
    # AGENT holds no `signal.customer` capability, so even a product-scoped
    # reader is refused — matching GET /signals/{id}'s own wall.
    r = client.get(f"/signals/{created['id']}/blob", headers=_h())
    assert r.status_code == 404
    # SYSTEM holds both capabilities and reads it back fine.
    r2 = client.get(f"/signals/{created['id']}/blob", headers=_h(SYSTEM))
    assert r2.status_code == 200
    assert r2.text == "customer text"


# --- blobs.py itself --------------------------------------------------------------


def test_write_blob_is_content_addressed(tmp_path, monkeypatch):
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.db")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    ref1 = blobs.write_blob(b"hello")
    ref2 = blobs.write_blob(b"hello")
    assert ref1 == ref2
    assert blobs.read_blob(ref1) == b"hello"


def test_write_blob_chmods_the_file_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.db")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    ref = blobs.write_blob(b"read-only")
    mode = blobs._path(ref).stat().st_mode
    assert oct(mode)[-3:] == "444"


def test_write_blob_refuses_when_it_would_leave_too_little_free(
        tmp_path, monkeypatch):
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.db")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(blobs, "MIN_FREE_BYTES", 10**18)  # nothing has this much free
    with pytest.raises(blobs.StorageFull):
        blobs.write_blob(b"anything")


def test_write_blob_does_not_check_free_space_for_content_already_stored(
        tmp_path, monkeypatch):
    """The idempotent short-circuit needs no new disk space, so it must not
    be blocked by a full filesystem for content that is already there."""
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.db")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    ref = blobs.write_blob(b"already here")
    monkeypatch.setattr(blobs, "MIN_FREE_BYTES", 10**18)
    assert blobs.write_blob(b"already here") == ref


def test_write_blob_refuses_while_writes_are_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        blobs.write_blob(b"anything")


def test_read_blob_refuses_a_malformed_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    with pytest.raises(blobs.InvalidRef):
        blobs.read_blob("../../etc/passwd")
    with pytest.raises(blobs.InvalidRef):
        blobs.read_blob("not-hex")
    with pytest.raises(blobs.InvalidRef):
        blobs.read_blob("a" * 63)  # one short
    with pytest.raises(blobs.InvalidRef):
        # `$` matches before a trailing newline; `\Z` does not (security-
        # reviewer, PR #40).
        blobs.read_blob("0" * 64 + "\n")


def test_read_blob_raises_file_not_found_for_a_well_formed_absent_ref(
        tmp_path, monkeypatch):
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    with pytest.raises(FileNotFoundError):
        blobs.read_blob("0" * 64)


# --- quarantine: the two-phase delete `discard_sweep.py` drives -------------


@pytest.fixture
def blob_store(tmp_path, monkeypatch):
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.db")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)


def test_quarantine_blob_moves_the_file_out_of_its_content_addressed_path(
        blob_store):
    ref = blobs.write_blob(b"quarantine me")
    assert blobs.quarantine_blob(ref) is True
    assert not blobs._path(ref).is_file()
    with pytest.raises(FileNotFoundError):
        blobs.read_blob(ref)
    assert blobs._quarantine_path(ref).read_bytes() == b"quarantine me"


def test_quarantine_blob_reports_false_for_a_ref_with_nothing_there(blob_store):
    assert blobs.quarantine_blob("0" * 64) is False


def test_a_write_after_quarantine_writes_a_fresh_copy_without_colliding(
        blob_store):
    """The race `discard_sweep.py`'s module docstring names: a concurrent
    `write_blob` for the same content, arriving after the quarantine rename,
    must find nothing at `ref` and write a fresh copy rather than raising or
    silently doing nothing."""
    ref = blobs.write_blob(b"resend me")
    blobs.quarantine_blob(ref)
    assert blobs.write_blob(b"resend me") == ref
    assert blobs.read_blob(ref) == b"resend me"
    # the quarantined duplicate is untouched by the fresh write
    assert blobs._quarantine_path(ref).read_bytes() == b"resend me"


def test_restore_blob_moves_it_back_when_nothing_is_at_the_ref_path(
        blob_store):
    ref = blobs.write_blob(b"restore me")
    blobs.quarantine_blob(ref)
    assert blobs.restore_blob(ref) is True
    assert blobs.read_blob(ref) == b"restore me"
    assert not blobs._quarantine_path(ref).is_file()


def test_restore_blob_drops_the_quarantined_copy_when_a_fresh_one_already_landed(
        blob_store):
    ref = blobs.write_blob(b"raced")
    blobs.quarantine_blob(ref)
    blobs.write_blob(b"raced")  # a fresh copy lands at `ref` while quarantined
    assert blobs.restore_blob(ref) is False
    assert blobs.read_blob(ref) == b"raced"
    assert not blobs._quarantine_path(ref).is_file()


def test_restore_blob_reports_false_for_nothing_quarantined(blob_store):
    assert blobs.restore_blob("0" * 64) is False


def test_discard_quarantined_unlinks_the_quarantine_file(blob_store):
    ref = blobs.write_blob(b"gone for good")
    blobs.quarantine_blob(ref)
    assert blobs.discard_quarantined(ref) is True
    assert not blobs._quarantine_path(ref).is_file()
    with pytest.raises(FileNotFoundError):
        blobs.read_blob(ref)


def test_discard_quarantined_reports_false_for_nothing_quarantined(blob_store):
    assert blobs.discard_quarantined("0" * 64) is False


def test_quarantined_refs_finds_every_quarantined_ref_with_its_mtime(
        blob_store):
    ref_a = blobs.write_blob(b"one")
    ref_b = blobs.write_blob(b"two")
    blobs.quarantine_blob(ref_a)
    blobs.quarantine_blob(ref_b)

    found = dict(blobs.quarantined_refs())
    assert set(found) == {ref_a, ref_b}
    assert all(isinstance(mtime, float) for mtime in found.values())


def test_quarantined_refs_is_empty_with_nothing_quarantined(blob_store):
    blobs.write_blob(b"never quarantined")
    assert blobs.quarantined_refs() == []


def test_quarantine_operations_refuse_while_writes_are_disabled(
        blob_store, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        blobs.quarantine_blob("0" * 64)
    with pytest.raises(store.WritesDisabled):
        blobs.restore_blob("0" * 64)
    with pytest.raises(store.WritesDisabled):
        blobs.discard_quarantined("0" * 64)


# --- resolve_blob_dir: the startup refusal (memory-retention epic clause 8,
# issue_01M35R5KXQJGWPRFC9BGEZAYQE) ------------------------------------------


def test_unset_library_blob_dir_refuses():
    with pytest.raises(blobs.MisconfiguredBlobStore, match="LIBRARY_BLOB_DIR is not set"):
        blobs.resolve_blob_dir(env={})


def test_a_missing_path_refuses(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(blobs.MisconfiguredBlobStore, match=re.escape(str(missing))):
        blobs.resolve_blob_dir(env={"LIBRARY_BLOB_DIR": str(missing)})


def test_a_path_that_is_a_file_refuses(tmp_path):
    a_file = tmp_path / "not-a-directory"
    a_file.write_text("")
    with pytest.raises(blobs.MisconfiguredBlobStore, match="not a directory"):
        blobs.resolve_blob_dir(env={"LIBRARY_BLOB_DIR": str(a_file)})


def test_an_unwritable_directory_refuses(tmp_path):
    unwritable = tmp_path / "read-only"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        with pytest.raises(blobs.MisconfiguredBlobStore, match="not writable"):
            blobs.resolve_blob_dir(env={"LIBRARY_BLOB_DIR": str(unwritable)})
    finally:
        # Restored so pytest's own tmp_path cleanup can remove it afterwards.
        unwritable.chmod(0o700)


def test_a_valid_directory_resolves(tmp_path):
    root = tmp_path / "blobs"
    root.mkdir()
    assert blobs.resolve_blob_dir(env={"LIBRARY_BLOB_DIR": str(root)}) == root


def test_a_write_under_a_valid_root_still_creates_its_subdirectories(
        tmp_path, monkeypatch):
    """The root itself is never created by this service (checked above); the
    two-character shard directory under an already-existing root still is —
    unchanged from before this refusal existed."""
    root = tmp_path / "blobs"
    root.mkdir()
    monkeypatch.setattr(blobs, "BLOB_DIR", root)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.db")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    ref = blobs.write_blob(b"shard me")
    assert (root / ref[:2] / ref).is_file()
