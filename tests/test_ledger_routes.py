"""The ledger's routes and the admission refusal they feed.

Built on a database from the real migration, for `test_ledger_store`'s reason:
the behaviour under test includes constraints, and a hand-written fixture schema
is a second copy that drifts.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import store

REPO = Path(__file__).resolve().parent.parent
AGENT = "agent-token"
NARROW = "narrow-token"
ORIENT = "orient-token"
SUBSCRIBER = "subscriber-token"
H = {"Library-Protocol": "1"}


def _agent(**over):
    h = {**H, "Authorization": f"Bearer {AGENT}"}
    h.update(over)
    return h


def _orient():
    return {**H, "Authorization": f"Bearer {ORIENT}"}


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "ledger.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    vector_index(db)

    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        " state, created_by, created_at, type, needs) "
        "VALUES ('issue_1','library','a thing','b','{}','medium','open',"
        " 'someone','2026-08-23 00:00:00','bug','agent')")
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "an-agent", "products": ["*"]},
        NARROW: {"name": "vivy-only", "products": ["vivy"]},
        SUBSCRIBER: {"name": "bosun-reporter", "products": ["*"],
                     "grants": ["ledger.spend.subscription"]}}))
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({ORIENT: {"name": "orient", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    # `POST /ledger-entries` books spend only to a registered, non-private
    # project and fails closed when the registry cannot be read; CI has no
    # real one, so every product these tests report under is listed here.
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {}, "library": {"repos": []}, "vivy": {"repos": []},
        "keel": {"repos": []}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    return TestClient(libapp.app)


def set_caps(client, scope_id="library", **over):
    body = {"scope_kind": "project", "scope_id": scope_id,
           "acting_human": "operator"}
    body.update(over)
    return client.put("/ledgers", headers=_orient(), json=body)


def report(client, usd, *, token=AGENT, **over):
    body = {"product": "library", "source": "agentctl", "usd": usd}
    body.update(over)
    headers = {**H, "Authorization": f"Bearer {token}"}
    return client.post("/ledger-entries", headers=headers, json=body)


def void(client, entry_id, **over):
    body = {"acting_human": "operator"}
    body.update(over)
    return client.post(f"/ledger-entries/{entry_id}/void", headers=_orient(),
                       json=body)


# --- setting a cap is human-only ----------------------------------------------

def test_an_agent_cannot_set_its_own_caps(client):
    """The whole reason `ledger.grant` is reserved.

    An agent that could raise its own ceiling makes the checkout refusal
    unenforceable: refused for a capped ledger, it raises its own cap and
    checks out again.
    """
    r = client.put("/ledgers", headers=_agent(), json={
        "scope_kind": "project", "scope_id": "library",
        "soft_cap_usd": 100, "acting_human": "operator"})
    assert r.status_code == 403
    assert "ledger.grant" in r.json()["error"]


def test_an_approved_client_sets_caps_and_the_person_is_recorded(client):
    r = set_caps(client, soft_cap_usd=160)
    assert r.status_code == 200, r.text
    assert r.json()["caps"]["soft_cap_usd"] == 160
    assert r.json()["caps"]["warn_usd"] is None
    assert r.json()["caps"]["hard_cap_usd"] is None
    # THE PERSON, NOT THE CLIENT. Recording Orient here would attribute the
    # cap change to the software that carried it.
    assert r.json()["acting_human"] == "operator"
    assert r.json()["attested_by"] == "orient"


def test_a_cap_change_without_an_attested_human_is_refused(client):
    r = client.put("/ledgers", headers=_orient(), json={
        "scope_kind": "project", "scope_id": "library", "soft_cap_usd": 10})
    assert r.status_code == 403
    assert "acting_human" in r.json()["error"]


def test_a_run_is_not_a_settable_scope(client):
    """A run's cap is its project's per-run cap, delivered in the checkout
    response, and agentctl enforces it."""
    r = client.put("/ledgers", headers=_orient(), json={
        "scope_kind": "run", "scope_id": "run-1",
        "soft_cap_usd": 10, "acting_human": "operator"})
    assert r.status_code == 400
    assert r.json()["supported"] == ["project"]


def test_all_four_caps_absent_is_a_legal_uncapped_ledger(client):
    r = set_caps(client)
    assert r.status_code == 200, r.text
    assert r.json()["caps"] == {"warn_usd": None, "soft_cap_usd": None,
                                "hard_cap_usd": None, "per_run_cap_usd": None}


def test_caps_must_be_non_decreasing(client):
    r = set_caps(client, warn_usd=100, soft_cap_usd=50, acting_human="operator")
    assert r.status_code == 400
    assert "non-decreasing" in r.json()["error"]


# --- the per-run cap ------------------------------------------------------------

def test_an_agent_cannot_set_its_own_per_run_cap(client):
    """The same reservation `test_an_agent_cannot_set_its_own_caps` pins for
    the three threshold caps: an agent that could set its own run's cap
    could also raise it."""
    r = client.put("/ledgers", headers=_agent(), json={
        "scope_kind": "project", "scope_id": "library",
        "per_run_cap_usd": 5, "acting_human": "operator"})
    assert r.status_code == 403
    assert "ledger.grant" in r.json()["error"]


def test_the_per_run_cap_is_set_read_and_cleared_through_put_and_get(client):
    r = set_caps(client, per_run_cap_usd=5)
    assert r.status_code == 200, r.text
    assert r.json()["caps"]["per_run_cap_usd"] == 5

    read = client.get("/ledgers", headers=_agent()).json()["ledgers"]
    assert read[0]["caps"]["per_run_cap_usd"] == 5

    cleared = set_caps(client, per_run_cap_usd=None)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["caps"]["per_run_cap_usd"] is None
    read_again = client.get("/ledgers", headers=_agent()).json()["ledgers"]
    assert read_again[0]["caps"]["per_run_cap_usd"] is None


def test_a_per_run_cap_alone_does_not_refuse_checkout(client):
    """A per-run cap bounds one run's own spend, not the project's
    accumulating total — set alone, with no warn, soft or hard cap, it must
    not refuse a checkout by itself."""
    set_caps(client, per_run_cap_usd=1)
    report(client, 1000)
    assert checkout(client).status_code == 200


def test_a_per_run_cap_does_not_change_a_capped_refusals_shape(client):
    """Set alongside an already-crossed soft cap, the per-run cap must not
    change the state or the refusal the soft cap already produces."""
    set_caps(client, soft_cap_usd=10, per_run_cap_usd=1)
    report(client, 10)
    r = checkout(client)
    assert r.status_code == 409
    body = r.json()["ledger"]
    assert body["cap"] == "soft_cap"
    assert body["cap_usd"] == 10
    assert body["state"] == "soft_capped"
    assert "per_run_cap" not in r.json()["ledger"]


# --- spend is agent-writable and never refused on a cap -----------------------

def test_an_agent_reports_spend(client):
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1.5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is True
    assert body["idempotent"] is False
    assert body["usd"] == 1.5
    assert body["total_usd"] == 1.5
    assert body["state"] == "ok"
    assert body["crossed"] is None


def test_spend_past_the_soft_cap_is_still_recorded(client):
    """A spend that already happened cannot be un-spent. Refusing the report
    would keep the ledger tidy by losing the record of the money."""
    set_caps(client, soft_cap_usd=10)
    r = report(client, 25)
    assert r.status_code == 200, r.text
    assert r.json()["total_usd"] == 25
    assert r.json()["state"] == "soft_capped"
    row = client.get("/ledgers", headers=_agent()).json()["ledgers"][0]
    assert row["total_usd"] == 25
    assert row["state"] == "soft_capped"


def test_a_repeated_report_is_idempotent(client):
    set_caps(client, soft_cap_usd=160)
    first = report(client, 2, dedup_key="run-1:msg-1:req-1")
    second = report(client, 2, dedup_key="run-1:msg-1:req-1")
    assert first.json()["idempotent"] is False
    assert second.json()["idempotent"] is True
    assert second.json()["id"] == first.json()["id"]
    assert client.get("/ledgers", headers=_agent()).json()["ledgers"][0]["total_usd"] == 2


def test_spend_for_an_uncapped_scope_is_recorded(client):
    """An uncapped project is the normal case, and most projects stay that
    way — losing the spend of every one of them is the bug this pins shut.
    An uncapped product's report records exactly like a capped one's, and
    both ledger reads serve it back."""
    r = report(client, 5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is True
    assert body["total_usd"] == 5
    assert body["state"] == "uncapped"

    ledgers = client.get("/ledgers", headers=_agent()).json()["ledgers"]
    assert len(ledgers) == 1
    assert ledgers[0]["scope_id"] == "library"
    assert ledgers[0]["total_usd"] == 5
    assert ledgers[0]["state"] == "uncapped"
    assert ledgers[0]["caps"] == {"warn_usd": None, "soft_cap_usd": None,
                                  "hard_cap_usd": None, "per_run_cap_usd": None}

    rows = client.get("/ledger-entries", headers=_agent()).json()["entries"]
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]
    assert rows[0]["usd"] == 5


def test_a_repeated_report_for_an_uncapped_scope_is_idempotent(client):
    """`dedup_key` idempotency holds with no cap set, exactly as it does for a
    capped ledger (`test_a_repeated_report_is_idempotent`) — the ledger row
    `ensure_live_ledger` creates on the first report is the same row the
    second report's dedup check reads."""
    first = report(client, 2, dedup_key="run-1:msg-1:req-1")
    second = report(client, 2, dedup_key="run-1:msg-1:req-1")
    assert first.json()["recorded"] is True
    assert first.json()["idempotent"] is False
    assert second.json()["recorded"] is True
    assert second.json()["idempotent"] is True
    assert second.json()["id"] == first.json()["id"]
    ledgers = client.get("/ledgers", headers=_agent()).json()["ledgers"]
    assert len(ledgers) == 1
    assert ledgers[0]["total_usd"] == 2
    assert len(client.get("/ledger-entries", headers=_agent()).json()["entries"]) == 1


