"""What `/items` answers when it is asked to narrow.

**Two filters were wrong in opposite ways, and the wider one was worse.**
`state=claimable` matched the column, and no row holds that string, so it
answered 0 against a queue of 258 — a narrower answer, which reads as a failure.
`needs` was not read at all, so `?needs=operator` returned every open item as
though nothing had been asked — 275 rows where Orient returns 2, which reads as a
working call. Both were measured against the two live services rather than
inferred (`issue_01M06JCN1Y99SNSN7EXAKN3DJ4`).

**Neither was caught by the read-parity shadow**, which compared the two services
on the queries it was given rather than on the ones the clients send. These tests
are the queries the clients send.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import lease
import protocol
import store

TOKEN = "t-agent"


def _at(**delta) -> str:
    """A stored timestamp `delta` before now, in the shape the vault stores."""
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    return (now - _dt.timedelta(**delta)).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT, created_by TEXT, created_at TEXT, run_id TEXT, type TEXT,
            created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT);
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE corrections_queues (
            id TEXT PRIMARY KEY, product TEXT UNIQUE, created_at TEXT);
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({TOKEN: {"name": "drain", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


def _h():
    return {"Authorization": f"Bearer {TOKEN}",
            protocol.HEADER: str(protocol.CURRENT)}


def seed(item_id, **cols):
    """One item, with only the columns a case cares about."""
    cols.setdefault("product", "keel")
    cols.setdefault("title", item_id)
    cols.setdefault("type", None)
    cols.setdefault("state", "open")
    cols.setdefault("created_at", "2026-08-01 12:00:00.000000")
    conn = sqlite3.connect(store.DB_PATH)
    names = ", ".join(["id", *cols])
    conn.execute(f"INSERT INTO issue_items ({names}) "
                 f"VALUES ({','.join('?' * (len(cols) + 1))})",
                 [item_id, *cols.values()])
    conn.commit()
    conn.close()


def lease_on(item_id, *, taken, beat, released=None):
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at, released_at) VALUES (?,?,?,?,?,?,?)",
        (f"lease_{item_id}", item_id, item_id, "run-1", taken, beat, released))
    conn.commit()
    conn.close()


def listed(client, query) -> set[str]:
    r = client.get(f"/items?{query}", headers=_h())
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


# --- claimable, which returned nothing at all ------------------------------------

def test_an_open_item_is_claimable(client):
    seed("open_1")
    assert listed(client, "state=claimable") == {"open_1"}


def test_a_resolved_or_pr_open_item_is_not(client):
    seed("open_1")
    seed("fixed_1", state="fixed")
    seed("pr_1", state=store.ITEM_STATE_PR_OPEN)
    assert listed(client, "state=claimable") == {"open_1"}


def test_an_operator_item_is_never_claimable(client):
    """A drain that takes one cannot finish it, and the item then sits claimed
    with no outcome — invisible to the next pass for a full TTL, then claimed and
    abandoned again."""
    seed("open_1")
    seed("operator_1", needs="operator")
    assert listed(client, "state=claimable") == {"open_1"}


def test_a_pre_column_item_is_still_claimable(client):
    """**`needs IS NULL` is required rather than decorative.** NULL is a row
    predating the column and reads as the `agent` default everywhere else, and
    SQL's three-valued logic makes `!= 'operator'` false for NULL — so without the
    null check every pre-column item would silently vanish from the drain's
    queue."""
    seed("legacy_1", needs=None)
    assert listed(client, "state=claimable") == {"legacy_1"}


# --- claimable and the claim's age ----------------------------------------------

def test_a_freshly_claimed_item_is_not_claimable(client):
    seed("held_1", state="claimed", claimed_by="run-1", claimed_at=_at(minutes=5))
    assert listed(client, "state=claimable") == set()


def test_a_claim_past_the_ttl_with_no_lease_is_claimable(client):
    """The fallback for rows predating checkout: with no lease governing the
    item, the 24-hour TTL decides."""
    seed("stale_1", state="claimed", claimed_by="run-1",
         claimed_at=_at(hours=lease.CLAIM_TTL.total_seconds() / 3600 + 1))
    assert listed(client, "state=claimable") == {"stale_1"}


def test_a_live_lease_holds_the_item_even_past_the_ttl(client):
    """**Where a lease exists it is the authority**, which is the whole of
    `claim_status`'s reasoning carried into SQL: one act must not have two
    expiries. A claim older than the TTL whose lease is still beating is held."""
    seed("leased_1", state="claimed", claimed_by="run-1",
         claimed_at=_at(hours=lease.CLAIM_TTL.total_seconds() / 3600 + 1))
    lease_on("leased_1", taken=_at(minutes=10), beat=_at(seconds=5))
    assert listed(client, "state=claimable") == set()


def test_a_dead_lease_releases_the_item_before_the_ttl(client):
    """And the authority runs the other way too: a lease whose heartbeat has
    lapsed makes the item claimable even though the 24-hour TTL has not run."""
    seed("dead_1", state="claimed", claimed_by="run-1", claimed_at=_at(minutes=30))
    lease_on("dead_1", taken=_at(minutes=30),
             beat=_at(seconds=lease.grace().total_seconds() + 60))
    assert listed(client, "state=claimable") == {"dead_1"}


def test_a_released_lease_does_not_hold_anything(client):
    """A released row is history. Only a live lease governs, which is what
    `released_at IS NULL` says in both halves of this rule."""
    seed("freed_1", state="claimed", claimed_by="run-1",
         claimed_at=_at(hours=lease.CLAIM_TTL.total_seconds() / 3600 + 1))
    lease_on("freed_1", taken=_at(minutes=10), beat=_at(seconds=5),
             released=_at(seconds=1))
    assert listed(client, "state=claimable") == {"freed_1"}


# --- needs, which was dropped on the floor --------------------------------------

def test_needs_narrows_the_listing(client):
    seed("agent_1", needs="agent")
    seed("operator_1", needs="operator")
    seed("either_1", needs="either")
    assert listed(client, "needs=operator") == {"operator_1"}
    assert listed(client, "needs=either") == {"either_1"}


def test_asking_for_the_default_matches_rows_predating_the_column(client):
    """The default is stored as NULL on pre-column rows, so asking for it has to
    match both spellings or the filter hides history."""
    seed("agent_1", needs="agent")
    seed("legacy_1", needs=None)
    seed("operator_1", needs="operator")
    assert listed(client, "needs=agent") == {"agent_1", "legacy_1"}


def test_needs_composes_with_state(client):
    """The query `issues-waiting` and `issue-queue-drain` actually send."""
    seed("open_op", state="open", needs="operator")
    seed("open_agent", state="open", needs="agent")
    seed("fixed_op", state="fixed", needs="operator")
    assert listed(client, "state=open&needs=operator") == {"open_op"}


def test_needs_composes_with_claimable(client):
    """`claimable` already excludes `operator`, so asking for both is a narrower
    question rather than a contradictory one."""
    seed("agent_1", needs="agent")
    seed("either_1", needs="either")
    assert listed(client, "state=claimable&needs=either") == {"either_1"}


def test_an_unknown_needs_is_refused(client):
    """**Refused rather than ignored**, which is the failure this route had: an
    unrecognised filter answered 200 with every row, and a caller reads that as
    the filter having been applied."""
    r = client.get("/items?needs=nonsense", headers=_h())
    assert r.status_code == 400, "400 is what this service returns for a query " \
        "parameter outside its set — the contract reserves 422 for the revision " \
        "route's structural validation"
    assert "needs" in r.json()["error"]


# --- type, which is epic or nothing ----------------------------------------------

def test_type_epic_narrows_the_listing(client):
    seed("epic_1", type="epic")
    seed("untyped_1")
    assert listed(client, "type=epic") == {"epic_1"}


def test_a_dropped_type_is_refused_rather_than_answered_empty(client):
    """A work item carries no type but `epic`, so `bug` could only ever match a
    closed row kept as history — refusing says so instead of a caller reading an
    empty page as "no matches in scope"."""
    seed("untyped_1")
    r = client.get("/items?type=bug", headers=_h())
    assert r.status_code == 400
    assert "bug" in r.json()["error"]


# --- the total, which paging depends on ------------------------------------------

def test_the_total_counts_the_filtered_rows_not_the_table(client):
    """A `total` computed before the filter would make a client page through
    rows the filter had already excluded."""
    for n in range(5):
        seed(f"agent_{n}", needs="agent")
    seed("operator_1", needs="operator")
    body = client.get("/items?needs=operator&limit=1", headers=_h()).json()
    assert body["total"] == 1
    assert body["truncated"] is False


# --- the scope, which every filter composes with ---------------------------------

@pytest.fixture
def scoped(tmp_path, monkeypatch, client):
    """A credential confined to one product, beside one with no products at all."""
    tokens = tmp_path / "scoped.json"
    tokens.write_text(json.dumps({
        "t-narrow": {"name": "narrow", "products": ["keel"]},
        "t-none": {"name": "none", "products": []},
    }))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return client


def _as(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def test_a_narrow_credential_sees_only_its_own_claimable_items(scoped):
    """**The one failure here that would matter**, and the reason the new clauses
    are each parenthesised as a unit: they are appended to a WHERE that already
    carries the identity's product scope, and an `OR` escaping its brackets would
    widen a listing past what the caller may see. `total` is computed over the
    same query, so a leak would be visible in the count even on a page that did
    not show the row."""
    seed("mine_1", product="keel")
    seed("theirs_1", product="vivy")
    seed("theirs_2", product="constance")
    for query in ("state=claimable", "state=claimable&needs=agent",
                  "state=open", "needs=agent"):
        body = scoped.get(f"/items?{query}", headers=_as("t-narrow")).json()
        assert [i["id"] for i in body["items"]] == ["mine_1"], query
        assert body["total"] == 1, query


def test_a_credential_scoped_to_nothing_sees_nothing_claimable(scoped):
    seed("mine_1", product="keel")
    body = scoped.get("/items?state=claimable", headers=_as("t-none")).json()
    assert body["items"] == [] and body["total"] == 0



# --- topics, the third filter this route dropped on the floor (2026-09-08) -------
# It failed the way the module docstring calls worse: `?topics=DESIGN` returned
# every open item rather than none, so a caller reading a grouped queue got the
# whole queue and no error.

def _ordered(client, query) -> list[str]:
    r = client.get(f"/items?{query}", headers=_h())
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


def test_topics_narrows_the_listing(client):
    seed("design_1", topics=json.dumps(["DESIGN"]))
    seed("design_2", topics=json.dumps(["DESIGN", "API"]))
    seed("api_only", topics=json.dumps(["API"]))
    seed("untopiced")
    assert listed(client, "topics=DESIGN") == {"design_1", "design_2"}
    assert listed(client, "topics=API") == {"design_2", "api_only"}


def test_an_unmatched_topic_returns_nothing_rather_than_everything(client):
    """The failure this closes. A filter that is ignored answers with the whole
    queue, which reads as a working call — the direction the docstring above
    calls worse than a narrow answer."""
    seed("design_1", topics=json.dumps(["DESIGN"]))
    seed("untopiced")
    assert listed(client, "topics=NOSUCHTOPIC") == set()
    # ...and the same call without the filter really does return them, so the
    # empty answer above is the filter working rather than an empty table.
    assert listed(client, "") == {"design_1", "untopiced"}


def test_a_topic_matches_exactly_not_as_a_substring(client):
    seed("exact", topics=json.dumps(["API"]))
    seed("longer", topics=json.dumps(["APIGATEWAY"]))
    assert listed(client, "topics=API") == {"exact"}


def test_topics_composes_with_the_other_filters(client):
    seed("keep", product="keel", state="open", topics=json.dumps(["DESIGN"]))
    seed("wrong_product", product="vivy", state="open",
         topics=json.dumps(["DESIGN"]))
    seed("wrong_state", product="keel", state="fixed",
         topics=json.dumps(["DESIGN"]))
    assert listed(client, "topics=DESIGN&product=keel&state=open") == {"keep"}


def test_a_malformed_topics_column_is_invisible_rather_than_fatal(client):
    """`json_each` RAISES on a non-NULL unparseable column and would take the
    whole page down.

    **THIS TEST CANNOT TELL A GUARANTEED GUARD FROM A LUCKY ONE**, and the
    structural test below is what covers that. It passed against the original
    `AND`-chain version too, because SQLite 3.53.3's planner happened to apply
    the cheap `json_valid` check before the `EXISTS` scan. Nothing in SQL
    guarantees that order (reviewer, PR #99), so behaviour alone is not evidence
    the guard is in a position it cannot be optimised out of."""
    seed("good", topics=json.dumps(["DESIGN"]))
    seed("broken", topics="{not json at all")
    seed("wrong_shape", topics=json.dumps({"topic": "DESIGN"}))
    assert listed(client, "topics=DESIGN") == {"good"}
    # The unfiltered page still serves every row, malformed ones included, so
    # the guard hides a bad row from the FILTER and not from the queue.
    assert listed(client, "") == {"good", "broken", "wrong_shape"}


# --- found_during, which epic's work turned an item up ---------------------------
# (issue_01M3BFN34Y8P8K39RWS6MM89ZT)


def test_found_during_narrows_the_listing(client):
    seed("found_a", found_during="epic_a")
    seed("found_b", found_during="epic_b")
    seed("not_found_during_anything")
    assert listed(client, "found_during=epic_a") == {"found_a"}


def test_an_unmatched_found_during_returns_nothing_rather_than_everything(client):
    seed("found_a", found_during="epic_a")
    assert listed(client, "found_during=epic_nope") == set()
    assert listed(client, "") == {"found_a"}


def test_found_during_composes_with_the_other_filters(client):
    seed("keep", product="keel", state="open", found_during="epic_a")
    seed("wrong_product", product="vivy", state="open", found_during="epic_a")
    seed("wrong_epic", product="keel", state="open", found_during="epic_b")
    assert listed(client, "found_during=epic_a&product=keel&state=open") == {"keep"}


# --- sort, which did not exist ---------------------------------------------------

def test_sort_by_topic_groups_the_queue(client):
    seed("b1", topics=json.dumps(["BILLING"]), created_at="2026-08-01 10:00:00")
    seed("a1", topics=json.dumps(["API"]), created_at="2026-08-01 09:00:00")
    seed("b2", topics=json.dumps(["BILLING"]), created_at="2026-08-02 10:00:00")
    assert _ordered(client, "sort=topic") == ["a1", "b2", "b1"], (
        "grouped by first topic, then newest first inside a group")


def test_untopiced_items_sort_last(client):
    """They are the residue of a grouping pass. Burying the grouped work under
    them would defeat the read the sort exists for."""
    seed("none_1", created_at="2026-08-09 10:00:00")
    seed("zed", topics=json.dumps(["ZEBRA"]), created_at="2026-08-01 10:00:00")
    assert _ordered(client, "sort=topic") == ["zed", "none_1"]


def test_the_default_sort_is_unchanged(client):
    seed("older", created_at="2026-08-01 10:00:00")
    seed("newer", created_at="2026-08-05 10:00:00")
    assert _ordered(client, "") == ["newer", "older"]
    assert _ordered(client, "sort=created_at") == ["newer", "older"]


def test_an_unknown_sort_is_refused_rather_than_ignored(client):
    """A caller asking for `sort=priority` and receiving a newest-first page has
    no way to tell it did not happen — the same shape as the `needs` filter that
    was dropped on the floor."""
    r = client.get("/items?sort=priority", headers=_h())
    assert r.status_code == 400
    assert "sort must be one of" in r.json()["error"]


def test_sort_by_topic_survives_a_malformed_column(client):
    """`json_extract` would raise on the same rows `json_each` does."""
    seed("good", topics=json.dumps(["API"]))
    seed("broken", topics="{not json at all")
    assert _ordered(client, "sort=topic") == ["good", "broken"]


def test_json_each_is_never_handed_an_unguarded_column(client):
    """THE PROPERTY IS STRUCTURAL, so the test is too — and that is the point
    rather than a weakness. Whether `json_each` sees a malformed blob depends on
    where the guard sits in the statement, not on any input the test can supply:
    the unsafe `AND` form and the safe `CASE` form return identical results on
    the SQLite bundled today, and differ only when a planner reorders.

    So this asserts the shape the three sibling sites in this file settled on —
    the guard wrapping the call as a `CASE` branch, which SQLite is defined to
    evaluate lazily — rather than a behaviour that cannot distinguish them."""
    import inspect

    import app as libapp
    src = inspect.getsource(libapp.list_items)
    start = src.index('request.query_params.get("topics")')
    fragment = src[start:start + 400]
    assert "CASE WHEN json_valid(topics)" in fragment, (
        "the topics filter must guard json_each with CASE, not a trailing AND — "
        "an AND relies on planner order, which nothing guarantees")
    guard = fragment.index("CASE WHEN json_valid(topics)")
    call = fragment.index("json_each(topics)")
    assert guard < call, "the CASE must open before json_each is reached"


# --- home=invalid, the non-epic rows no ordinary write path can create any
# more (library/specs/one-home-per-work-item, PR 2) --------------------------


def queue(product, queue_id):
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO corrections_queues (id, product, created_at) "
        "VALUES (?,?,?)", (queue_id, product, "2026-08-01 00:00:00.000000"))
    conn.commit()
    conn.close()


def test_home_invalid_surfaces_a_null_parent_non_epic(client):
    seed("null_parent")
    assert listed(client, "home=invalid") == {"null_parent"}


def test_home_invalid_surfaces_a_dangling_parent(client):
    seed("dangling", parent_id="issue_does_not_exist")
    assert listed(client, "home=invalid") == {"dangling"}


def test_home_invalid_excludes_a_valid_epic_parent(client):
    seed("epic_1", type="epic")
    seed("child_1", parent_id="epic_1")
    assert listed(client, "home=invalid") == set()


def test_home_invalid_excludes_a_valid_queue_parent(client):
    queue("keel", "cq_keel")
    seed("corrected", parent_id="cq_keel")
    assert listed(client, "home=invalid") == set()


def test_home_invalid_excludes_a_top_level_epic(client):
    seed("epic_bare", type="epic")
    assert listed(client, "home=invalid") == set()


def test_home_invalid_catches_a_queue_from_another_product(client):
    """A queue id is a valid home only in its own product
    (`_validate_parent`'s same-product rule) — a row pointing at a queue
    from elsewhere is as homeless as one pointing at nothing."""
    queue("a-different-product", "cq_other")
    seed("cross_product", parent_id="cq_other")
    assert listed(client, "home=invalid") == {"cross_product"}


def test_home_invalid_mixes_only_the_invalid_rows_in(client):
    seed("epic_1", type="epic")
    seed("child_1", parent_id="epic_1")
    queue("keel", "cq_keel")
    seed("corrected", parent_id="cq_keel")
    seed("null_parent")
    seed("dangling", parent_id="issue_does_not_exist")
    assert listed(client, "home=invalid") == {"null_parent", "dangling"}


def test_home_rejects_any_other_value(client):
    r = client.get("/items?home=something-else", headers=_h())
    assert r.status_code == 400
    assert "home must be" in r.json()["error"]
