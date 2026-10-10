"""Checkout/check-in for a Reference Room document (`issue_01M0DVAH9KAACHH0RRWQZ3KZTE`).

The document itself never moves through these routes — it stays a file in the
`knowledge` repository (ADR-0022) and is asserted here only as a row in the
read-only knowledge index. What these tests hold is the lock: a document can
be checked out, a second concurrent checkout is refused (naming the holder and
expiry), a dead checkout is reclaimed rather than blocking, and check-in is
scoped to the holder — the same shape `/items/{id}/checkout` and `/release`
already hold for a work item's lease, applied to a KB document instead.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import reference
import store

AGENT = "agent-token-vivy"
SYSTEM = "system-token"

DOC_ID = "platform/decisions/0022-library-repo"


def _hash_bodies(iconn):
    """`body_hash` for every fixture row, as `sha256(body)`.

    **Derived from the body rather than written beside it**, matching
    `lib/knowledge/indexer.py`, which hashes the body it is about to store. A
    fixture that carried a hand-written hash could assert a value that was
    never a digest of anything, and the test asserting `body_sha256` would
    pass against a constant.
    """
    for doc_id, body in iconn.execute("SELECT id, body FROM docs").fetchall():
        iconn.execute("UPDATE docs SET body_hash = ? WHERE id = ?",
                      (hashlib.sha256((body or "").encode()).hexdigest(), doc_id))


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE reference_checkouts (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, holder TEXT NOT NULL,
            run_id TEXT, taken_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
            released_at TEXT, release_reason TEXT, released_by TEXT);
        CREATE UNIQUE INDEX ix_reference_checkouts_live_doc
            ON reference_checkouts (doc_id) WHERE released_at IS NULL;
    """)
    conn.commit()
    conn.close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
    """)
    iconn.execute(
        "INSERT INTO docs (id, source_ref, kind, project, title, status, "
        "topics, audience, body, last_reviewed, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (DOC_ID, "platform/decisions/0022.md", "decision", "platform",
         "The Library gets its own repository", "current", "[]", "[]",
         "the Library is a service with its own storage",
         "2026-08-01", "2026-08-02"))
    _hash_bodies(iconn)
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _checkout(client, doc_id=DOC_ID, token=AGENT, **body):
    return client.post(f"/reference/documents/{doc_id}/checkout", json=body,
                       headers=_h(token))


def _checkin(client, doc_id=DOC_ID, token=AGENT, **body):
    return client.post(f"/reference/documents/{doc_id}/checkin", json=body,
                       headers=_h(token))


def _rows():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM reference_checkouts ORDER BY taken_at").fetchall()
    finally:
        conn.close()


# --- checkout: the lock -------------------------------------------------------


def test_a_document_can_be_checked_out(client):
    r = _checkout(client)
    assert r.status_code == 200
    body = r.json()["checkout"]
    assert body["doc_id"] == DOC_ID
    # `holder` FALLS BACK TO THE TOKEN'S NAME, not the bearer string —
    # `_claim_owner`'s own contract, exercised here rather than assumed.
    assert body["holder"] == "vivy-agent"
    assert body["released_at"] is None
    assert "policy" in r.json()


def test_checking_out_a_document_the_index_does_not_know_404s(client):
    r = _checkout(client, doc_id="platform/reference/nope")
    assert r.status_code == 404


def test_a_second_concurrent_checkout_is_refused(client):
    first = _checkout(client, owner="holder-a")
    assert first.status_code == 200
    second = _checkout(client, owner="holder-b")
    assert second.status_code == 409
    assert "holder-a" in second.json()["error"]
    assert second.json()["checkout"]["holder"] == "holder-a"


def test_the_same_holder_re_checking_out_is_still_refused(client):
    """A live checkout blocks even its own holder — re-entrant checkout is not
    the same thing as a heartbeat, and this route has no heartbeat verb."""
    _checkout(client, owner="holder-a")
    r = _checkout(client, owner="holder-a")
    assert r.status_code == 409


def test_a_dead_checkout_is_reclaimed_rather_than_blocking(client, monkeypatch):
    _checkout(client, owner="holder-a")
    stale = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(
        hours=5)
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE reference_checkouts SET taken_at = ?, heartbeat_at = ?",
                 (stale.isoformat(" "), stale.isoformat(" ")))
    conn.commit()
    conn.close()
    r = _checkout(client, owner="holder-b")
    assert r.status_code == 200
    assert r.json()["checkout"]["holder"] == "holder-b"
    rows = _rows()
    assert len(rows) == 2
    assert rows[0]["release_reason"] == "reclaimed"
    # STAMPED WITH THE VERIFIED ACTOR, NOT THE CALLER-DECLARED `holder`. The
    # reclaiming request's owner is "holder-b", a self-chosen label; the audit
    # column must say who actually broke the dead lock, which is the token
    # identity behind it (`vivy-agent`), not that label.
    assert rows[0]["released_by"] == "vivy-agent"
    assert rows[0]["released_by"] != "holder-b"


def test_force_is_refused_on_checkout(client):
    r = _checkout(client, force=True)
    assert r.status_code == 403
    assert "human" in r.json()["error"]
    assert _rows() == []


def test_writes_disabled_refuses_the_same_way(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = _checkout(client)
    assert r.status_code == 503


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_agent_and_system_can_checkout(client, token):
    assert _checkout(client, token=token).status_code == 200


def test_unauthenticated_checkout_is_refused(client):
    r = client.post(f"/reference/documents/{DOC_ID}/checkout", json={},
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_a_missing_capability_is_403_naming_the_capability(client, monkeypatch):
    """Matches `test_course_writes.py`'s convention for reaching a capability
    gate every minted role otherwise carries."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"reference.checkout"}})
    r = _checkout(client)
    assert r.status_code == 403
    assert "reference.checkout" in r.json()["error"]