def test_an_oversized_product_is_refused_before_it_creates_a_ledger(client):
    """`ensure_live_ledger` persists the caller-supplied `product` as
    `ledgers.scope_id`, the same column `PUT /ledgers` bounds to
    `MAX_LEDGER_SCOPE_ID` before it writes a row — without the same bound
    here, a wildcard-scoped agent credential could write an arbitrarily
    large string into that column, repeatably, with no row ever refused."""
    r = report(client, 1, product="x" * 5000)
    assert r.status_code == 400
    assert "longer than" in r.json()["error"]
    assert client.get("/ledgers", headers=_agent()).json()["ledgers"] == []
    assert client.get("/ledger-entries", headers=_agent()).json()["entries"] == []


def test_a_negative_spend_is_refused(client):
    """No hint promising a compensating-entry mechanism through this route —
    the correction route is `POST /ledger-entries/{id}/void`, not a negative
    report."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, -5)
    assert r.status_code == 400
    assert "must not be negative" in r.json()["error"]


# --- lane: accepted, not yet required, and never blended on the way out ------
#
# issue_01M35QG5NA13JE8FQVM9XET4HD. `gateway` is metered provider spend;
# `subscription` is a flat-billed estimate that is not money a budget can be
# enforced against. This PR's contract: a write names a `lane` or does not,
# a value outside the closed set is refused, and no read ever sums the two.

def test_a_write_with_no_lane_is_still_recorded(client):
    """The first of two contract steps — this PR accepts `lane`, it does not
    yet require it. A client that has not started sending one sees exactly
    what it saw before this column existed."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 5)
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is True
    assert r.json()["lane"] is None
    entries = client.get("/ledger-entries", headers=_agent()).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["lane"] is None


def test_an_unknown_lane_is_refused_and_writes_nothing(client):
    """Refused as a bad VALUE for a field this route understands — the
    closed-set shape `source` already refuses with — not merely as a field
    this route has never heard of."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 5, lane="promo-credit")
    assert r.status_code == 400, r.text
    assert "lane" in r.json()["error"]
    assert r.json()["supported"] == sorted(store.LEDGER_LANES)
    assert client.get("/ledger-entries", headers=_agent()).json()["entries"] == []
    assert client.get("/ledgers", headers=_agent()).json()["ledgers"][0]["total_usd"] == 0


@pytest.mark.parametrize("field", ["source", "lane"])
@pytest.mark.parametrize("bad", [[], {}])
def test_a_non_string_closed_set_field_is_refused_not_a_500(client, field, bad):
    """`source not in LEDGER_SOURCES` and `lane not in LEDGER_LANES` both hash
    their operand: an unhashable `list`/`dict` raised an uncaught `TypeError`
    before this test existed, reaching the caller as a bare 500 instead of
    the closed-set refusal every other bad value on this route gets."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 5, **{field: bad})
    assert r.status_code == 400, r.text
    assert field in r.json()["error"]
    assert client.get("/ledger-entries", headers=_agent()).json()["entries"] == []
    assert client.get("/ledgers", headers=_agent()).json()["ledgers"][0]["total_usd"] == 0


def test_a_dedup_replay_naming_a_different_lane_keeps_the_original(client):
    """Idempotent replay never re-validates a field against what the caller
    just sent — it returns the stored row as it was first written, the same
    way it already does for a mismatched `usd`, `source` or `product`."""
    set_caps(client, soft_cap_usd=160)
    first = report(client, 5, lane="gateway", dedup_key="run-1:msg-1")
    assert first.status_code == 200, first.text
    second = report(client, 5, lane="subscription", dedup_key="run-1:msg-1",
                     token=SUBSCRIBER)
    assert second.status_code == 200, second.text
    assert second.json()["idempotent"] is True
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["lane"] == "gateway"
    row = client.get("/ledgers", headers=_agent()).json()["ledgers"][0]
    assert row["total_usd"] == 5
    assert row["lanes"]["gateway"] == 5
    assert row["lanes"]["subscription"] == 0


# --- lane: "subscription" needs a second capability, never ledger.spend alone -

