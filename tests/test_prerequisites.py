"""Work item prerequisites (the operator, 2026-09-10, the Deck session that chose
prerequisites over `parked`): a `prerequisites` field on every item, a read-time `blocked` flag
derived from it, and a blocked item excluded from `claimable` and refused at
checkout and claim.

**`blocked` is judged on a prerequisite's TRUE state, never on what the
reader is shown.** An out-of-scope prerequisite renders `missing` — the same
shape a Course entry outside scope already gets — but still counts toward
`blocked`, because redacting a prerequisite's identity must not also redact
the fact that it is unresolved.

Prerequisites replaced `parked`, `/park`, `/unpark` and `ITEM_HELD`, which
are now retired; `test_transitions.py` holds that the routes are gone.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import lease
import protocol
import store

AGENT = "agent-token-vivy"
WIDE = "agent-token-wide"
SYSTEM = "system-token"
APPROVED = "approved-client-token"
NARROW_APPROVED = "narrow-approved-client-token"


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index, ledger_tables):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_by_crew TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT, short_description TEXT);
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            from_state TEXT, to_state TEXT, at TEXT, note TEXT);
        CREATE TABLE issue_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            kind TEXT, body TEXT, run_id TEXT, at TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT, blocking_taken_for TEXT);
        CREATE TABLE call_slips (
            id TEXT PRIMARY KEY, item_id TEXT, product TEXT, claim_owner_label TEXT,
            issued_at TEXT, expires_at TEXT, grant_json TEXT);
        CREATE TABLE courses (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, entries TEXT,
            waypoints TEXT DEFAULT '[]',
            status TEXT, version INTEGER, created_by TEXT, created_at TEXT,
            updated_by TEXT, updated_at TEXT);
        CREATE UNIQUE INDEX ix_courses_live_product ON courses (product)
            WHERE status = 'live';
        CREATE TABLE course_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, course_id TEXT, actor TEXT,
            action TEXT, entries TEXT, waypoints TEXT, note TEXT, at TEXT);
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE corrections_queues (
            id TEXT PRIMARY KEY, product TEXT UNIQUE, created_at TEXT);
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        APPROVED: {"name": "orient", "products": ["*"]},
        NARROW_APPROVED: {"name": "orient-narrow", "products": ["vivy"]},
    }))
    vector_index(db)
    ledger_tables(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def seed(item_id, **cols):
    """One item, with only the columns a case cares about."""
    cols.setdefault("product", "vivy")
    cols.setdefault("title", item_id)
    cols.setdefault("type", None)
    cols.setdefault("state", "open")
    cols.setdefault("needs", "agent")
    cols.setdefault("created_by_kind", "agent")
    cols.setdefault("created_at", "2026-08-01 12:00:00.000000")
    conn = sqlite3.connect(store.DB_PATH)
    names = ", ".join(["id", *cols])
    conn.execute(f"INSERT INTO issue_items ({names}) "
                 f"VALUES ({','.join('?' * (len(cols) + 1))})",
                 [item_id, *cols.values()])
    conn.commit()
    conn.close()


def seed_course(course_id, product, entries, **cols):
    cols.setdefault("title", course_id)
    cols.setdefault("status", "live")
    cols.setdefault("version", 1)
    cols.setdefault("created_at", "2026-08-01 12:00:00.000000")
    conn = sqlite3.connect(store.DB_PATH)
    names = ", ".join(["id", "product", "entries", *cols])
    conn.execute(f"INSERT INTO courses ({names}) "
                 f"VALUES ({','.join('?' * (len(cols) + 3))})",
                 [course_id, product, json.dumps(entries), *cols.values()])
    conn.commit()
    conn.close()


def listed(client, query, token=AGENT) -> set[str]:
    r = client.get(f"/items?{query}", headers=_h(token))
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


def get_item(client, item_id, token=AGENT):
    return client.get(f"/items/{item_id}", headers=_h(token))


def correct(client, item_id, body, token=AGENT):
    return client.post(f"/items/{item_id}/correct", json=body, headers=_h(token))


def verdict(client, item_id, text, met, token=APPROVED):
    return client.post(f"/items/{item_id}/prerequisites/verdict",
                       json={"text": text, "met": met}, headers=_h(token))


def resolve(client, item_id, token=WIDE, resolution="fixed"):
    return client.post(f"/items/{item_id}/resolve",
                       json={"resolution": resolution}, headers=_h(token))


def checkout(client, item_id, owner="session-1", token=AGENT):
    return client.post(f"/items/{item_id}/checkout", json={"owner": owner},
                       headers=_h(token))


def claim(client, item_id, owner="session-1", token=AGENT):
    return client.post(f"/items/{item_id}/claim", json={"owner": owner},
                       headers=_h(token))


# --- served on every item, and round-trips through all three read routes -------


def test_prerequisites_and_blocked_round_trip_on_get_items(client):
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    r = client.get("/items", headers=_h(WIDE))
    assert r.status_code == 200
    by_id = {i["id"]: i for i in r.json()["items"]}
    assert by_id["blocked_a"]["prerequisites"] == [
        {"item": "prereq_a", "state": "open", "title": "prereq_a"}]
    assert by_id["blocked_a"]["blocked"] is True
    assert by_id["prereq_a"]["prerequisites"] == []
    assert by_id["prereq_a"]["blocked"] is False


def test_prerequisites_and_blocked_round_trip_on_get_item(client):
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    r = get_item(client, "blocked_a", WIDE)
    assert r.status_code == 200
    body = r.json()
    assert body["prerequisites"] == [
        {"item": "prereq_a", "state": "open", "title": "prereq_a"}]
    assert body["blocked"] is True


def test_prerequisites_and_blocked_round_trip_on_get_course(client):
    seed("prereq_a")
    seed("epic_a", type="epic",
        prerequisites=json.dumps([{"item": "prereq_a"}]))
    seed_course("course_v", "vivy", ["epic_a"])
    r = client.get("/courses/course_v", headers=_h(WIDE))
    assert r.status_code == 200
    items = {i["id"]: i for i in r.json()["items"]}
    assert items["epic_a"]["prerequisites"] == [
        {"item": "prereq_a", "state": "open", "title": "prereq_a"}]
    assert items["epic_a"]["blocked"] is True


def test_a_text_prerequisite_round_trips_unmet(client):
    seed("blocked_text", prerequisites=json.dumps(
        [{"text": "the migration ran", "met": False, "judged_by": None,
          "judged_at": None}]))
    r = get_item(client, "blocked_text", WIDE)
    body = r.json()
    assert body["prerequisites"] == [
        {"text": "the migration ran", "met": False, "judged_by": None,
         "judged_at": None}]
    assert body["blocked"] is True


# --- blocked clears when the item prerequisite resolves -------------------------


def test_blocked_from_an_unresolved_item_prerequisite_clears_when_resolved(client):
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    assert get_item(client, "blocked_a", WIDE).json()["blocked"] is True

    r = resolve(client, "prereq_a")
    assert r.status_code == 200, r.text

    body = get_item(client, "blocked_a", WIDE).json()
    assert body["blocked"] is False
    assert body["prerequisites"] == [
        {"item": "prereq_a", "state": "fixed", "title": "prereq_a"}]


# --- blocked clears when a text prerequisite is judged met ----------------------


def test_blocked_from_an_unmet_text_clears_by_verdict(client):
    seed("blocked_text", prerequisites=json.dumps(
        [{"text": "the migration ran", "met": False, "judged_by": None,
          "judged_at": None}]))
    assert get_item(client, "blocked_text", WIDE).json()["blocked"] is True

    r = verdict(client, "blocked_text", "the migration ran", True)
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] is False
    entry = r.json()["prerequisites"][0]
    assert entry["met"] is True
    assert entry["judged_by"] == "orient"
    assert entry["judged_at"] is not None

    body = get_item(client, "blocked_text", WIDE).json()
    assert body["blocked"] is False
    assert body["prerequisites"][0]["met"] is True


# --- an out-of-scope prerequisite renders `missing` and still blocks -----------


def test_an_out_of_scope_prerequisite_renders_missing_and_still_blocks(client):
    seed("other_prereq", product="constance")
    seed("scoped_blocker", prerequisites=json.dumps([{"item": "other_prereq"}]))

    narrow = get_item(client, "scoped_blocker", AGENT).json()
    assert narrow["prerequisites"] == [{"item": "other_prereq", "missing": True}]
    assert narrow["blocked"] is True

    wide = get_item(client, "scoped_blocker", WIDE).json()
    assert wide["prerequisites"] == [
        {"item": "other_prereq", "state": "open", "title": "other_prereq"}]
    assert wide["blocked"] is True


def test_a_missing_prerequisite_item_renders_missing_and_blocks(client):
    """An id naming no row at all — not merely one out of scope — is the same
    disclosure-and-block shape: a dangling reference is not silently
    satisfied."""
    seed("dangling_blocker",
        prerequisites=json.dumps([{"item": "issue_does_not_exist"}]))
    body = get_item(client, "dangling_blocker", WIDE).json()
    assert body["prerequisites"] == [
        {"item": "issue_does_not_exist", "missing": True}]
    assert body["blocked"] is True


# --- the claimable listing excludes a blocked item, and its count is right -----


def test_claimable_listing_excludes_blocked_items_and_the_count_is_right(client):
    seed("open_1")
    seed("prereq_a")
    seed("blocked_by_item", prerequisites=json.dumps([{"item": "prereq_a"}]))
    seed("blocked_by_text", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    assert listed(client, "state=claimable", WIDE) == {"open_1", "prereq_a"}
    r = client.get("/items?state=claimable", headers=_h(WIDE))
    assert r.json()["total"] == 2
    assert r.json()["count"] == 2


def test_claimable_listing_includes_an_item_once_its_prerequisite_resolves(client):
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    assert listed(client, "state=claimable", WIDE) == {"prereq_a"}
    resolve(client, "prereq_a")
    assert listed(client, "state=claimable", WIDE) == {"blocked_a"}


# --- a blocked item is checked out without being claimed; a claim refuses -------


def _live_leases():
    conn = sqlite3.connect(store.DB_PATH)
    rows = conn.execute("SELECT taken_for, holder FROM issue_leases "
                        "WHERE released_at IS NULL").fetchall()
    conn.close()
    return set(rows)


def test_checkout_takes_the_lease_on_a_blocked_item_without_claiming_it(client):
    """The operator, 2026-09-12 (platform/decisions/0079): the owning crew's co has to be
    able to hold its own blocked item to judge and correct it, and the claim
    stays refused, so the lease is taken and the item is left `open`."""
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    r = checkout(client, "blocked_a")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claimed"] is False
    assert body["unmet_count"] == 1
    assert body["item"]["state"] == "open"
    assert body["item"]["blocked"] is True
    assert body["item"]["claimed_by"] is None
    assert body["lease"]["holder"] == "session-1"
    # No dispatch: no call slip minted, no briefing computed.
    assert "slip_id" not in body and "adjacency" not in body
    assert _live_leases() == {("blocked_a", "session-1")}
    assert get_item(client, "blocked_a", WIDE).json()["state"] == "open"
    assert "blocked_a" not in listed(client, "state=claimable", WIDE)


def test_the_holder_of_a_blocked_checkout_can_correct_the_item(client):
    """What the lease is for: the write platform/decisions/0076 puts under it.
    Anyone else is refused as `held`, the same as under any checkout."""
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    assert checkout(client, "blocked_a").status_code == 200
    mine = correct(client, "blocked_a", {"owner": "session-1", "severity": "high"})
    assert mine.status_code == 200, mine.text
    theirs = correct(client, "blocked_a", {"owner": "session-2", "severity": "low"})
    assert theirs.status_code == 409, theirs.text
    assert theirs.json()["reason"] == "held"


def test_a_blocked_checkout_is_released_and_the_item_stays_open(client):
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    assert checkout(client, "blocked_a").status_code == 200
    r = client.post("/items/blocked_a/release", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["lease_released"] is not None
    assert r.json()["state"] == "open"
    assert _live_leases() == set()
    # The fence is really gone: another session can now take its own hold.
    assert checkout(client, "blocked_a", owner="session-2").status_code == 200


def test_the_holder_claims_by_checking_out_again_once_unblocked(client):
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    assert checkout(client, "blocked_a").json()["claimed"] is False
    resolve(client, "prereq_a")
    r = checkout(client, "blocked_a")
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is True
    assert r.json()["item"]["state"] == "claimed"
    assert r.json()["item"]["claimed_by"] == "session-1"


def test_an_expired_claim_on_a_blocked_item_is_handed_back_not_passed_on(client):
    """A blocked item whose claim has expired: the new holder's checkout takes
    the lease and hands the dead claim back, so the item is `open` and the new
    holder can release what it holds. Passing `claimed_by` on to the dead holder
    left a lease `/release` refused as "claim held by" someone else (reviewer,
    PR #113). The hand-back is recorded as a `claimed -> open` event."""
    import datetime as dt

    def ago(hours):
        return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                - dt.timedelta(hours=hours)).isoformat(" ")

    seed("prereq_a")
    seed("blocked_a")
    assert checkout(client, "blocked_a").json()["claimed"] is True
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE issue_leases SET taken_at = ?, heartbeat_at = ?",
                 (ago(9), ago(9)))
    conn.execute("UPDATE issue_items SET claimed_at = ?, prerequisites = ? "
                 "WHERE id = 'blocked_a'",
                 (ago(30), json.dumps([{"item": "prereq_a"}])))
    conn.commit()
    conn.close()

    r = checkout(client, "blocked_a", owner="session-2")
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is False
    assert r.json()["item"]["state"] == "open"
    assert r.json()["item"]["claimed_by"] is None
    assert _live_leases() == {("blocked_a", "session-2")}
    conn = sqlite3.connect(store.DB_PATH)
    last = conn.execute(
        "SELECT from_state, to_state, note FROM issue_events "
        "WHERE item_id = 'blocked_a' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert last[:2] == ("claimed", "open")
    assert "expired claim by session-1 handed back" in last[2]

    released = client.post("/items/blocked_a/release",
                           json={"owner": "session-2"}, headers=_h())
    assert released.status_code == 200, released.text
    assert _live_leases() == set()


def test_claim_refuses_a_blocked_item_with_409(client):
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    r = claim(client, "blocked_a")
    assert r.status_code == 409, r.text
    assert "unmet prerequisites" in r.json()["error"]


def test_checkout_succeeds_once_the_prerequisite_resolves(client):
    seed("prereq_a")
    seed("blocked_a", prerequisites=json.dumps([{"item": "prereq_a"}]))
    resolve(client, "prereq_a")
    r = checkout(client, "blocked_a")
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is True


def test_an_out_of_scope_blocking_prerequisite_is_counted_and_never_named(client):
    """Neither answer discloses an out-of-scope prerequisite beyond what the
    rendered field already shows. The claim refusal names only a count. The
    lease-only checkout carries the item, whose `prerequisites` must be exactly
    what `GET /items/{id}` renders for the same caller (the id, marked
    `missing`) and nothing of the prerequisite itself."""
    seed("other_prereq", product="constance", severity="high")
    seed("scoped_blocker", prerequisites=json.dumps([{"item": "other_prereq"}]))
    refused = claim(client, "scoped_blocker", owner="session-2")
    assert refused.status_code == 409, refused.text
    assert refused.json()["unmet_count"] == 1
    assert "other_prereq" not in json.dumps(refused.json())
    held = checkout(client, "scoped_blocker")
    assert held.status_code == 200, held.text
    assert held.json()["claimed"] is False
    assert held.json()["unmet_count"] == 1
    rendered = get_item(client, "scoped_blocker").json()["prerequisites"]
    assert held.json()["item"]["prerequisites"] == rendered == [
        {"item": "other_prereq", "missing": True}]
    assert "constance" not in json.dumps(held.json())


# --- /correct: writing prerequisites and its validation -------------------------


def test_correct_sets_prerequisites_and_it_reads_back(client):
    seed("prereq_a")
    seed("plain")
    r = correct(client, "plain", {"prerequisites": [{"item": "prereq_a"}]})
    assert r.status_code == 200, r.text
    assert r.json()["prerequisites"] == [
        {"item": "prereq_a", "state": "open", "title": "prereq_a"}]
    assert r.json()["blocked"] is True


def test_correct_clears_prerequisites_with_null(client):
    seed("prereq_a")
    seed("plain", prerequisites=json.dumps([{"item": "prereq_a"}]))
    r = correct(client, "plain", {"prerequisites": None})
    assert r.status_code == 200, r.text
    assert r.json()["prerequisites"] == []
    assert r.json()["blocked"] is False


def test_correct_clearing_prerequisites_writes_a_field_correction_note(client):
    """Clearing a block is a recorded act (ADR platform/decisions/0074),
    matching the note `topics` already gets on the same shape."""
    seed("prereq_a")
    seed("plain", prerequisites=json.dumps([{"item": "prereq_a"}]))
    r = correct(client, "plain", {"prerequisites": []})
    assert r.status_code == 200, r.text
    body = get_item(client, "plain").json()
    notes = [c["body"] for c in body["comments"]]
    note = next((n for n in notes if "prerequisites" in n), None)
    assert note is not None, notes
    assert "vivy-agent" in note
    assert "prereq_a" in note


def test_correct_rejects_a_non_list_prerequisites(client):
    seed("plain")
    r = correct(client, "plain", {"prerequisites": "not-a-list"})
    assert r.status_code == 400, r.text


def test_correct_rejects_more_than_the_cap(client):
    seed("plain")
    too_many = [{"text": f"condition {i}"} for i in range(
        store.MAX_PREREQUISITES_PER_ITEM + 1)]
    r = correct(client, "plain", {"prerequisites": too_many})
    assert r.status_code == 400, r.text


def test_correct_rejects_an_entry_with_neither_item_nor_text(client):
    seed("plain")
    r = correct(client, "plain", {"prerequisites": [{}]})
    assert r.status_code == 400, r.text


def test_correct_rejects_an_entry_with_both_item_and_text(client):
    seed("prereq_a")
    seed("plain")
    r = correct(client, "plain",
               {"prerequisites": [{"item": "prereq_a", "text": "also this"}]})
    assert r.status_code == 400, r.text


def test_correct_rejects_empty_text(client):
    seed("plain")
    r = correct(client, "plain", {"prerequisites": [{"text": "   "}]})
    assert r.status_code == 400, r.text


def test_correct_rejects_text_longer_than_the_prerequisite_bound(client):
    seed("plain")
    r = correct(client, "plain",
               {"prerequisites": [{"text": "x" * (store.MAX_PREREQUISITE_TEXT + 1)}]})
    assert r.status_code == 400, r.text


def test_correct_accepts_a_text_longer_than_max_ref(client):
    """An unpark condition migrates verbatim as one text entry, and the longest
    real one was 898 characters. `MAX_REF` (500) bounds a PR reference, not a
    condition, so a text past it is accepted up to the prerequisite bound."""
    seed("plain")
    text = "y" * 898
    r = correct(client, "plain", {"prerequisites": [{"text": text}]})
    assert r.status_code == 200, r.text
    assert r.json()["prerequisites"][0]["text"] == text


def test_correct_rejects_a_duplicate_text_entry(client):
    """The verdict route judges only the first text match (`break`), so a
    second identical entry would silently keep the item blocked no matter
    what the caller judges — refused rather than accepted and ignored."""
    seed("plain")
    r = correct(client, "plain", {"prerequisites": [
        {"text": "same condition"}, {"text": "same condition"}]})
    assert r.status_code == 400, r.text


def test_correct_rejects_a_duplicate_item_entry(client):
    seed("prereq_a")
    seed("plain")
    r = correct(client, "plain", {"prerequisites": [
        {"item": "prereq_a"}, {"item": "prereq_a"}]})
    assert r.status_code == 400, r.text


def test_correct_rejects_an_item_naming_itself(client):
    seed("plain")
    r = correct(client, "plain", {"prerequisites": [{"item": "plain"}]})
    assert r.status_code == 400, r.text


def test_correct_rejects_an_unknown_item_id(client):
    seed("plain")
    r = correct(client, "plain",
               {"prerequisites": [{"item": "issue_does_not_exist"}]})
    assert r.status_code == 400, r.text
    assert "unknown item id" in r.json()["error"]


def test_correct_rejects_an_out_of_scope_item_id_the_same_as_unknown(client):
    """One wording for both, matching contract §6.1's rule for Course
    entries — so the refusal cannot be used to learn which ids exist in a
    product this identity cannot read."""
    seed("other_prereq", product="constance")
    seed("plain")
    unknown = correct(client, "plain",
                      {"prerequisites": [{"item": "issue_does_not_exist"}]})
    out_of_scope = correct(client, "plain",
                           {"prerequisites": [{"item": "other_prereq"}]})
    assert unknown.status_code == out_of_scope.status_code == 400
    assert unknown.json()["error"] == out_of_scope.json()["error"]


def test_correct_replacing_prerequisites_keeps_a_matching_texts_verdict(client):
    seed("plain", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    verdict(client, "plain", "a condition", True)
    # A full replacement resending the SAME text alongside a new one — the
    # matching entry must keep its verdict rather than reset to unmet.
    r = correct(client, "plain", {"prerequisites": [
        {"text": "a condition"}, {"text": "a brand new condition"}]})
    assert r.status_code == 200, r.text
    by_text = {e["text"]: e for e in r.json()["prerequisites"]}
    assert by_text["a condition"]["met"] is True
    assert by_text["a brand new condition"]["met"] is False


def test_correcting_prerequisites_on_an_approved_epic_leaves_maturity_approved(client):
    seed("epic_ok", type="epic", maturity="approved",
        specification="the long form", definition_of_done="the finish line")
    seed("prereq_a")
    r = correct(client, "epic_ok",
               {"prerequisites": [{"item": "prereq_a"}]})
    assert r.status_code == 200, r.text
    assert r.json()["maturity"] == "approved"


# --- POST /items also accepts prerequisites -------------------------------------


def test_create_item_accepts_prerequisites(client):
    seed("prereq_a")
    r = client.post("/items", json={
        "product": "vivy", "title": "a new item", "severity": "low",
        "prerequisites": [{"item": "prereq_a"}]}, headers=_h(WIDE))
    assert r.status_code == 201, r.text
    assert r.json()["prerequisites"] == [
        {"item": "prereq_a", "state": "open", "title": "prereq_a"}]
    assert r.json()["blocked"] is True


# --- the verdict route's capability gate ----------------------------------------


def test_verdict_route_succeeds_for_an_agent_holding_item_resolve(client):
    seed("plain", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    r = verdict(client, "plain", "a condition", True, token=AGENT)
    assert r.status_code == 200, r.text


def test_verdict_route_refuses_an_identity_without_item_resolve(client, monkeypatch):
    seed("plain", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"item.resolve"}})
    r = verdict(client, "plain", "a condition", True, token=AGENT)
    assert r.status_code == 403, r.text
    assert "item.resolve" in r.json()["error"]


def test_verdict_route_succeeds_for_approved_client(client):
    seed("plain", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    r = verdict(client, "plain", "a condition", True, token=APPROVED)
    assert r.status_code == 200, r.text


def test_verdict_route_404s_when_no_entry_has_that_text(client):
    seed("plain", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    r = verdict(client, "plain", "a different condition entirely", True)
    assert r.status_code == 404, r.text


def test_verdict_route_404s_on_an_out_of_scope_item(client):
    seed("other", product="constance", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    r = verdict(client, "other", "a condition", True, token=NARROW_APPROVED)
    assert r.status_code == 404, r.text


def test_verdict_route_404s_on_no_such_item(client):
    r = verdict(client, "issue_does_not_exist", "a condition", True)
    assert r.status_code == 404, r.text


def test_verdict_route_404_wording_matches_every_other_item_route(client):
    """One wording for "no such item, or none this identity may see"
    (`refusals.not_found()`, platform/contracts/library-protocol §8) — not a
    route-local `{"error": "no such item"}`."""
    r = verdict(client, "issue_does_not_exist", "a condition", True)
    assert r.status_code == 404, r.text
    assert r.json()["error"] == "no such item, or none this identity may see"

    seed("other", product="constance", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    r = verdict(client, "other", "a condition", True, token=NARROW_APPROVED)
    assert r.status_code == 404, r.text
    assert r.json()["error"] == "no such item, or none this identity may see"


def test_verdict_route_refuses_a_non_boolean_met(client):
    seed("plain", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    r = client.post("/items/plain/prerequisites/verdict",
                    json={"text": "a condition", "met": "true"}, headers=_h())
    assert r.status_code == 400, r.text


def test_verdict_route_records_the_verdict_on_the_thread(client):
    seed("plain", prerequisites=json.dumps(
        [{"text": "a condition", "met": False, "judged_by": None,
          "judged_at": None}]))
    verdict(client, "plain", "a condition", True)
    body = get_item(client, "plain", WIDE).json()
    notes = [c["body"] for c in body["comments"]]
    assert any("a condition" in n for n in notes)


# --- a malformed stored JSON value does not 500 the listing ---------------------


def test_a_malformed_stored_prerequisites_value_does_not_500_the_listing(client):
    seed("malformed", prerequisites="{not valid json")
    r = client.get("/items", headers=_h(WIDE))
    assert r.status_code == 200, r.text
    by_id = {i["id"]: i for i in r.json()["items"]}
    assert by_id["malformed"]["prerequisites"] == []
    assert by_id["malformed"]["blocked"] is False


def test_a_malformed_stored_prerequisites_value_does_not_500_claimable(client):
    seed("malformed", prerequisites="{not valid json")
    r = client.get("/items?state=claimable", headers=_h(WIDE))
    assert r.status_code == 200, r.text
    assert "malformed" in {i["id"] for i in r.json()["items"]}


def test_a_malformed_stored_prerequisites_value_does_not_500_get_item(client):
    seed("malformed", prerequisites="{not valid json")
    r = get_item(client, "malformed", WIDE)
    assert r.status_code == 200, r.text
    assert r.json()["prerequisites"] == []
    assert r.json()["blocked"] is False


# --- one rule: the claimable listing, GET /items/{id}, and checkout agree on
# --- EVERY stored value, well-formed or not -------------------------------------
#
# `blocked_clause()` (SQL, the claimable listing) and
# `prerequisite_statuses`/`unmet_prerequisite_count` (Python, `GET
# /items/{id}` and the checkout refusal) each implement the same rule
# independently. Each row below is written straight into the column — the
# way a hand-edited row reaches this table — bypassing `_validate_prerequisites`
# entirely, so this covers shapes no write path in this service produces
# today as well as the ordinary ones. `expected_blocked=None` means the case
# exists to prove nothing 500s, not to assert a particular verdict.
#
# Two of these disagreed on eea2d31: `item_null` (the claimable listing said
# not blocked, `GET /items/{id}` said blocked) and `text_no_met` (the other
# way round) — both from three-valued SQL logic and a Python "in" check that
# didn't require the value to be a string. Three more (the JSON scalars)
# 500'd `GET /items/{id}` and the listing outright, since `_json_or` returning
# a non-list still reached a bare `for e in entries`.
PARITY_SHAPES = [
    ("null_column", None, False),
    ("empty_string", "", False),
    ("invalid_json", "{not valid json", False),
    ("scalar_int", "5", False),
    ("scalar_true", "true", False),
    ("scalar_null_json", "null", False),
    ("empty_array", "[]", False),
    ("array_bare_string", '["a string"]', False),
    ("array_number", "[5]", False),
    ("item_null", '[{"item": null}]', False),
    ("item_number", '[{"item": 5}]', False),
    ("item_unresolved", '[{"item": "prereq_open"}]', True),
    ("item_resolved", '[{"item": "prereq_done"}]', False),
    ("item_missing_id", '[{"item": "issue_does_not_exist"}]', True),
    ("text_no_met", '[{"text": "cond"}]', True),
    ("text_met_false", '[{"text": "cond", "met": false}]', True),
    ("text_met_true", '[{"text": "cond", "met": true}]', False),
    ("text_met_string_true", '[{"text": "cond", "met": "true"}]', True),
    ("text_met_int_one", '[{"text": "cond", "met": 1}]', True),
    ("text_number", '[{"text": 5}]', False),
    ("empty_object", "[{}]", False),
    ("unrelated_keys", '[{"foo": "bar"}]', False),
]


@pytest.mark.parametrize("label,raw,expected_blocked", PARITY_SHAPES,
                         ids=[c[0] for c in PARITY_SHAPES])
def test_claimable_listing_get_item_and_checkout_agree_on_every_shape(
        client, label, raw, expected_blocked):
    seed("prereq_open")
    seed("prereq_done", state="fixed")
    seed("subject", prerequisites=raw)

    item_view = get_item(client, "subject", WIDE)
    assert item_view.status_code == 200, f"{label}: {item_view.text}"
    item_blocked = item_view.json()["blocked"]
    assert item_blocked is expected_blocked, (
        f"{label}: GET /items/{{id}} blocked={item_blocked}, "
        f"expected {expected_blocked}")

    listing = client.get("/items?state=claimable", headers=_h(WIDE))
    assert listing.status_code == 200, f"{label}: {listing.text}"
    subject_claimable = "subject" in {
        i["id"] for i in listing.json()["items"]}
    assert subject_claimable == (not expected_blocked), (
        f"{label}: claimable listing disagrees with GET /items/{{id}}'s "
        f"blocked={item_blocked}")

    r = checkout(client, "subject")
    assert r.status_code == 200, f"{label}: {r.text}"
    assert r.json()["claimed"] is (not expected_blocked), (
        f"{label}: checkout claimed={r.json()['claimed']} disagrees with "
        f"GET /items/{{id}}'s blocked={item_blocked}")


# --- checkout closes the race a concurrent write opens ---------------------------


def _block_it_mid_flight(monkeypatch):
    """Commit a blocking prerequisite from a SEPARATE connection just before
    `lease.take()` runs, which is after the early unlocked check has passed."""
    real_take = lease.take

    def sneaky_take(conn, item, **kwargs):
        side = sqlite3.connect(store.DB_PATH)
        side.execute(
            "UPDATE issue_items SET prerequisites = ? WHERE id = ?",
            (json.dumps([{"text": "snuck in", "met": False,
                         "judged_by": None, "judged_at": None}]),
             item["id"]))
        side.commit()
        side.close()
        return real_take(conn, item, **kwargs)

    monkeypatch.setattr(lease, "take", sneaky_take)


def test_a_claim_refuses_a_race_where_a_concurrent_write_blocks_the_item(
        client, monkeypatch):
    """The claim UPDATE's `WHERE NOT blocked` is what refuses it, on the row as
    SQLite sees it under the write lock: no claim, no lease, no event."""
    seed("racer")
    _block_it_mid_flight(monkeypatch)
    r = claim(client, "racer")
    assert r.status_code == 409, r.text
    assert "unmet prerequisites" in r.json()["error"]
    fresh = get_item(client, "racer", WIDE).json()
    assert fresh["state"] == "open" and fresh["blocked"] is True
    assert _live_leases() == set()


def test_a_checkout_racing_a_concurrent_block_ends_lease_only(
        client, monkeypatch):
    """`_do_checkout`'s early `unmet_prerequisite_count` check reads an
    unlocked row. A concurrent write (a `/correct` adding a blocking
    prerequisite, or a verdict flipping one back to unmet) can commit after
    that read. A checkout decides claimed-or-lease-only on the row as it is
    once `lease.take()` holds the write lock, so it ends holding the lease and
    not the claim, the same answer it gives an item that was blocked all along.

    The concurrent write lands through a SEPARATE connection that commits
    independently, so it survives this checkout's own rollback — the same
    as a genuinely concurrent writer would."""
    seed("racer")
    real_take = lease.take

    def sneaky_take(conn, item, **kwargs):
        side = sqlite3.connect(store.DB_PATH)
        side.execute(
            "UPDATE issue_items SET prerequisites = ? WHERE id = ?",
            (json.dumps([{"text": "snuck in", "met": False,
                         "judged_by": None, "judged_at": None}]),
             item["id"]))
        side.commit()
        side.close()
        return real_take(conn, item, **kwargs)

    monkeypatch.setattr(lease, "take", sneaky_take)
    r = checkout(client, "racer")
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is False
    assert r.json()["unmet_count"] == 1
    assert _live_leases() == {("racer", "session-1")}

    fresh = get_item(client, "racer", WIDE).json()
    assert fresh["state"] == "open"
    assert fresh["blocked"] is True
    assert fresh["prerequisites"] == [
        {"text": "snuck in", "met": False, "judged_by": None,
         "judged_at": None}]
