"""`POST /merge/admission`, `PUT`/`GET /merge/admission/enforcement` and
`POST /merge/outcome` (CQ·219, issue_01M5FM2W1JV82A3XH3P0R5TX8J): the
route now calls `merge_admission.evaluate` (CQ·218) and enforces by
default, and turning enforcement off is a recorded operator write shown
on `/health`.

**`merge_admission.evaluate` itself is monkeypatched here.** It is tested
against the real schema and a real git checkout in
`test_merge_admission_evaluator.py`; this file is about the route's own
wiring — the enforcement switch, the response shape, the capability gates
— and stubbing the evaluator keeps it from needing a knowledge checkout or
review-record fixtures of its own.

`merge_admission_enforced` itself (an empty table enforces, the newest row
wins, append-only) is tested directly against `store.py` in
`test_merge_admission_settings.py`; this file exercises it only through
the routes.

`/merge/outcome`'s reconciliation is unchanged by CQ·219 and still a
deliberate no-op — it validates, records a signal, and answers.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import merge_admission
import projects
import protocol
import store

AGENT = "agent-token-library"
SYSTEM = "system-token"
NOSCOPE = "agent-token-noscope"
OTHER_PRODUCT = "agent-token-fennick"
OPERATOR = "deck-token"
HUMAN = "operator@example.com"

# The evaluator's answer when a pull request has no run-verified review on
# file — the shape a caller with no reviews sees against an empty settings
# table (enforced by default).
MISSING_REVIEW = [{"kind": "review", "role": "reviewer", "workflow": None,
                   "detail": "no run-verified review from reviewer",
                   "retryable": False}]


def _refuses(repo, pr, head_sha, *, conn=None):
    return {"approved": False, "missing": MISSING_REVIEW}


def _approves(repo, pr, head_sha, *, conn=None):
    return {"approved": True, "missing": []}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE merge_admission_settings (
            id INTEGER PRIMARY KEY, enforced BOOLEAN NOT NULL,
            set_by_client TEXT NOT NULL, acting_human TEXT NOT NULL,
            reason TEXT, set_at TEXT NOT NULL);
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        # `_admission_body()`'s repo is `convivy/library`, which resolves to
        # the `library` product — so the default credential is scoped there.
        AGENT: {"name": "library-agent", "products": ["library"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
        NOSCOPE: {"name": "scopeless", "products": []},
        # Scoped to a real product, but not the one `_admission_body()` asks
        # about — the "wrong scope" case, distinct from NOSCOPE's "no scope".
        OTHER_PRODUCT: {"name": "fennick-agent", "products": ["fennick"]},
    }))
    clients = tmp_path / "client-tokens.json"
    clients.write_text(json.dumps({
        OPERATOR: {"name": "deck", "products": ["*"]},
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "library": {"repos": ["library"]}, "fennick": {"repos": ["fennick"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", clients)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(merge_admission, "evaluate", _refuses)
    # `_merge_admission_cache` is normally seeded by `lifespan`, which this
    # fixture's plain `TestClient(...)` never runs — reset it here so a
    # previous test's `PUT` cannot leak into this one through the module
    # global `/health` reads.
    monkeypatch.setattr(libapp, "_merge_admission_cache",
                        dict(store.MERGE_ADMISSION_DEFAULT_HEALTH))
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _admission_body(**over):
    out = {"repo": "convivy/library", "pr": 53, "head_sha": "a" * 40,
           "requester": "co-bosun-0912"}
    out.update(over)
    return out


def _outcome_body(**over):
    out = {**_admission_body(), "merged": True}
    out.update(over)
    return out


def _put_enforcement(client, *, token=OPERATOR, **over):
    body = {"enforced": False, "acting_human": HUMAN}
    body.update(over)
    return client.put("/merge/admission/enforcement", json=body, headers=_h(token))


def _signal_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM signals").fetchall()]
    conn.close()
    return rows


# --- both routes exist and validate their bodies --------------------------------


def test_admission_exists(client):
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    assert r.status_code == 200


def test_outcome_exists(client):
    r = client.post("/merge/outcome", json=_outcome_body(), headers=_h())
    assert r.status_code == 200


@pytest.mark.parametrize("field", ["repo", "head_sha", "requester"])
def test_admission_refuses_a_missing_string_field(client, field):
    body = _admission_body()
    del body[field]
    r = client.post("/merge/admission", json=body, headers=_h())
    assert r.status_code == 400
    assert field in r.json()["error"]


@pytest.mark.parametrize("field", ["repo", "head_sha", "requester"])
def test_admission_refuses_a_wrong_typed_field(client, field):
    r = client.post("/merge/admission", json=_admission_body(**{field: 12}),
                    headers=_h())
    assert r.status_code == 400


def test_admission_refuses_a_missing_pr(client):
    body = _admission_body()
    del body["pr"]
    r = client.post("/merge/admission", json=body, headers=_h())
    assert r.status_code == 400
    assert "pr" in r.json()["error"]


def test_admission_refuses_a_boolean_pr(client):
    """`isinstance(True, int)` is true in Python, and `pr: true` is not a PR
    number — the same trap `POST /items/{id}/link_pr` already guards against."""
    r = client.post("/merge/admission", json=_admission_body(pr=True), headers=_h())
    assert r.status_code == 400


def test_admission_accepts_a_string_pr(client):
    r = client.post("/merge/admission", json=_admission_body(pr="53"), headers=_h())
    assert r.status_code == 200


def test_a_non_numeric_string_pr_fails_closed_not_500(client):
    """`_validate_merge_identity` accepts any non-empty string, so a `pr` that
    is not a number reaches the evaluator's `int()`. It must answer 200 with
    `approved: false`, never a 500 (CQ·219 round 1, reviewer)."""
    r = client.post("/merge/admission", json=_admission_body(pr="abc"), headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is False
    assert any(m["kind"] == "unreadable" for m in r.json()["missing"])


def test_admission_refuses_an_empty_string_pr(client):
    r = client.post("/merge/admission", json=_admission_body(pr="  "), headers=_h())
    assert r.status_code == 400


def test_admission_refuses_a_non_object_body(client):
    r = client.post("/merge/admission", content="not json", headers=_h())
    assert r.status_code == 400


@pytest.mark.parametrize("field", ["repo", "head_sha", "requester"])
def test_outcome_refuses_a_missing_string_field(client, field):
    body = _outcome_body()
    del body[field]
    r = client.post("/merge/outcome", json=body, headers=_h())
    assert r.status_code == 400
    assert field in r.json()["error"]


def test_outcome_refuses_a_missing_merged(client):
    body = _outcome_body()
    del body["merged"]
    r = client.post("/merge/outcome", json=body, headers=_h())
    assert r.status_code == 400
    assert "merged" in r.json()["error"]


def test_outcome_refuses_a_non_boolean_merged(client):
    r = client.post("/merge/outcome", json=_outcome_body(merged="yes"), headers=_h())
    assert r.status_code == 400


def test_outcome_refuses_a_non_string_reason(client):
    r = client.post("/merge/outcome",
                    json=_outcome_body(merged=False, reason=404), headers=_h())
    assert r.status_code == 400


def test_outcome_accepts_a_false_merge_with_a_reason(client):
    r = client.post("/merge/outcome",
                    json=_outcome_body(merged=False, reason="conflicts"),
                    headers=_h())
    assert r.status_code == 200


# --- an empty table enforces, and names what the evaluator found ----------------


def test_an_empty_table_refuses_and_names_the_missing_review(client):
    """The done-when: with no variable and an empty table, the route refuses
    a PR lacking reviews and names them."""
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is False
    assert body["enforced"] is True
    assert body["head_sha"] == "a" * 40
    assert body["missing"] == MISSING_REVIEW
    assert "no run-verified review from reviewer" in body["reason"]


def test_the_response_is_the_v1_shape_plus_three_fields(client):
    body = client.post("/merge/admission", json=_admission_body(),
                       headers=_h()).json()
    assert set(body.keys()) == {"approved", "reason", "head_sha", "enforced",
                                "missing"}


def test_an_approving_evaluator_approves(client, monkeypatch):
    monkeypatch.setattr(merge_admission, "evaluate", _approves)
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    body = r.json()
    assert body["approved"] is True
    assert body["enforced"] is True
    assert body["missing"] == []
    assert "satisfied" in body["reason"]


def test_enforcement_off_approves_regardless_of_the_evaluator(client, tmp_path):
    _put_enforcement(client, enforced=False, reason="incident")
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    body = r.json()
    assert body["approved"] is True
    assert body["enforced"] is False
    # Still reported — turning enforcement off does not hide what the
    # evaluator actually found.
    assert body["missing"] == MISSING_REVIEW
    assert "enforcement is off" in body["reason"]
    assert "no run-verified review from reviewer" in body["reason"]


def test_enforcement_off_with_nothing_missing_says_so_plainly(client, monkeypatch):
    monkeypatch.setattr(merge_admission, "evaluate", _approves)
    _put_enforcement(client, enforced=False)
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    body = r.json()
    assert body["approved"] is True
    assert body["missing"] == []
    assert body["reason"] == "approved: merge admission enforcement is off"


# --- capability -------------------------------------------------------------------


def test_admission_requires_a_credential(client):
    r = client.post("/merge/admission", json=_admission_body(),
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_outcome_requires_a_credential(client):
    r = client.post("/merge/outcome", json=_outcome_body(),
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


@pytest.mark.parametrize("role, token", [("agent", AGENT), ("system", SYSTEM)])
def test_admission_refused_without_merge_admit(client, monkeypatch, role, token):
    """Parametrized over both kinds rather than just `agent` — a future edit
    that drops `merge.admit` from the `system` literal in `ROLE_CAPS` while
    leaving `agent` alone must fail this file too, not just the comment
    warning against it in `identity.py`."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        role: identity.ROLE_CAPS[role] - {"merge.admit"}})
    r = client.post("/merge/admission", json=_admission_body(), headers=_h(token))
    assert r.status_code == 403
    assert "merge.admit" in r.json()["error"]


