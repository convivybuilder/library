"""`POST /merge/admission` and `POST /merge/outcome` — the Library's half of
merge admission (`issue_01M0EQTMZMAB93V9FFP4AD52JK`).

**The whole design is the enforcement switch.** `LIBRARY_MERGE_ADMISSION_ENFORCED`
unset (today's default) approves every request; set, no evaluator exists yet, so
the route answers an honest refusal rather than keep approving under a flag that
claims to be a gate. Both branches answer the same `{approved, reason}` shape, so
most of this file is proving that shape holds and that the flag is the only thing
that moves between the two answers.

`/merge/outcome`'s reconciliation is a deliberate no-op — it validates, records a
signal, and answers — so its tests only cover that surface, not a lease or a work
item it does not touch.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

AGENT = "agent-token-library"
SYSTEM = "system-token"
NOSCOPE = "agent-token-noscope"
OTHER_PRODUCT = "agent-token-vivy"


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
        OTHER_PRODUCT: {"name": "vivy-agent", "products": ["vivy"]},
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "library": {"repos": ["library"]}, "vivy": {"repos": ["vivy"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(store, "MERGE_ADMISSION_ENFORCED", False)
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


# --- the stub approves when the flag is unset ------------------------------------


def test_unset_flag_approves(client):
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    body = r.json()
    assert body["approved"] is True
    assert "not yet enforced" in body["reason"]


# --- the flag, set, refuses honestly rather than approve blind -------------------


def test_enforced_flag_refuses_honestly(client, monkeypatch):
    monkeypatch.setattr(store, "MERGE_ADMISSION_ENFORCED", True)
    r = client.post("/merge/admission", json=_admission_body(), headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is False
    assert "no evaluator" in body["reason"]


def test_the_enforced_answer_is_the_same_shape_as_the_stub(client, monkeypatch):
    """Same request, same response shape either way — flipping the flag is a
    Library-only deploy change, never one the Bosun has to coordinate with."""
    unset = client.post("/merge/admission", json=_admission_body(), headers=_h()).json()
    monkeypatch.setattr(store, "MERGE_ADMISSION_ENFORCED", True)
    enforced = client.post("/merge/admission", json=_admission_body(), headers=_h()).json()
    assert set(unset.keys()) == set(enforced.keys()) == {"approved", "reason"}


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
    off, which is every deploy today. The merge answer must not depend on
    Archives being writable — coupling the two would make `/merge/admission`
    refuse every call for the whole read-only shadow."""
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


# --- the boot line ------------------------------------------------------------------


def test_boot_line_says_stubbed_when_unset(capsys, monkeypatch):
    import asyncio
    monkeypatch.setattr(store, "MERGE_ADMISSION_ENFORCED", False)

    async def _run():
        async with libapp.lifespan(libapp.app):
            pass

    asyncio.run(_run())
    err = capsys.readouterr().err
    assert "merge admission STUBBED (always approves)" in err


def test_boot_line_says_enforced_when_set(capsys, monkeypatch):
    import asyncio
    monkeypatch.setattr(store, "MERGE_ADMISSION_ENFORCED", True)

    async def _run():
        async with libapp.lifespan(libapp.app):
            pass

    asyncio.run(_run())
    err = capsys.readouterr().err
    assert "merge admission ENFORCED" in err