def test_an_ordinary_agent_cannot_claim_the_subscription_lane(client):
    """`ledger.spend` sits on every agent and system credential; without
    `ledger.spend.subscription` an ordinary caller cannot keep its spend out
    of enforcement by tagging it `subscription`."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 5, lane="subscription")
    assert r.status_code == 403, r.text
    assert "ledger.spend.subscription" in r.json()["error"]
    assert client.get("/ledger-entries", headers=_agent()).json()["entries"] == []


def test_a_credential_granted_the_subscription_capability_may_claim_it(client):
    """Granted only through the credential's own `grants`
    (`identity.GRANT_ONLY_CAPS`), the same path `machine.verify` takes."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 5, lane="subscription", token=SUBSCRIBER)
    assert r.status_code == 200, r.text
    assert r.json()["lane"] == "subscription"


def test_ledger_spend_subscription_is_on_no_role(client):
    """`ledger.spend.subscription` must not reach a caller through `agent`,
    `system` or `approved_client` — only through an entry's own `grants`, the
    gap this capability exists to close."""
    for role, caps in identity.ROLE_CAPS.items():
        assert "ledger.spend.subscription" not in caps, role


def test_entries_carry_their_lane_and_no_read_blends_the_two(client):
    """Gateway and subscription spend on the same ledger, the same day. Every
    read that reports a figure for this ledger keeps the two apart —
    `GET /ledger-entries`' rows each name their own lane, and
    `GET /ledgers`' totals never sum the two lanes into one number."""
    set_caps(client, soft_cap_usd=160)
    gateway = report(client, 10, lane="gateway").json()
    subscription = report(client, 7, lane="subscription", dedup_key="sub-1",
                          token=SUBSCRIBER).json()
    unset = report(client, 3).json()
    assert {gateway["lane"], subscription["lane"], unset["lane"]} == \
        {"gateway", "subscription", None}

    entries = client.get("/ledger-entries", headers=_agent()).json()["entries"]
    by_id = {e["id"]: e for e in entries}
    assert by_id[gateway["id"]]["lane"] == "gateway"
    assert by_id[subscription["id"]]["lane"] == "subscription"
    assert by_id[unset["id"]]["lane"] is None

    row = client.get("/ledgers", headers=_agent()).json()["ledgers"][0]
    # The enforceable figure folds the untagged entry in with gateway, never
    # with subscription — $10 (gateway) + $3 (no lane) = $13, not $20.
    assert row["total_usd"] == 13
    assert row["lanes"]["gateway"] == 13
    assert row["lanes"]["subscription"] == 7
    # NEVER SUMMED: the one number this test exists to rule out.
    assert row["total_usd"] != gateway["usd"] + subscription["usd"] + unset["usd"]


def test_ledgers_refuses_an_unrecognised_parameter(client):
    r = client.get("/ledgers?product=library", headers=_agent())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["product"]
    assert r.json()["supported"] == ["limit", "offset", "scope_id"]


def test_ledgers_ordinary_request_still_succeeds(client):
    r = client.get("/ledgers?scope_id=library&limit=5&offset=0", headers=_agent())
    assert r.status_code == 200


def test_ledger_entries_refuses_an_unrecognised_parameter(client):
    r = client.get("/ledger-entries?scope_id=library", headers=_agent())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["scope_id"]
    assert r.json()["supported"] == sorted(
        ["day", "epic_id", "from", "item_id", "limit", "offset", "product",
         "run_id", "source", "to"])


def test_subscription_spend_never_crosses_a_threshold_or_refuses_checkout(client):
    """Only gateway spend (and spend with no lane, today's default) is money
    a budget can be enforced against. A subscription-lane report that would,
    by its dollar figure alone, blow well past a soft cap must neither cross
    a threshold nor refuse the next checkout — enforcement reads the
    enforceable total, which this entry never joins. $50 is comfortably past
    the $10 soft cap and comfortably under its $100 proportionate ceiling
    (10x), so this is the cap refusing, or not, rather than that bound."""
    set_caps(client, soft_cap_usd=10)
    r = report(client, 50, lane="subscription", token=SUBSCRIBER)
    assert r.status_code == 200, r.text
    assert r.json()["crossed"] is None
    assert r.json()["total_usd"] == 0
    assert r.json()["state"] == "ok"
    assert checkout(client).status_code == 200

    # The same dollar amount, tagged gateway instead, does refuse — proof
    # the test above is exercising the lane and not some other exemption.
    r2 = report(client, 50, lane="gateway", dedup_key="gateway-50")
    assert r2.status_code == 200, r2.text
    assert r2.json()["state"] == "soft_capped"
    assert checkout(client).status_code == 409


# --- the closed set: no free-text attribution dimension ------------------------

def test_an_unrecognised_field_is_refused_naming_it(client):
    """Clause 4 of the ledger-store epic (issue_01M0B8TDRCH7SCGNSXHRGSV4TF):
    a payload naming a free-text attribution dimension is refused rather than
    silently dropped."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, feature="report-narrative")
    assert r.status_code == 400, r.text
    assert "feature" in r.json()["error"]


def test_several_unrecognised_fields_are_all_named(client):
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, feature="report-narrative", cost_center="eng")
    assert r.status_code == 400, r.text
    assert "cost_center" in r.json()["error"]
    assert "feature" in r.json()["error"]


def test_the_full_documented_set_is_accepted(client):
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, day=libapp.local_day(), run_id="run-1", agent="an-agent",
               model="claude-sonnet-5", item_id="issue_1", person="operator",
               team="library", dedup_key="run-1:msg-1", lane="gateway")
    assert r.status_code == 200, r.text
    assert r.json()["lane"] == "gateway"


def test_the_ingest_contracts_worked_example_is_accepted(client):
    """`platform/reference/llm-spend-ingest-contract`'s worked example for a
    cloud app's own report — the one real caller shape the contract
    documents; nothing in the fleet calls this route yet
    (issue_01M0B8TDRCH7SCGNSXHRGSV4TF's parent epic, checked 2026-09-29)."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 0.0123, product="library", source="app",
               model="claude-sonnet-4-6", dedup_key="recipe-extract-8821")
    assert r.status_code == 200, r.text


# --- the day is bounded, not trusted -------------------------------------------

def test_a_day_far_from_the_librarys_own_is_refused(client):
    """A gate must never trust a field its subject controls.

    Unbounded, a caller books spend to whichever day still has no cap crossed
    and the checkout refusal never fires.
    """
    set_caps(client, soft_cap_usd=10)
    assert report(client, 100, day="2020-01-01").status_code == 422
    r = client.get("/ledgers", headers=_agent()).json()["ledgers"][0]
    assert r["total_usd"] == 0


def test_a_day_one_either_side_is_accepted(client):
    """Wide enough for a real timezone offset and for clock skew."""
    set_caps(client, soft_cap_usd=160)
    today = libapp.local_day()
    import datetime as dt
    for delta in (-1, 0, 1):
        day = (dt.date.fromisoformat(today) + dt.timedelta(days=delta)).isoformat()
        assert report(client, 1, day=day).status_code == 200, day


def test_the_day_window_is_compared_as_dates_not_strings(client):
    """`'2026-03-01'` and `'2026-02-28'` are one day apart and eleven characters
    of string comparison say otherwise. A string-compare version of this check
    would be wrong at every month boundary — twelve times a year, in a check
    whose entire job is to be unfoolable."""
    assert libapp._within_a_day("2026-03-01", "2026-02-28") is True
    assert libapp._within_a_day("2026-02-28", "2026-03-01") is True
    assert libapp._within_a_day("2026-03-05", "2026-02-28") is False
    assert libapp._within_a_day("not-a-date", "2026-02-28") is False