@pytest.mark.parametrize("role, token", [("agent", AGENT), ("system", SYSTEM)])
def test_outcome_refused_without_merge_report(client, monkeypatch, role, token):
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        role: identity.ROLE_CAPS[role] - {"merge.report"}})
    r = client.post("/merge/outcome", json=_outcome_body(), headers=_h(token))
    assert r.status_code == 403
    assert "merge.report" in r.json()["error"]


def test_admission_authorizes_a_system_credential(client):
    r = client.post("/merge/admission", json=_admission_body(), headers=_h(SYSTEM))
    assert r.status_code == 200


def test_outcome_authorizes_a_system_credential(client):
    r = client.post("/merge/outcome", json=_outcome_body(), headers=_h(SYSTEM))
    assert r.status_code == 200


# --- scope --------------------------------------------------------------------


def test_admission_refuses_a_caller_scoped_to_a_different_product(client):
    r = client.post("/merge/admission", json=_admission_body(), headers=_h(OTHER_PRODUCT))
    assert r.status_code == 404


def test_admission_refuses_a_scopeless_caller(client):
    r = client.post("/merge/admission", json=_admission_body(), headers=_h(NOSCOPE))
    assert r.status_code == 404


def test_outcome_refuses_a_caller_scoped_to_a_different_product(client):
    r = client.post("/merge/outcome", json=_outcome_body(), headers=_h(OTHER_PRODUCT))
    assert r.status_code == 404