def test_a_losing_concurrent_checkout_gets_a_clean_conflict_not_a_500(client):
    """The race `reference_checkout.take` guards with `BEGIN IMMEDIATE`: two
    callers scanning the same dead-or-absent `doc_id` at once must not both
    reach the insert. Reproduced with two real threads hitting the same
    on-disk database (the `client` fixture's db is a file, not `:memory:`,
    which is what makes two connections meaningfully concurrent here) rather
    than asserted from the single-threaded happy path alone."""
    import threading

    results = []

    def go(owner):
        r = _checkout(client, owner=owner)
        results.append((owner, r.status_code, r.json()))

    threads = [threading.Thread(target=go, args=(f"holder-{i}",))
              for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(status for _, status, _ in results)
    assert statuses == [200] + [409] * 7, (
        "every loser must get a clean 409, never a 500")
    winners = [owner for owner, status, _ in results if status == 200]
    assert len(winners) == 1
    for owner, status, body in results:
        if status == 409:
            assert body["checkout"]["holder"] == winners[0]

    rows = _rows()
    assert len(rows) == 1, "exactly one row must have been inserted"


# --- check-in: scoped to the holder --------------------------------------------


def test_checking_in_what_you_hold_releases_it(client):
    _checkout(client, owner="holder-a")
    r = _checkin(client, owner="holder-a")
    assert r.status_code == 200
    out = r.json()["checkout"]
    assert out["released_at"] is not None
    assert out["release_reason"] == "released"
    # STAMPED WITH THE VERIFIED ACTOR, NOT THE CALLER-DECLARED `holder` — the
    # same reasoning `take`'s reclaim uses, applied to the ordinary check-in
    # path too (security-reviewer, PR #45, round 2).
    assert out["released_by"] == "vivy-agent"
    assert out["released_by"] != "holder-a"

    again = _checkout(client, owner="holder-b")
    assert again.status_code == 200


def test_checking_in_a_document_nobody_holds_is_refused(client):
    r = _checkin(client, owner="holder-a")
    assert r.status_code == 409
    assert "not checked out" in r.json()["error"]


def test_checking_in_someone_elses_checkout_is_refused(client):
    _checkout(client, owner="holder-a")
    r = _checkin(client, owner="holder-b")
    assert r.status_code == 409
    assert "holder-a" in r.json()["error"]
    # STILL LIVE — the refused check-in must not have released it.
    second = _checkout(client, owner="holder-c")
    assert second.status_code == 409


def test_force_is_refused_on_checkin(client):
    _checkout(client, owner="holder-a")
    r = _checkin(client, owner="holder-b", force=True)
    assert r.status_code == 403
    assert "human" in r.json()["error"]