# --- the admission refusal ------------------------------------------------------

def checkout(client):
    return client.post("/items/issue_1/checkout", headers=_agent(),
                       json={"owner": "co-library"})


def test_an_uncapped_product_checks_out_normally(client):
    """The load-bearing default. Most projects stay uncapped, so failing
    closed would stop the fleet on the day this ships."""
    assert checkout(client).status_code == 200


def test_an_uncapped_products_checkout_is_not_refused_once_it_has_spend(client):
    """The same default, after `ensure_live_ledger` has materialized a row for
    this scope. `_refuse_on_capped_ledger` must keep treating that row's
    `uncapped` state as never refusing, the same as the no-row case above —
    `store.live_ledger` now answers with a row rather than None, so this is
    the one case the earlier test cannot exercise."""
    report(client, 5)
    assert checkout(client).status_code == 200


def test_a_ledger_under_its_caps_checks_out_normally(client):
    set_caps(client, soft_cap_usd=160)
    report(client, 1)
    assert checkout(client).status_code == 200


def test_a_soft_capped_ledger_refuses_the_checkout(client):
    set_caps(client, soft_cap_usd=10)
    report(client, 10)
    r = checkout(client)
    assert r.status_code == 409
    body = r.json()
    assert "soft_cap" in body["error"]
    # NAMES THE NUMBERS, following the lease refusal's worked example: a refusal
    # a caller cannot act on wastes the run twice.
    assert body["ledger"]["cap"] == "soft_cap"
    assert body["ledger"]["cap_usd"] == 10
    assert body["ledger"]["total_usd"] == 10
    assert "another item" in body["hint"]


def test_a_hard_capped_ledger_with_no_soft_cap_refuses_the_checkout(client):
    set_caps(client, hard_cap_usd=10)
    report(client, 10)
    r = checkout(client)
    assert r.status_code == 409
    assert r.json()["ledger"]["cap"] == "hard_cap"


def test_crossing_the_warning_threshold_alone_does_not_refuse_checkout(client):
    set_caps(client, warn_usd=10)
    report(client, 100)
    assert checkout(client).status_code == 200


def test_the_refusal_leaves_the_item_exactly_as_it_found_it(client):
    """It runs before the lease is taken, so a refused checkout strands
    nothing."""
    set_caps(client, soft_cap_usd=10)
    report(client, 10)
    assert checkout(client).status_code == 409
    leases = client.get("/leases", headers=_agent()).json()["leases"]
    assert leases == []
    item = client.get("/items/issue_1", headers=_agent()).json()
    assert item["state"] == "open"


def test_release_is_not_refused_by_a_capped_ledger(client):
    """Refusing to let go of work because the cap was hit would strand the
    lease it was trying to release."""
    assert checkout(client).status_code == 200
    set_caps(client, soft_cap_usd=10)
    report(client, 50)
    r = client.post("/items/issue_1/release", headers=_agent(),
                    json={"owner": "co-library"})
    assert r.status_code == 200, r.text


def test_a_scoped_caller_neither_sees_nor_counts_another_products_ledger(client):
    """Scoped in SQL, so `total` cannot count rows the caller may not see.

    `_scope_clause`'s docstring names this failure directly, and the first
    version of this route had it: the filter ran in Python after the LIMIT, so
    `total` reported every ledger in the vault and a page came back shorter than
    the `count` a client pages by.
    """
    set_caps(client, scope_id="library", soft_cap_usd=160)
    set_caps(client, scope_id="vivy", soft_cap_usd=80)
    wide = client.get("/ledgers", headers=_agent()).json()
    assert wide["total"] == 2
    assert {r["scope_id"] for r in wide["ledgers"]} == {"library", "vivy"}

    narrow = client.get(
        "/ledgers", headers={**H, "Authorization": f"Bearer {NARROW}"}).json()
    assert narrow["total"] == 1
    assert narrow["count"] == 1
    assert [r["scope_id"] for r in narrow["ledgers"]] == ["vivy"]


# --- crossing a threshold is a reported signal, once per day -------------------

def test_crossing_the_warning_threshold_writes_exactly_one_signal_for_the_day(client):
    set_caps(client, warn_usd=10)
    first = report(client, 6)
    assert first.json()["crossed"] is None
    second = report(client, 6)
    assert second.json()["crossed"] == "warn"
    third = report(client, 1)
    assert third.json()["crossed"] is None

    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE kind = 'ledger.threshold-crossed'"
        ).fetchone()
        count = rows["c"]
    finally:
        conn.close()
    assert count == 1


def test_crossing_the_soft_cap_reports_soft_cap_not_warn(client):
    set_caps(client, warn_usd=5, soft_cap_usd=10)
    r = report(client, 12)
    assert r.json()["crossed"] == "soft_cap"


# --- void ------------------------------------------------------------------------

def test_a_void_corrects_the_total(client):
    set_caps(client, soft_cap_usd=160)
    entry_id = report(client, 5).json()["id"]
    report(client, 2)
    r = void(client, entry_id, reason="typo")
    assert r.status_code == 200, r.text
    assert r.json()["already_voided"] is False
    assert r.json()["total_usd"] == 2
    row = client.get("/ledgers", headers=_agent()).json()["ledgers"][0]
    assert row["total_usd"] == 2


def test_a_repeated_void_does_not_double_subtract(client):
    set_caps(client, soft_cap_usd=160)
    entry_id = report(client, 5).json()["id"]
    void(client, entry_id, reason="typo")
    r = void(client, entry_id, reason="typo again")
    assert r.status_code == 200
    assert r.json()["already_voided"] is True
    row = client.get("/ledgers", headers=_agent()).json()["ledgers"][0]
    assert row["total_usd"] == 0


def test_an_agent_cannot_void_its_own_spend(client):
    """The same self-widening risk `ledger.grant` closes: an agent able to
    void its own spend could spend past a cap and erase the evidence."""
    set_caps(client, soft_cap_usd=160)
    entry_id = report(client, 5).json()["id"]
    r = client.post(f"/ledger-entries/{entry_id}/void", headers=_agent(),
                    json={"acting_human": "operator"})
    assert r.status_code == 403
    assert "ledger.void" in r.json()["error"]


def test_approved_client_is_admitted_to_void(client):
    set_caps(client, soft_cap_usd=160)
    entry_id = report(client, 5).json()["id"]
    r = void(client, entry_id)
    assert r.status_code == 200, r.text


def test_voiding_an_unknown_entry_is_not_found(client):
    r = void(client, "lde_nonexistent")
    assert r.status_code == 404


def test_a_void_without_an_attested_human_is_refused(client):
    set_caps(client, soft_cap_usd=160)
    entry_id = report(client, 5).json()["id"]
    r = client.post(f"/ledger-entries/{entry_id}/void", headers=_orient(),
                    json={})
    assert r.status_code == 403
    assert "acting_human" in r.json()["error"]


# --- what the reviewers found under the draw-down model, still true -----------