def test_outcome_refuses_a_scopeless_caller(client):
    r = client.post("/merge/outcome", json=_outcome_body(), headers=_h(NOSCOPE))
    assert r.status_code == 404


def test_admission_refuses_an_unresolvable_repo(client):
    """Owner-qualified but the name half of it names no registered repo — this
    must refuse, not resolve to `None` and then leak an approval."""
    r = client.post("/merge/admission",
                    json=_admission_body(repo="convivy/no-such-repo"), headers=_h())
    assert r.status_code == 404


def test_admission_refuses_when_the_registry_is_unreadable(client, monkeypatch, tmp_path):
    """`projects.resolve()` fails OPEN when the registry can't be read — the
    scope check must not inherit that, or an outage turns into a pass-through
    for every caller."""
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "does-not-exist.json")
    projects.reload()
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    assert r.status_code == 404


# --- the audit signal --------------------------------------------------------------


def test_admission_writes_a_signal(client, tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    client.post("/merge/admission", json=_admission_body(), headers=_h())
    rows = _signal_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "merge_admission_stub_call"
    meta = json.loads(row["meta"])
    assert meta["repo"] == "convivy/library"
    assert meta["pr"] == 53
    assert meta["requester"] == "co-bosun-0912"
    assert meta["enforced"] is True
    assert meta["approved"] is False


def test_outcome_writes_a_signal(client, tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    client.post("/merge/outcome", json=_outcome_body(merged=False, reason="x"),
               headers=_h())
    rows = _signal_rows(db)
    assert len(rows) == 1
    row = rows[0]
    # SAME `kind` AS THE ADMISSION CALL — the spec's `kind` is fixed across both
    # routes; `source` is what tells the two apart in the Archives.
    assert row["kind"] == "merge_admission_stub_call"
    meta = json.loads(row["meta"])
    assert meta["merged"] is False
    assert meta["reason"] == "x"


def test_the_admission_signal_carries_a_produced_at_since_can_bound_on(
        client, tmp_path, monkeypatch):
    """CQ·207: `produced_at` used to be stored `None` on this kind, which
    `GET /signals?since=` never matches (`_since_clause`'s own docstring). A
    bound just before the call finds the row; a bound just after it does
    not — the shape that breaks again if the write site reverts to `None`."""
    db = tmp_path / "control.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    before = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    client.post("/merge/admission", json=_admission_body(), headers=_h())
    after = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # SYSTEM, NOT AGENT: the row's `product` is the literal `repo` field
    # (`convivy/library`), which `_scope_clause` compares verbatim, and
    # AGENT's scope is the resolved product slug (`library`) rather than
    # that literal. SYSTEM's `products: ["*"]` sees every row regardless.
    since_before = client.get(
        "/signals?kind=merge_admission_stub_call&since=" + before,
        headers=_h(SYSTEM)).json()
    assert len(since_before["signals"]) == 1
    assert since_before["signals"][0]["produced_at"] is not None

    since_after = client.get(
        "/signals?kind=merge_admission_stub_call&since=" + after,
        headers=_h(SYSTEM)).json()
    assert since_after["signals"] == []


def test_each_call_writes_its_own_signal_with_no_dedup_key(client, tmp_path, monkeypatch):
    """No `dedup_key`: a second admission check for the same head_sha is still a
    distinct event, not a collision to fold away."""
    db = tmp_path / "control.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    client.post("/merge/admission", json=_admission_body(), headers=_h())
    client.post("/merge/admission", json=_admission_body(), headers=_h())
    rows = _signal_rows(db)
    assert len(rows) == 2
    assert all(r["dedup_key"] is None for r in rows)


def test_a_failed_audit_write_does_not_turn_an_approval_into_a_refusal(
        client, monkeypatch):
    """`insert_signal` raises `WritesDisabled` whenever the vault write path is
    off. The merge answer must not depend on Archives being writable."""
    monkeypatch.setattr(merge_admission, "evaluate", _approves)
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    assert r.status_code == 200
    assert r.json()["approved"] is True


def test_a_failed_audit_write_does_not_cost_the_outcome_route_its_answer(
        client, monkeypatch):
    """The write failed, so the response must say so rather than assert a
    record that was never made — `recorded` is false here, not true."""
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post("/merge/outcome", json=_outcome_body(), headers=_h())
    assert r.status_code == 200
    assert r.json()["recorded"] is False


def test_outcome_reports_recorded_true_on_a_successful_write(client):
    r = client.post("/merge/outcome", json=_outcome_body(), headers=_h())
    assert r.status_code == 200
    assert r.json()["recorded"] is True


# --- PUT /merge/admission/enforcement: human-reserved -----------------------


def test_the_capability_is_human_reserved():
    assert "merge.enforcement" in identity.HUMAN_RESERVED_CAPS
    assert "merge.enforcement" in identity.ROLE_CAPS["approved_client"]
    assert "merge.enforcement" not in identity.ROLE_CAPS["agent"]
    assert "merge.enforcement" not in identity.ROLE_CAPS["system"]


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_put_refuses_an_agent_or_system_credential(client, token):
    """The done-when: an `agent` credential (and, parametrized, `system`) is
    refused 403 on the PUT."""
    r = _put_enforcement(client, token=token)
    assert r.status_code == 403
    assert "merge.enforcement" in r.json()["error"]


def test_put_refuses_no_credential(client):
    r = client.put("/merge/admission/enforcement",
                   json={"enforced": False, "acting_human": HUMAN},
                   headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_put_requires_acting_human(client):
    r = client.put("/merge/admission/enforcement", json={"enforced": False},
                   headers=_h(OPERATOR))
    assert r.status_code == 403
    assert "acting_human" in r.json()["error"]


@pytest.mark.parametrize("value", [None, "yes", 1])
def test_put_requires_an_enforced_boolean(client, value):
    r = _put_enforcement(client, enforced=value)
    assert r.status_code == 400


def test_put_refuses_a_non_object_body(client):
    r = client.put("/merge/admission/enforcement", content="not json",
                   headers=_h(OPERATOR))
    assert r.status_code == 400


def test_put_appends_a_row_and_returns_it(client):
    r = _put_enforcement(client, enforced=False, reason="incident")
    assert r.status_code == 200
    body = r.json()
    assert body["enforced"] is False
    assert body["changed_by_client"] == "deck"
    assert body["acting_human"] == HUMAN
    assert body["reason"] == "incident"
    assert body["changed_at"]


def test_put_refuses_while_writes_are_disabled(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = _put_enforcement(client)
    assert r.status_code == 503


def test_the_newest_put_wins(client):
    _put_enforcement(client, enforced=False)
    _put_enforcement(client, enforced=True)
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    assert r.json()["enforced"] is True


# --- GET /merge/admission/enforcement: gated on merge.enforcement, like PUT -


def test_get_enforcement_requires_a_credential(client):
    r = client.get("/merge/admission/enforcement",
                   headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_get_enforcement_refuses_an_agent_or_system_credential(client, token):
    """`merge.enforcement` gates the `GET` too, the same reserved capability
    as the `PUT` — `/health` already shows everything but `acting_human`
    with no credential at all, so an agent gains nothing this route
    withholds from it elsewhere."""
    r = client.get("/merge/admission/enforcement", headers=_h(token))
    assert r.status_code == 403
    assert "merge.enforcement" in r.json()["error"]


def test_get_enforcement_defaults_to_enforced_for_an_empty_table(client):
    r = client.get("/merge/admission/enforcement", headers=_h(OPERATOR))
    assert r.status_code == 200
    assert r.json() == {"enforced": True, "changed_at": None,
                        "changed_by_client": None, "acting_human": None,
                        "reason": None}


def test_get_enforcement_reflects_a_put_including_acting_human(client):
    _put_enforcement(client, enforced=False, reason="incident")
    r = client.get("/merge/admission/enforcement", headers=_h(OPERATOR))
    body = r.json()
    assert body["enforced"] is False
    assert body["changed_by_client"] == "deck"
    assert body["acting_human"] == HUMAN
    assert body["reason"] == "incident"


# --- /health: enforced, changed_at, changed_by_client, reason — never acting_human


def test_health_shows_enforced_true_for_an_empty_table(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["merge_admission"] == {
        "enforced": True, "changed_at": None, "changed_by_client": None,
        "reason": None}


def test_health_shows_enforced_false_with_the_time_and_client_after_a_put(client):
    """The done-when: after an approved-client PUT, `/health` shows
    `enforced: false` with the time and client — and never `acting_human`."""
    _put_enforcement(client, enforced=False, reason="incident")
    r = client.get("/health")
    assert r.status_code == 200
    state = r.json()["merge_admission"]
    assert state["enforced"] is False
    assert state["changed_by_client"] == "deck"
    assert state["changed_at"]
    assert state["reason"] == "incident"
    assert "acting_human" not in state


def test_health_needs_no_credential(client):
    _put_enforcement(client, enforced=False)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["merge_admission"]["enforced"] is False


# --- the boot line ------------------------------------------------------------------


def _boot_db(tmp_path):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE merge_admission_settings (
            id INTEGER PRIMARY KEY, enforced BOOLEAN NOT NULL,
            set_by_client TEXT NOT NULL, acting_human TEXT NOT NULL,
            reason TEXT, set_at TEXT NOT NULL);
    """)
    conn.commit()
    conn.close()
    return db


def _run_lifespan():
    import asyncio

    async def _run():
        async with libapp.lifespan(libapp.app):
            pass

    asyncio.run(_run())


def test_boot_line_says_enforced_for_an_empty_table(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", _boot_db(tmp_path))
    monkeypatch.delenv("LIBRARY_MERGE_ADMISSION_ENFORCED", raising=False)
    _run_lifespan()
    err = capsys.readouterr().err
    assert "library: merge admission ENFORCED" in err


def test_boot_line_names_the_client_when_a_put_has_turned_it_off(
        tmp_path, capsys, monkeypatch):
    db = _boot_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO merge_admission_settings "
        "(enforced, set_by_client, acting_human, reason, set_at) "
        "VALUES (0, 'deck', 'op@example.com', 'incident', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.delenv("LIBRARY_MERGE_ADMISSION_ENFORCED", raising=False)
    _run_lifespan()
    err = capsys.readouterr().err
    assert "NOT enforced" in err
    assert "deck" in err


def test_boot_line_says_the_env_variable_is_ignored_when_set(
        tmp_path, capsys, monkeypatch):
    """The done-when: the startup message when `LIBRARY_MERGE_ADMISSION_
    ENFORCED` is set names it as ignored — enforcement is a vault setting
    now, not an environment flag."""
    monkeypatch.setattr(store, "DB_PATH", _boot_db(tmp_path))
    monkeypatch.setenv("LIBRARY_MERGE_ADMISSION_ENFORCED", "1")
    _run_lifespan()
    err = capsys.readouterr().err
    assert "LIBRARY_MERGE_ADMISSION_ENFORCED is set but ignored" in err


def test_boot_line_says_nothing_about_the_env_variable_when_unset(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", _boot_db(tmp_path))
    monkeypatch.delenv("LIBRARY_MERGE_ADMISSION_ENFORCED", raising=False)
    _run_lifespan()
    err = capsys.readouterr().err
    assert "LIBRARY_MERGE_ADMISSION_ENFORCED" not in err