@pytest.mark.parametrize("literal", ["Infinity", "NaN"])
def test_a_non_finite_amount_is_refused_rather_than_crashing(client, literal):
    """Starlette parses bodies with the stdlib `json.loads`, which accepts the
    non-standard `Infinity`/`NaN` literals.

    Both walked past `usd < 0` (False for each) into `round()`, where
    `round(inf)` raises `OverflowError` — not a `ValueError`, so caught by
    nothing — and returned a bare 500 to any agent-role caller.

    `-Infinity` is NOT parametrized here because it was never the hole: the
    negativity check refused it correctly all along, which is the kind of detail
    worth pinning so a later reader does not "fix" a case that already worked.
    """
    set_caps(client, soft_cap_usd=160)
    r = client.post(
        "/ledger-entries",
        headers={**_agent(), "Content-Type": "application/json"},
        content=f'{{"product":"library","source":"agentctl","usd":{literal}}}')
    assert r.status_code == 400, r.text
    assert "finite" in r.json()["error"]


def test_a_non_finite_cap_is_refused_rather_than_crashing(client):
    r = client.put("/ledgers",
                   headers={**_orient(), "Content-Type": "application/json"},
                   content='{"scope_kind":"project","scope_id":"library",'
                           '"soft_cap_usd":Infinity,"acting_human":"operator"}')
    assert r.status_code == 400
    assert "finite" in r.json()["error"]


def test_an_absurd_spend_cannot_stop_every_agent_on_a_product(client):
    """The denial of service this cap exists to stop.

    Spend deliberately never refuses on a cap, so before the ceiling a single
    call reporting an absurd amount drove a product's ledger deeply past every
    threshold and put every other agent on that product into the capped
    refusal until local midnight. `ledger.spend` is held by every agent-role
    credential, so that was a fleet-wide stop for one product from one
    request — on the exact gate this work exists to build.
    """
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1e15)
    assert r.status_code == 400
    assert "larger than this vault will record" in r.json()["error"]
    # And the gate still opens, which is the half that matters.
    assert checkout(client).status_code == 200


def test_a_spend_that_would_overflow_the_column_is_refused_not_a_500(client):
    """A finite value can still exceed SQLite's 64-bit INTEGER on the way in and
    raise `OverflowError` from inside the INSERT — the same 500 by another
    road."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1e20)
    assert r.status_code == 400, r.text


def test_an_oversized_identifier_is_refused(client):
    """A SQLite `String(100)` is type affinity and enforces no length, so
    without this an agent-role credential writes an arbitrarily large string
    into any of these columns."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, agent="x" * 5000)
    assert r.status_code == 400
    assert "longer than" in r.json()["error"]


def test_a_workstream_grant_is_refused_rather_than_silently_unenforced(client):
    """Nothing resolves a work item to a workstream, so a workstream cap set
    would be stored and enforced by nothing — an operator would reasonably
    believe it was live. Refusing is the reversible direction."""
    r = client.put("/ledgers", headers=_orient(), json={
        "scope_kind": "workstream", "scope_id": "keel",
        "soft_cap_usd": 10, "acting_human": "operator"})
    assert r.status_code == 400
    assert r.json()["supported"] == ["project"]


# --- the proportionate bound ----------------------------------------------------

def test_a_report_out_of_all_proportion_to_the_soft_cap_is_refused(client):
    """The denial of service the GLOBAL cap did not close.

    `MAX_LEDGER_USD` is sized for overflow and typos ($1,000,000) while real
    caps here are $10-$160 a day, so a call well inside the global cap could
    still report four orders of magnitude more than the cap it draws against
    and refuse every checkout on that product until local midnight. Any
    agent-role credential holds `ledger.spend`, so that was one authorized call
    away (security-reviewer, PR #50 round 2, on the draw-down model's
    equivalent bound).
    """
    set_caps(client, soft_cap_usd=100)
    r = report(client, 50_000)          # inside the $1M global cap, 500x the cap
    assert r.status_code == 422, r.text
    assert r.json()["maximum_single_report_usd"] == 1000
    assert r.json()["basis"] == "soft_cap"
    # The gate still opens, which is the property that matters.
    assert checkout(client).status_code == 200


def test_a_genuine_overrun_still_records(client):
    """The bound must not refuse the case the ledger exists to capture.

    Two runs bypassing checkout each spend against one cap, so an overrun is
    legitimate and expected. A bound at 1x the cap would refuse exactly those
    real overruns — the failure this ledger exists to avoid, arriving from the
    other direction. The slack is why the multiple is 10 and not 1.
    """
    set_caps(client, soft_cap_usd=100)
    r = report(client, 250)             # 2.5x the cap: a real overrun
    assert r.status_code == 200, r.text
    row = client.get("/ledgers", headers=_agent()).json()["ledgers"][0]
    assert row["total_usd"] == 250
    # And it does what an overrun should: closes the gate.
    assert checkout(client).status_code == 409


def test_the_proportionate_bound_scales_with_the_soft_cap(client):
    """It is a ratio, not a constant — a bigger cap legitimately absorbs a
    bigger single report."""
    set_caps(client, soft_cap_usd=100)
    assert report(client, 1500).status_code == 422
    set_caps(client, soft_cap_usd=1000)                 # re-set, same ledger
    assert report(client, 1500).status_code == 200


def test_a_replay_still_answers_with_the_recorded_row_after_the_cap_drops(client):
    """The idempotent-retry flip, reproduced before it was fixed under the
    draw-down model and still true here.

    An identical replay of an already-recorded report must not be refused
    after the cap is lowered — the caller told its report was rejected while
    that exact entry sits in the store unchanged.
    """
    set_caps(client, soft_cap_usd=1000)
    first = report(client, 5000, dedup_key="run-1:msg-1:req-1")
    assert first.status_code == 200, first.text

    set_caps(client, soft_cap_usd=100)      # ceiling falls from $10,000 to $1,000
    replay = report(client, 5000, dedup_key="run-1:msg-1:req-1")
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["id"] == first.json()["id"]

    # A genuinely NEW report of that size is still refused — the replay path is
    # the exception, not a hole in the bound.
    fresh = report(client, 5000, dedup_key="run-1:msg-2:req-1")
    assert fresh.status_code == 422


def test_the_bound_admits_exactly_its_multiple(client):
    """The boundary itself, which neither the refusal nor the overrun test
    pinned."""
    set_caps(client, soft_cap_usd=100)
    assert report(client, 1000).status_code == 200      # exactly 10x
    assert report(client, 1000.01).status_code == 422   # a hair over


def test_a_zero_cap_still_records_the_spend_it_did_not_authorise(client):
    """A cap of zero has no ratio in the old model's sense, but this bound
    falls through to the flat uncapped ceiling rather than to "no bound at
    all" — a real spend under the $1,000 ceiling still records.

    A zeroed cap is a deliberate freeze and the freeze already works without
    help here: `ledger_state` reports `soft_capped` from the first dollar, so
    checkout refuses. Money spent anyway is still real and still belongs in
    the record.
    """
    set_caps(client, soft_cap_usd=0)
    r = report(client, 25)
    assert r.status_code == 200, r.text
    row = client.get("/ledgers", headers=_agent()).json()["ledgers"][0]
    assert row["total_usd"] == 25
    assert row["state"] == "soft_capped"
    # The freeze is intact, and it comes from ledger_state rather than from
    # the proportionate bound.
    assert checkout(client).status_code == 409


def test_an_uncapped_project_is_bound_by_the_flat_ceiling(client):
    """The uncapped ledger case built by hand through `PUT /ledgers` with all
    three caps unset — `test_spend_for_an_uncapped_scope_is_recorded` and
    `test_a_repeated_report_for_an_uncapped_scope_is_idempotent` cover the
    same bound where `ensure_live_ledger` built the row instead."""
    set_caps(client)
    r = report(client, 999)
    assert r.status_code == 200, r.text
    over = report(client, 1001, dedup_key="over")
    assert over.status_code == 422
    assert over.json()["basis"] == "uncapped"
    assert over.json()["maximum_single_report_usd"] == 1000


def test_a_ledger_ensure_live_ledger_builds_is_bound_by_the_flat_ceiling_too(client):
    """The same bound, against the ledger row `ensure_live_ledger` built on
    the first report rather than one a human set up in advance — nothing
    about where the row came from should change what it compares against."""
    r = report(client, 999)
    assert r.status_code == 200, r.text
    over = report(client, 1001, dedup_key="over")
    assert over.status_code == 422
    assert over.json()["basis"] == "uncapped"
    assert over.json()["maximum_single_report_usd"] == 1000


# --- GET /ledger-entries ------------------------------------------------

def entries(client, headers=None, **params):
    return client.get("/ledger-entries", headers=headers or _agent(), params=params)


def test_the_happy_path_returns_entries_for_a_scoped_caller(client):
    set_caps(client, scope_id="vivy", soft_cap_usd=160)
    report(client, 1.5, product="vivy")
    report(client, 2.5, product="vivy")
    r = entries(client, headers={**H, "Authorization": f"Bearer {NARROW}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["total"] == 2
    assert {e["usd"] for e in body["entries"]} == {1.5, 2.5}


def test_a_scoped_caller_neither_sees_nor_counts_another_products_entries(client):
    """Scoped in SQL, so `total` cannot count rows the caller may not see, the
    same failure `_scope_clause`'s docstring names for every list in this file."""
    set_caps(client, scope_id="library", soft_cap_usd=160)
    set_caps(client, scope_id="vivy", soft_cap_usd=80)
    report(client, 1)
    report(client, 2, product="vivy")
    wide = entries(client).json()
    assert wide["total"] == 2

    narrow = entries(
        client, headers={**H, "Authorization": f"Bearer {NARROW}"}).json()
    assert narrow["total"] == 1
    assert narrow["count"] == 1
    assert narrow["entries"][0]["product"] == "vivy"


def test_the_product_filter_narrows(client):
    set_caps(client, scope_id="library", soft_cap_usd=160)
    set_caps(client, scope_id="vivy", soft_cap_usd=80)
    report(client, 1)
    report(client, 2, product="vivy")
    r = entries(client, product="vivy")
    assert r.json()["total"] == 1
    assert r.json()["entries"][0]["product"] == "vivy"


def test_the_day_filter_narrows(client):
    import datetime as dt
    set_caps(client, soft_cap_usd=160)
    today = libapp.local_day()
    yesterday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    report(client, 1, day=yesterday)
    report(client, 2, day=today)
    r = entries(client, day=today)
    assert r.json()["total"] == 1
    assert r.json()["entries"][0]["day"] == today


def test_the_from_to_range_narrows(client):
    import datetime as dt
    set_caps(client, soft_cap_usd=160)
    today = libapp.local_day()
    yesterday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    tomorrow = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()
    report(client, 1, day=yesterday)
    report(client, 2, day=today)
    report(client, 3, day=tomorrow)
    r = entries(client, **{"from": today, "to": tomorrow})
    assert r.json()["total"] == 2
    assert {e["day"] for e in r.json()["entries"]} == {today, tomorrow}


def test_the_source_filter_narrows(client):
    set_caps(client, soft_cap_usd=160)
    report(client, 1, source="agentctl")
    report(client, 2, source="app")
    r = entries(client, source="app")
    assert r.json()["total"] == 1
    assert r.json()["entries"][0]["source"] == "app"


def test_the_run_id_filter_narrows(client):
    set_caps(client, soft_cap_usd=160)
    report(client, 1, run_id="run-a")
    report(client, 2, run_id="run-b")
    r = entries(client, run_id="run-a")
    assert r.json()["total"] == 1
    assert r.json()["entries"][0]["run_id"] == "run-a"


def test_the_item_id_filter_narrows(client):
    set_caps(client, soft_cap_usd=160)
    report(client, 1, item_id="issue_1")
    report(client, 2)
    r = entries(client, item_id="issue_1")
    assert r.json()["total"] == 1
    assert r.json()["entries"][0]["item_id"] == "issue_1"


# --- epic_id, derived from the work item, never accepted from the caller -------

def insert_item(item_id, item_type="bug", parent_id=None, product="library"):
    conn = store.connect_rw()
    try:
        conn.execute(
            "INSERT INTO issue_items (id, product, title, body, refs, severity, "
            " state, created_by, created_at, type, needs, parent_id) "
            "VALUES (?,?,'a thing','b','{}','medium','open',"
            " 'someone','2026-08-23 00:00:00',?,'agent',?)",
            (item_id, product, item_type, parent_id))
        conn.commit()
    finally:
        conn.close()


def test_spend_with_no_item_id_has_no_epic(client):
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1)
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] is None
    assert entries(client).json()["entries"][0]["epic_id"] is None


def test_spend_against_an_epic_itself_is_its_own_epic(client):
    insert_item("issue_epic", item_type="epic")
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, item_id="issue_epic")
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] == "issue_epic"
    assert entries(client).json()["entries"][0]["epic_id"] == "issue_epic"


def test_spend_against_a_sub_item_carries_its_parents_epic(client):
    insert_item("issue_epic", item_type="epic")
    insert_item("issue_sub", item_type="task", parent_id="issue_epic")
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, item_id="issue_sub")
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] == "issue_epic"
    assert entries(client).json()["entries"][0]["epic_id"] == "issue_epic"


def test_spend_against_a_non_epic_with_no_queue_parent_has_no_epic(client):
    """`issue_1`, the fixture's own item, is inserted directly rather than
    through `insert_item` and so has no parent at all — the shape `GET
    /items?home=invalid` surfaces now that every ordinary write path gives a
    non-epic row its product's corrections queue
    (`library/specs/one-home-per-work-item`, PR 2). A dangling reference
    derives nothing, the same answer a missing `item_id` gets."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, item_id="issue_1")
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] is None
    assert entries(client).json()["entries"][0]["epic_id"] is None


def test_spend_against_a_correction_records_its_queue_as_the_epic(client):
    """The ordinary shape once a non-epic's `parent_id` names its product's
    corrections queue — `epic_id` carries that queue id, the same way it
    carries a sub-item's epic."""
    conn = store.connect_rw()
    conn.execute(
        "INSERT INTO corrections_queues (id, product, created_at) "
        "VALUES ('cq_library', 'library', '2026-08-23 00:00:00')")
    conn.execute(
        "UPDATE issue_items SET parent_id = 'cq_library' WHERE id = 'issue_1'")
    conn.commit()
    conn.close()
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, item_id="issue_1")
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] == "cq_library"
    assert entries(client).json()["entries"][0]["epic_id"] == "cq_library"


def test_spend_against_an_item_this_vault_does_not_hold_has_no_epic(client):
    """A dangling `item_id` derives nothing, and is not refused — `record_spend`
    never refuses a spend that already happened."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, item_id="issue_nonexistent")
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] is None
    assert entries(client).json()["entries"][0]["epic_id"] is None


# An item that exists, but in a different product than the entry reporting
# against it, must be indistinguishable from a missing one — the disclosure
# `_validate_parent` was hardened against (security review, PR #167 round 1):
# a caller scoped to one product must not learn, via `epic_id`, whether an id
# from another product exists, is an epic, or has a parent.

def test_spend_against_a_cross_product_epic_has_no_epic(client):
    insert_item("issue_other_epic", item_type="epic", product="vivy")
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, item_id="issue_other_epic")
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] is None
    assert entries(client).json()["entries"][0]["epic_id"] is None


def test_spend_against_a_cross_product_sub_item_has_no_epic(client):
    insert_item("issue_other_epic", item_type="epic", product="vivy")
    insert_item("issue_other_sub", item_type="task",
                parent_id="issue_other_epic", product="vivy")
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, item_id="issue_other_sub")
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] is None
    assert entries(client).json()["entries"][0]["epic_id"] is None


def test_spend_against_a_cross_product_parentless_item_has_no_epic(client):
    insert_item("issue_other_bug", item_type="bug", product="vivy")
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, item_id="issue_other_bug")
    assert r.status_code == 200, r.text
    assert r.json()["epic_id"] is None
    assert entries(client).json()["entries"][0]["epic_id"] is None


def test_the_epic_id_filter_narrows(client):
    insert_item("issue_epic", item_type="epic")
    insert_item("issue_sub", item_type="task", parent_id="issue_epic")
    set_caps(client, soft_cap_usd=160)
    report(client, 1, item_id="issue_sub")
    report(client, 2, item_id="issue_1")             # has no epic at all
    r = entries(client, epic_id="issue_epic")
    assert r.json()["total"] == 1
    assert r.json()["entries"][0]["epic_id"] == "issue_epic"


def test_the_epic_id_filter_narrows_to_a_corrections_queue(client):
    conn = store.connect_rw()
    conn.execute(
        "INSERT INTO corrections_queues (id, product, created_at) "
        "VALUES ('cq_library', 'library', '2026-08-23 00:00:00')")
    conn.execute(
        "UPDATE issue_items SET parent_id = 'cq_library' WHERE id = 'issue_1'")
    conn.commit()
    conn.close()
    insert_item("issue_epic", item_type="epic")
    insert_item("issue_sub", item_type="task", parent_id="issue_epic")
    set_caps(client, soft_cap_usd=160)
    report(client, 1, item_id="issue_sub")
    report(client, 2, item_id="issue_1")             # files under its queue
    r = entries(client, epic_id="cq_library")
    assert r.json()["total"] == 1
    assert r.json()["entries"][0]["epic_id"] == "cq_library"


def test_a_caller_supplied_epic_id_is_refused_as_an_unrecognised_key(client):
    """`epic_id` is derived, never accepted — the unknown-key refusal names it
    exactly as it would any other invented attribution dimension."""
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, epic_id="issue_epic")
    assert r.status_code == 400, r.text
    assert "epic_id" in r.json()["error"]


def test_a_caller_supplied_epic_field_is_refused_as_an_unrecognised_key(client):
    set_caps(client, soft_cap_usd=160)
    r = report(client, 1, epic="issue_epic")
    assert r.status_code == 400, r.text
    assert "epic" in r.json()["error"]


@pytest.mark.parametrize("field", ["day", "from", "to"])
def test_a_malformed_day_filter_is_refused_not_ignored(client, field):
    """Silently dropping a bad filter widens the result set, which is the wrong
    direction for a route that exists to answer "what did we spend on"."""
    set_caps(client, soft_cap_usd=160)
    report(client, 1)
    r = entries(client, **{field: "not-a-day"})
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["error"]


def test_an_unrecognised_source_is_refused_naming_the_supported_set(client):
    set_caps(client, soft_cap_usd=160)
    report(client, 1)
    r = entries(client, source="carrier-pigeon")
    assert r.status_code == 400
    assert r.json()["supported"] == sorted(store.LEDGER_SOURCES)


def test_usd_comes_back_in_dollars_not_micro_dollars(client):
    set_caps(client, soft_cap_usd=160)
    report(client, 1.5)
    assert entries(client).json()["entries"][0]["usd"] == 1.5


def test_person_team_and_dedup_key_are_absent_from_every_row(client):
    """The deferred person axis (ADR-0021) and a write-side idempotency key —
    withheld rather than served, the reversible direction."""
    set_caps(client, soft_cap_usd=160)
    report(client, 1, person="operator", team="platform",
           dedup_key="run-1:msg-1:req-1")
    row = entries(client).json()["entries"][0]
    assert "person" not in row
    assert "team" not in row
    assert "dedup_key" not in row


def test_voided_at_is_present_and_null_on_a_live_entry(client):
    set_caps(client, soft_cap_usd=160)
    report(client, 1)
    row = entries(client).json()["entries"][0]
    assert "voided_at" in row
    assert row["voided_at"] is None


def test_voided_at_is_set_after_a_void(client):
    set_caps(client, soft_cap_usd=160)
    entry_id = report(client, 1).json()["id"]
    void(client, entry_id)
    row = entries(client).json()["entries"][0]
    assert row["voided_at"] is not None


def test_voided_by_is_never_served(client):
    """Who voided is the same undecided person-disclosure question as
    `person` — withheld for the same reason."""
    set_caps(client, soft_cap_usd=160)
    entry_id = report(client, 1).json()["id"]
    void(client, entry_id)
    row = entries(client).json()["entries"][0]
    assert "voided_by" not in row


def test_a_column_added_to_ledger_entries_does_not_reach_a_client_by_itself(client):
    """`ledger_entry_dict` is built from a named field list precisely so this
    stays true — the same discipline `GET /runs` holds for its own table."""
    set_caps(client, soft_cap_usd=160)
    report(client, 1)
    import sqlite3
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "ALTER TABLE ledger_entries ADD COLUMN a_new_field TEXT DEFAULT 'secret'")
    conn.commit()
    conn.close()
    row = entries(client).json()["entries"][0]
    assert "a_new_field" not in row


# --- the migration -----------------------------------------------------------

def test_the_migrations_upgrade_moves_a_grant_into_the_soft_cap(tmp_path):
    import sqlite3

    db = tmp_path / "migration.db"
    up_to = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "f71949b78df4"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert up_to.returncode == 0, up_to.stderr

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ledgers (id, scope_kind, scope_id, "
        " daily_granted_usd_micro, state, granted_by, created_at, updated_at) "
        "VALUES ('ldg_x','project','library',42000000,'live','operator',"
        " '2026-09-02 00:00:00','2026-09-02 00:00:00')")
    conn.commit()
    conn.close()

    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert up.returncode == 0, up.stderr

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT warn_usd_micro, soft_cap_usd_micro, hard_cap_usd_micro "
        "FROM ledgers WHERE id = 'ldg_x'").fetchone()
    conn.close()
    assert row["soft_cap_usd_micro"] == 42_000_000
    assert row["warn_usd_micro"] is None
    assert row["hard_cap_usd_micro"] is None

    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "f71949b78df4"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert down.returncode == 0, down.stderr

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT daily_granted_usd_micro FROM ledgers WHERE id = 'ldg_x'").fetchone()
    conn.close()
    assert row["daily_granted_usd_micro"] == 42_000_000


def test_the_migrations_downgrade_survives_every_cap_set_this_pr_makes_legal(tmp_path):
    """The row shape the round above does not cover: a ledger with no soft
    cap, which is legal and common the moment this PR ships (uncapped is the
    stated default, and a warn-only or hard-cap-only cap set is explicitly
    legal — `test_all_three_caps_absent_is_a_legal_uncapped_ledger`).

    `downgrade()`'s batch recreate makes `daily_granted_usd_micro NOT NULL`,
    so any of these four rows left it NULL before the COALESCE fallback was
    added and crashed SQLite's batch mode with `NOT NULL constraint failed`
    (reviewer, PR #77 round 1).
    """
    import sqlite3

    db = tmp_path / "migration.db"
    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert up.returncode == 0, up.stderr

    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO ledgers (id, scope_kind, scope_id, state, granted_by, "
        " created_at, updated_at, warn_usd_micro, soft_cap_usd_micro, "
        " hard_cap_usd_micro) "
        "VALUES (?,'project',?,'live','operator','2026-09-02 00:00:00',"
        " '2026-09-02 00:00:00',?,?,?)",
        [
            ("ldg_soft", "soft-set", None, 100_000_000, None),
            ("ldg_hard", "hard-only", None, None, 200_000_000),
            ("ldg_warn", "warn-only", 5_000_000, None, None),
            ("ldg_bare", "all-null", None, None, None),
        ])
    conn.commit()
    conn.close()

    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "f71949b78df4"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert down.returncode == 0, down.stderr

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: r["daily_granted_usd_micro"] for r in conn.execute(
        "SELECT id, daily_granted_usd_micro FROM ledgers "
        "WHERE id IN ('ldg_soft','ldg_hard','ldg_warn','ldg_bare')")}
    conn.close()
    assert rows["ldg_soft"] == 100_000_000
    assert rows["ldg_hard"] == 200_000_000
    assert rows["ldg_warn"] == store.MAX_LEDGER_USD_MICRO
    assert rows["ldg_bare"] == store.MAX_LEDGER_USD_MICRO


def test_the_epic_id_migration_backfills_every_branch_of_the_rule(tmp_path):
    """The same four-way rule `record_spend` applies going forward, applied
    once in SQL to rows that predate the column."""
    import sqlite3

    db = tmp_path / "migration.db"
    up_to = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "7a43066dc44d"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert up_to.returncode == 0, up_to.stderr

    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        " state, created_by, created_at, type, needs, parent_id) "
        "VALUES (?,?,'a thing','b','{}','medium','open','someone',"
        " '2026-08-23 00:00:00',?,'agent',?)",
        [
            ("i_epic", "library", "epic", None),
            ("i_sub", "library", "task", "i_epic"),
            ("i_corr", "library", "bug", None),
            # A different product's own epic, same id shape as any other —
            # the row an out-of-product `item_id` would otherwise resolve
            # against.
            ("i_other_epic", "vivy", "epic", None),
        ])
    conn.executemany(
        "INSERT INTO ledger_entries (id, ledger_id, day, usd_micro, at, "
        " ingested_at, source, product, reported_by, item_id) "
        "VALUES (?,'ldg_x','2026-08-23',1000000,'2026-08-23 00:00:00',"
        " '2026-08-23 00:00:00','agentctl','library','agentctl',?)",
        [
            ("lde_none", None),
            ("lde_epic", "i_epic"),
            ("lde_sub", "i_sub"),
            ("lde_corr", "i_corr"),
            ("lde_dangling", "i_missing"),
            # `library`'s own entry naming `vivy`'s epic: cross-product, and
            # required to backfill to NULL exactly as `lde_dangling` does.
            ("lde_cross_product", "i_other_epic"),
        ])
    conn.commit()
    conn.close()

    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert up.returncode == 0, up.stderr

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: r["epic_id"] for r in conn.execute(
        "SELECT id, epic_id FROM ledger_entries")}
    # **"head" NOW ALSO CARRIES `e18a2c4f9b06`** (PR 2 of `library/specs/
    # one-home-per-work-item`), which runs after `d12fde7d50e9` backfilled
    # `lde_corr` to the literal `'corrections'` here and turns that literal
    # into `library`'s own corrections queue id — the same queue `i_corr`
    # itself is backfilled to.
    queue_id = conn.execute(
        "SELECT id FROM corrections_queues WHERE product = 'library'"
    ).fetchone()[0]
    conn.close()
    assert rows["lde_none"] is None
    assert rows["lde_epic"] == "i_epic"
    assert rows["lde_sub"] == "i_epic"
    assert rows["lde_corr"] == queue_id
    assert rows["lde_dangling"] is None
    assert rows["lde_cross_product"] is None

    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "7a43066dc44d"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert down.returncode == 0, down.stderr

    conn = sqlite3.connect(db)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(ledger_entries)")}
    conn.close()
    assert "epic_id" not in columns


def test_the_per_run_cap_migration_is_additive_and_nullable(tmp_path):
    """One nullable column, matching `80ab55fe0dba`'s shape for the lane
    column: an existing row gets NULL on the way up, and the column is gone
    on the way back down."""
    import sqlite3

    db = tmp_path / "migration.db"
    up_to = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "80ab55fe0dba"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert up_to.returncode == 0, up_to.stderr

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ledgers (id, scope_kind, scope_id, state, granted_by, "
        " created_at, updated_at, warn_usd_micro, soft_cap_usd_micro, "
        " hard_cap_usd_micro) "
        "VALUES ('ldg_x','project','library','live','operator',"
        " '2026-10-07 00:00:00','2026-10-07 00:00:00',NULL,10000000,NULL)")
    conn.commit()
    conn.close()

    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert up.returncode == 0, up.stderr

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT per_run_cap_usd_micro FROM ledgers WHERE id = 'ldg_x'").fetchone()
    conn.close()
    assert row["per_run_cap_usd_micro"] is None

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE ledgers SET per_run_cap_usd_micro = 1000000 WHERE id = 'ldg_x'")
    conn.commit()
    conn.close()

    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "80ab55fe0dba"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert down.returncode == 0, down.stderr

    conn = sqlite3.connect(db)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(ledgers)")}
    # THE REST OF THE ROW SURVIVES THE ROUND TRIP, not only the new column's
    # absence — a batch recreate that dropped or reordered another column
    # would pass a test that checked only for `per_run_cap_usd_micro`.
    row = conn.execute(
        "SELECT soft_cap_usd_micro FROM ledgers WHERE id = 'ldg_x'").fetchone()
    conn.close()
    assert "per_run_cap_usd_micro" not in columns
    assert row[0] == 10_000_000
