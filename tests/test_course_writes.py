"""Starting a plan: the registry, the one-live-Course slot, and the membership
rules for the order.

Three things carry the weight here.

**A product may hold one live Course, and the slot is enforced by an index.**
The route checks first so a caller gets the id of the plan already holding the
slot, and takes the write lock before checking so the check and the insert
cannot be split by a concurrent create. Both halves are tested, because a
pre-check without the lock passes every single-threaded test ever written.

**Entry validation is where a Course could stop meaning anything.** Only epics,
each one real, each one in the Course's own product, none of them twice.

**The entry lookup is scoped, and Orient's is not.** Orient tells a caller
"wrong product" for an id it cannot otherwise see, which is an existence oracle
in the shape `_validate_parent` already refuses to be. The divergence is
deliberate and one test is entirely about it.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

AGENT = "agent-token-vivy"
WIDE = "agent-token-wide"
NOSCOPE = "agent-token-noscope"

REGISTRY = {
    "_scopes": {"platform": {}},
    "vivy": {"aliases": ["viv"], "repos": ["vivy-app"]},
    "constance": {"aliases": [], "repos": []},
    "keel": {"aliases": ["steward"], "repos": ["substrate", "agent-platform"]},
}


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT);
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE courses (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, entries TEXT,
            status TEXT, version INTEGER, created_by TEXT, created_at TEXT,
            updated_by TEXT, updated_at TEXT);
        -- **THE PARTIAL UNIQUE INDEX IS PART OF THE FIXTURE**, matching the live
        -- schema. It is what actually enforces one live Course per product, so a
        -- test database without it would let the lost-race path pass by never
        -- being reachable — the control would be untested and look tested.
        CREATE UNIQUE INDEX ix_courses_live_product
            ON courses (product) WHERE status = 'live';
        CREATE TABLE course_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, course_id TEXT, actor TEXT,
            action TEXT, entries TEXT, note TEXT, at TEXT);
        -- THE ARCHIVES, because one Course write reports into them. Retiring a
        -- Course whose status was neither `live` nor `superseded` records the
        -- anomaly here, and a fixture without the table would let that write
        -- fail into its own best-effort catch and the test pass regardless.
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
    """)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, created_at) "
        "VALUES (?,?,?,?,?,?)",
        [("issue_epic_v", "vivy", "A vivy epic", "epic", "open",
          "2026-08-01 12:00:00.000000"),
         ("issue_epic_v2", "vivy", "Another vivy epic", "epic", "open",
          "2026-08-01 12:01:00.000000"),
         ("issue_task_v", "vivy", "A vivy task", "task", "open",
          "2026-08-01 12:02:00.000000"),
         # THE PROBE TARGET. A caller scoped to vivy must not be able to tell
         # this id from one that was never issued.
         ("issue_epic_c", "constance", "A constance epic", "epic", "open",
          "2026-08-01 12:03:00.000000")])
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
        NOSCOPE: {"name": "scopeless", "products": []},
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    # PINNED, so an edit to the real registry cannot break these tests and a test
    # cannot silently depend on which projects happen to exist today.
    projects.reload()
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _body(**over):
    out = {"product": "vivy", "title": "The route to the bead"}
    out.update(over)
    return out


def _courses(db_path, product="vivy"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM courses WHERE product = ?", (product,))]
    finally:
        conn.close()


def _signals(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM signals")]
    finally:
        conn.close()


def _events(db_path, course_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM course_events WHERE course_id = ? ORDER BY id",
            (course_id,))]
    finally:
        conn.close()


# --- the happy path ------------------------------------------------------------


def test_a_new_plan_comes_back_in_protocol_shape(client):
    r = client.post("/courses", json=_body(), headers=_h())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["id"].startswith("course_")
    assert out["product"] == "vivy"
    assert out["title"] == "The route to the bead"
    assert out["status"] == store.COURSE_LIVE
    assert out["entries"] == []
    # **`version` STARTS AT 1 AND IS IN THE RESPONSE.** It is the value the next
    # reorder has to send back, and a caller that has to guess it will guess 1 —
    # which is right exactly once and wrong from then on.
    assert out["version"] == 1
    assert out["created_at"].endswith("Z")


def test_a_plan_can_be_created_with_its_order_already_set(client, db_path):
    r = client.post("/courses",
                    json=_body(entries=["issue_epic_v2", "issue_epic_v"]),
                    headers=_h())
    assert r.status_code == 201, r.text
    # THE ORDER IS THE CALLER'S, not sorted and not reordered. A Course is a
    # plan, so the sequence is the entire content.
    assert r.json()["entries"] == ["issue_epic_v2", "issue_epic_v"]
    stored = _courses(db_path)[0]
    assert json.loads(stored["entries"]) == ["issue_epic_v2", "issue_epic_v"]
    assert stored["created_by"] == "vivy-agent"


def test_the_title_is_trimmed_before_it_is_stored(client):
    r = client.post("/courses", json=_body(title="  a plan  "), headers=_h())
    assert r.status_code == 201
    assert r.json()["title"] == "a plan"


# --- the creation event --------------------------------------------------------


def test_creation_is_recorded_as_an_event(client, db_path):
    r = client.post("/courses", json=_body(entries=["issue_epic_v"]),
                    headers=_h())
    events = _events(db_path, r.json()["id"])
    assert [e["action"] for e in events] == [store.COURSE_ACTION_CREATED]
    assert events[0]["actor"] == "vivy-agent"
    assert json.loads(events[0]["entries"]) == ["issue_epic_v"]


def test_an_empty_order_writes_no_entries_snapshot(client, db_path):
    """A NULL snapshot means "the order did not move here". Writing `[]` would
    make it also mean "the order moved to nothing", and the history could no
    longer tell a plan that started empty from one that was emptied."""
    r = client.post("/courses", json=_body(), headers=_h())
    assert _events(db_path, r.json()["id"])[0]["entries"] is None


# --- identity, capability, scope -----------------------------------------------


def test_no_credential_is_401(client):
    r = client.post("/courses", json=_body(),
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_a_missing_capability_is_403_naming_the_capability(client, monkeypatch):
    """Every role the token file can mint carries `course.write` today, so this
    is the only way to reach the gate at all. It is worth reaching: a route that
    never checks passes every other test in this file identically."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"course.write"}})
    r = client.post("/courses", json=_body(), headers=_h())
    assert r.status_code == 403
    assert "course.write" in r.json()["error"]


def test_an_out_of_scope_product_is_404_rather_than_403(client):
    """The same wording an absent row gets. Splitting them would tell a
    `vivy`-scoped caller which products exist."""
    r = client.post("/courses", json=_body(product="constance"), headers=_h())
    assert r.status_code == 404


def test_an_identity_scoped_to_nothing_creates_nothing(client):
    r = client.post("/courses", json=_body(), headers=_h(NOSCOPE))
    assert r.status_code == 404


# --- the product registry ------------------------------------------------------


@pytest.mark.parametrize("given", ["vivy", "VIVY", "Vivy", " vivy ", "viv",
                                   "vivy-app"])
def test_a_product_resolves_to_its_canonical_slug(client, db_path, given):
    """The bug this closes is the one `POST /items` closes: `Substrate` beside
    `substrate`, a plan invisible to every view asking for the real slug."""
    r = client.post("/courses", json=_body(product=given), headers=_h(WIDE))
    assert r.status_code == 201, r.text
    assert r.json()["product"] == "vivy"
    assert _courses(db_path)[0]["product"] == "vivy"


def test_an_unknown_product_is_refused_and_names_only_what_this_caller_may_use(
        client):
    r = client.post("/courses", json=_body(product="nonesuch"), headers=_h())
    assert r.status_code == 400
    # NARROWED TO THE CALLER'S SCOPE. Listing the registry would turn a slow
    # oracle into a directory dump. Every spelling of the caller's own product is
    # offered — the slug, its alias and its repo all resolve — and nothing that
    # resolves to a product this token cannot see.
    assert set(r.json()["known"]) == {"vivy", "viv", "vivy-app"}
    assert {projects.resolve(n) for n in r.json()["known"]} == {"vivy"}


# --- the one-live-Course slot --------------------------------------------------


def test_a_second_live_plan_is_refused_and_names_the_one_that_exists(client):
    first = client.post("/courses", json=_body(), headers=_h())
    assert first.status_code == 201
    second = client.post("/courses", json=_body(title="another route"),
                         headers=_h())
    assert second.status_code == 409
    # THE ID, not just a refusal. A caller told only "no" has to go looking for
    # the plan it collided with before it can reorder or supersede it.
    assert second.json()["course_id"] == first.json()["id"]


def test_a_superseded_plan_does_not_hold_the_slot(client, db_path):
    """The partial index covers `status = 'live'` only, and the route's check
    matches it. A retired route must not stop a project planning a new one."""
    client.post("/courses", json=_body(), headers=_h())
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = ?", (store.COURSE_SUPERSEDED,))
    conn.commit()
    conn.close()
    r = client.post("/courses", json=_body(title="a new route"), headers=_h())
    assert r.status_code == 201


def test_the_slot_is_per_product(client):
    assert client.post("/courses", json=_body(product="vivy"),
                       headers=_h(WIDE)).status_code == 201
    assert client.post("/courses", json=_body(product="constance"),
                       headers=_h(WIDE)).status_code == 201


def test_the_check_and_the_insert_are_one_transaction(client, db_path):
    """**THE CHECK-THEN-ACT IS WHAT NEEDS THE LOCK, and a pre-check alone passes
    every single-threaded test.** Committing a live Course from a second
    connection AFTER the route has taken its write lock must not be possible: the
    second writer blocks until this transaction ends. Asserting on the lock
    directly is what a sequential test cannot do, so this asserts the observable
    consequence — the route holds a write transaction across its check.
    """
    import app as a

    seen = {}
    real = a._live_course_refusal

    def spy(conn, product):
        seen["in_transaction"] = conn.in_transaction
        other = sqlite3.connect(db_path, timeout=0.2)
        try:
            other.execute(
                "INSERT INTO courses (id, product, title, entries, status, "
                "version, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("course_racer", product, "a racing plan", "[]",
                 store.COURSE_LIVE, 1, "other", "2026-08-01 00:00:00"))
            other.commit()
            seen["racer_committed"] = True
        except sqlite3.OperationalError:
            seen["racer_committed"] = False
        finally:
            other.close()
        return real(conn, product)

    a._live_course_refusal = spy
    try:
        r = client.post("/courses", json=_body(), headers=_h())
    finally:
        a._live_course_refusal = real
    assert seen["in_transaction"], "the check ran outside a write transaction"
    assert seen["racer_committed"] is False, (
        "a second writer committed while the check was running; the check and "
        "the insert can be split and the slot is not really held")
    assert r.status_code == 201


def test_a_lost_race_still_names_the_plan_that_won(client, db_path):
    """The lock above should make this unreachable, and it stays handled because
    the INDEX is the real enforcement rather than the pre-check.

    **BLINDING THE PRE-CHECK IS HOW A SPLIT CHECK-THEN-ACT LOOKS FROM INSIDE THE
    ROUTE.** A live Course is committed first, by another connection and before
    the request, so it is exactly the row a concurrent create would have left;
    then the first look is made to miss it. The insert hits the index, and the
    caller still learns which plan holds the slot instead of reading a bare
    integrity error.
    """
    import app as a

    winner = sqlite3.connect(db_path)
    winner.execute(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("course_winner", "vivy", "the winning plan", "[]", store.COURSE_LIVE,
         1, "other", "2026-08-01 00:00:00"))
    winner.commit()
    winner.close()

    real = a._live_course_refusal
    calls = {"n": 0}

    def blind_first_look(conn, product):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(conn, product)

    a._live_course_refusal = blind_first_look
    try:
        r = client.post("/courses", json=_body(), headers=_h())
    finally:
        a._live_course_refusal = real
    assert calls["n"] == 2, "the integrity path did not re-read the winning plan"
    assert r.status_code == 409
    assert r.json()["course_id"] == "course_winner"


# --- the request body ----------------------------------------------------------


def test_a_non_object_body_is_refused(client):
    r = client.post("/courses", json=["not", "an", "object"], headers=_h())
    assert r.status_code == 400


@pytest.mark.parametrize("field", ["product", "title"])
def test_product_and_title_are_both_required(client, field):
    body = _body()
    del body[field]
    r = client.post("/courses", json=body, headers=_h())
    assert r.status_code == 400
    assert field in r.json()["error"]


@pytest.mark.parametrize("field", ["product", "title"])
def test_a_blank_field_is_refused_the_same_as_an_absent_one(client, field):
    r = client.post("/courses", json=_body(**{field: "   "}), headers=_h())
    assert r.status_code == 400


def test_an_over_long_title_is_refused_rather_than_clipped(client):
    """Refused, where the stored value is also clipped. A silently truncated plan
    title is a plan named something its author did not write."""
    r = client.post("/courses", json=_body(title="x" * (store.MAX_TITLE + 1)),
                    headers=_h())
    assert r.status_code == 400


def test_a_title_that_fits_after_trimming_is_accepted(client):
    """**THE CHECK AND THE STORED VALUE HAVE TO AGREE ON WHICH STRING THEY MEAN**
    (reviewer, PR #13). This route trims before storing, so checking the raw
    length refused a title that fits: `MAX_TITLE` characters behind three spaces
    is over the cap raw and exactly at it once stripped, and the caller was told
    it was too long about a value nobody would ever have written."""
    r = client.post("/courses", json=_body(title="   " + "x" * store.MAX_TITLE),
                    headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "x" * store.MAX_TITLE


def test_a_title_over_the_cap_after_trimming_is_still_refused(client):
    """The other half of the same fix: trimming must not become a way to smuggle
    an over-length title past the check."""
    r = client.post("/courses",
                    json=_body(title="   " + "x" * (store.MAX_TITLE + 1)),
                    headers=_h())
    assert r.status_code == 400


def test_an_over_long_product_is_refused(client):
    r = client.post("/courses", json=_body(product="x" * (store.MAX_IDENT + 1)),
                    headers=_h())
    assert r.status_code == 400


# --- what may be in the order --------------------------------------------------


def test_entries_must_be_an_array(client):
    r = client.post("/courses", json=_body(entries="issue_epic_v"), headers=_h())
    assert r.status_code == 400


def test_an_order_longer_than_the_cap_is_refused(client):
    r = client.post("/courses",
                    json=_body(entries=[f"issue_{n}"
                                        for n in range(store.MAX_COURSE_ENTRIES + 1)]),
                    headers=_h())
    assert r.status_code == 400
    assert str(store.MAX_COURSE_ENTRIES) in r.json()["error"]


@pytest.mark.parametrize("entry", ["", "   ", 7, None])
def test_every_entry_must_be_a_non_empty_id(client, entry):
    r = client.post("/courses", json=_body(entries=[entry]), headers=_h())
    assert r.status_code == 400


def test_a_repeated_epic_is_refused_and_the_duplicates_are_named(client):
    """An epic twice has no defined position, and it would make the array
    disagree with what a reader sees."""
    r = client.post("/courses",
                    json=_body(entries=["issue_epic_v", "issue_epic_v2",
                                        "issue_epic_v"]),
                    headers=_h())
    assert r.status_code == 400
    assert r.json()["duplicates"] == ["issue_epic_v"]


def test_an_unknown_entry_is_refused_and_every_bad_id_is_named(client):
    """ALL of them, not the first. A caller assembling a plan would otherwise fix
    them one round trip at a time."""
    r = client.post("/courses",
                    json=_body(entries=["issue_epic_v", "issue_nope",
                                        "issue_also_nope"]),
                    headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["issue_also_nope", "issue_nope"]


def test_a_course_contains_only_epics(client):
    """The operator, 2026-08-05. The Course is the planned lane and the issue queue is the
    interrupt lane; accepting an issue here would merge the two."""
    r = client.post("/courses", json=_body(entries=["issue_task_v"]),
                    headers=_h())
    assert r.status_code == 400
    assert r.json()["not_epics"] == ["issue_task_v"]


def test_an_epic_from_another_product_is_refused_when_the_caller_can_see_it(
        client):
    """A wildcard caller can already read the constance epic, so naming its
    product discloses nothing and makes the mistake fixable."""
    r = client.post("/courses",
                    json=_body(entries=["issue_epic_v", "issue_epic_c"]),
                    headers=_h(WIDE))
    assert r.status_code == 400
    assert r.json()["wrong_product"] == ["issue_epic_c"]


def test_an_epic_outside_the_callers_scope_reads_as_unknown_not_wrong_product(
        client):
    """**THE ORACLE THIS ROUTE REFUSES TO BE, and the one place it diverges from
    Orient.** Orient answers "wrong product" here, which lets a caller holding
    one product's token submit ids one at a time and learn which of them exist in
    products it cannot see. The scoped lookup collapses that into the wording an
    invented id gets, so the two are indistinguishable.

    The paired assertion is what makes this test mean something: an id that was
    never issued must come back looking IDENTICAL, or the oracle is still open
    one refusal-shape further down.
    """
    real = client.post("/courses", json=_body(entries=["issue_epic_c"]),
                       headers=_h())
    invented = client.post("/courses", json=_body(entries=["issue_never_filed"]),
                           headers=_h())
    assert real.status_code == invented.status_code == 400
    assert "wrong_product" not in real.json()
    assert real.json()["error"] == invented.json()["error"]
    assert real.json()["unknown"] == ["issue_epic_c"]
    assert invented.json()["unknown"] == ["issue_never_filed"]


def test_a_refused_order_writes_no_plan(client, db_path):
    """The validation runs inside the write transaction, so a refusal after the
    slot check must leave the table as it found it."""
    r = client.post("/courses", json=_body(entries=["issue_task_v"]),
                    headers=_h())
    assert r.status_code == 400
    assert _courses(db_path) == []


# --- the shadow ----------------------------------------------------------------


def test_the_route_refuses_while_writes_are_disabled(client, monkeypatch):
    """§4 requires Orient to own writes for the whole read-only shadow. The
    refusal comes from `connect_rw` rather than from this route, which is what
    stops a route added later from forgetting it."""
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post("/courses", json=_body(), headers=_h())
    assert r.status_code == 503
    assert "read-only shadow" in r.json()["hint"]


def test_the_protocol_header_is_required(client):
    r = client.post("/courses", json=_body(),
                    headers={"Authorization": f"Bearer {AGENT}"})
    assert r.status_code == 400
    assert protocol.HEADER in r.json()["error"]


# --- renaming a plan -----------------------------------------------------------


def _make(client, **over):
    r = client.post("/courses", json=_body(**over), headers=_h(WIDE))
    assert r.status_code == 201, r.text
    return r.json()


def test_a_plan_can_be_renamed(client, db_path):
    course = _make(client)
    r = client.patch(f"/courses/{course['id']}", json={"title": "a better name"},
                     headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "a better name"
    assert _courses(db_path)[0]["title"] == "a better name"


def test_renaming_does_not_move_the_order(client):
    """The order goes through one route, so there is one place a reorder is
    validated, versioned and recorded. A second door onto the array would be a
    second place the version check has to be remembered."""
    course = _make(client, entries=["issue_epic_v", "issue_epic_v2"])
    r = client.patch(f"/courses/{course['id']}",
                     json={"title": "renamed", "entries": ["issue_epic_v2"]},
                     headers=_h())
    assert r.status_code == 200
    assert r.json()["entries"] == ["issue_epic_v", "issue_epic_v2"]


def test_renaming_does_not_bump_the_version(client):
    """**`version` IS THE ORDER'S CONCURRENCY TOKEN AND NOTHING ELSE READS IT.**
    Bumping it here would make a caller's held version stale for a reason that
    has nothing to do with the array, so a reorder it had already rebased would
    come back 409 against a rename it never conflicted with."""
    course = _make(client)
    r = client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                     headers=_h())
    assert r.json()["version"] == course["version"] == 1


def test_a_rename_is_recorded_with_what_it_became(client, db_path):
    course = _make(client, entries=["issue_epic_v"])
    client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                 headers=_h())
    events = _events(db_path, course["id"])
    assert [e["action"] for e in events] == [store.COURSE_ACTION_CREATED,
                                             store.COURSE_ACTION_RETITLED]
    assert events[1]["note"] == "renamed"
    # NULL, because this action did not move the order. A snapshot written here
    # would make the history claim it did.
    assert events[1]["entries"] is None


def test_the_new_title_is_trimmed(client):
    course = _make(client)
    r = client.patch(f"/courses/{course['id']}", json={"title": "  spaced  "},
                     headers=_h())
    assert r.json()["title"] == "spaced"


def test_a_rename_stamps_who_and_when(client, db_path):
    course = _make(client)
    client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                 headers=_h())
    stored = _courses(db_path)[0]
    assert stored["updated_by"] == "vivy-agent"
    assert stored["updated_at"] is not None


# --- what a rename refuses -----------------------------------------------------


def test_a_superseded_plan_cannot_be_renamed(client, db_path):
    """Orient shipped this gap: `retitle` lacked the check `set_order` had, so a
    retired route stayed silently renameable. A frozen plan that can still be
    renamed is not frozen."""
    course = _make(client)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = ?", (store.COURSE_SUPERSEDED,))
    conn.commit()
    conn.close()
    r = client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                     headers=_h())
    assert r.status_code == 409
    assert store.COURSE_SUPERSEDED in r.json()["error"]


def test_an_out_of_scope_plan_is_404_and_matches_an_absent_one(client):
    """The paired assertion is the whole point: a `vivy`-scoped caller must not
    be able to tell a real constance Course from an id nobody ever minted."""
    other = _make(client, product="constance")
    real = client.patch(f"/courses/{other['id']}", json={"title": "renamed"},
                        headers=_h())
    invented = client.patch("/courses/course_never_minted",
                            json={"title": "renamed"}, headers=_h())
    assert real.status_code == invented.status_code == 404
    assert real.json() == invented.json()


def test_a_rename_needs_the_capability(client, monkeypatch):
    course = _make(client)
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"course.write"}})
    r = client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                     headers=_h())
    assert r.status_code == 403
    assert "course.write" in r.json()["error"]


def test_a_rename_needs_a_credential(client):
    course = _make(client)
    r = client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                     headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


@pytest.mark.parametrize("title", [None, "", "   ", 7, ["a list"]])
def test_a_rename_requires_a_real_title(client, title):
    course = _make(client)
    r = client.patch(f"/courses/{course['id']}", json={"title": title},
                     headers=_h())
    assert r.status_code == 400


def test_an_over_long_new_title_is_refused(client):
    course = _make(client)
    r = client.patch(f"/courses/{course['id']}",
                     json={"title": "x" * (store.MAX_TITLE + 1)}, headers=_h())
    assert r.status_code == 400


def test_a_new_title_that_fits_after_trimming_is_accepted(client):
    """The same fix as `create_course`, in the second route that trims. A check
    measuring a different string from the one it stores refuses a title that
    fits."""
    course = _make(client)
    r = client.patch(f"/courses/{course['id']}",
                     json={"title": "   " + "x" * store.MAX_TITLE}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "x" * store.MAX_TITLE


def test_a_refused_rename_leaves_the_title_alone(client, db_path):
    course = _make(client)
    client.patch(f"/courses/{course['id']}", json={"title": ""}, headers=_h())
    assert _courses(db_path)[0]["title"] == course["title"]
    assert _events(db_path, course["id"])[-1]["action"] == \
        store.COURSE_ACTION_CREATED


def test_a_rename_refuses_while_writes_are_disabled(client, monkeypatch):
    course = _make(client)
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                     headers=_h())
    assert r.status_code == 503


def test_a_rename_ignores_every_field_but_the_title(client, db_path):
    """The same defence-in-depth `test_renaming_does_not_move_the_order` gives
    for `entries` (reviewer, PR #14). The route reads only `title`, and this is
    what keeps that true if somebody widens the UPDATE later."""
    course = _make(client)
    r = client.patch(f"/courses/{course['id']}",
                     json={"title": "renamed", "version": 99,
                           "status": store.COURSE_SUPERSEDED,
                           "product": "constance", "id": "course_elsewhere"},
                     headers=_h())
    assert r.status_code == 200
    stored = _courses(db_path)[0]
    assert stored["version"] == 1
    assert stored["status"] == store.COURSE_LIVE
    assert stored["product"] == "vivy"
    assert stored["id"] == course["id"]


def test_the_frozen_check_and_the_rename_are_one_transaction(client, db_path):
    """**THE CHECK-THEN-ACT RETITLE ALSO HAS** (reviewer, PR #14). Reading the
    status and writing on the strength of it is the same shape as the slot check
    and the version check. Today nothing can move a Course off `live` mid-flight,
    but the supersede route is next in this stack, and a control that arrives one
    PR after the thing that needs it was missing for a PR."""
    import app as a

    course = _make(client)
    seen = {}
    calls = {"n": 0}
    real = a._load_course

    def spy(conn, ident, cid):
        # FIRST CALL ONLY. The post-commit re-read goes through `_load_course`
        # too, and letting the racer run again after the commit would overwrite
        # `seen` with a result from a transaction that has already ended.
        calls["n"] += 1
        if calls["n"] > 1:
            return real(conn, ident, cid)
        seen["in_transaction"] = conn.in_transaction
        other = sqlite3.connect(db_path, timeout=0.2)
        try:
            other.execute("UPDATE courses SET status = ? WHERE id = ?",
                          (store.COURSE_SUPERSEDED, cid))
            other.commit()
            seen["racer_committed"] = True
        except sqlite3.OperationalError:
            seen["racer_committed"] = False
        finally:
            other.close()
        return real(conn, ident, cid)

    a._load_course = spy
    try:
        r = client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                         headers=_h())
    finally:
        a._load_course = real
    assert seen["in_transaction"], "the frozen check ran outside a transaction"
    assert seen["racer_committed"] is False
    assert r.status_code == 200


def test_a_rename_that_loses_the_status_race_refuses(client, db_path):
    """The compare-and-swap is the control the lock does not have to be. Blinding
    the frozen check the way a split check-then-act would must still refuse,
    because the UPDATE carries `WHERE status = ?`."""
    import app as a

    course = _make(client)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = ?", (store.COURSE_SUPERSEDED,))
    conn.commit()
    conn.close()
    real = a._load_course

    def stale_view(conn, ident, cid):
        row = real(conn, ident, cid)
        return {**dict(row), "status": store.COURSE_LIVE} if row else None

    a._load_course = stale_view
    try:
        r = client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                         headers=_h())
    finally:
        a._load_course = real
    assert r.status_code == 409
    assert _courses(db_path)[0]["title"] == course["title"]


# --- moving the order ----------------------------------------------------------


def _order(client, course, entries, version=None, token=AGENT, **extra):
    body = {"version": course["version"] if version is None else version,
            "entries": entries}
    body.update(extra)
    return client.put(f"/courses/{course['id']}/order", json=body,
                      headers=_h(token))


def test_the_order_is_replaced_wholesale(client, db_path):
    course = _make(client, entries=["issue_epic_v"])
    r = _order(client, course, ["issue_epic_v2", "issue_epic_v"])
    assert r.status_code == 200, r.text
    assert r.json()["entries"] == ["issue_epic_v2", "issue_epic_v"]
    assert json.loads(_courses(db_path)[0]["entries"]) == ["issue_epic_v2",
                                                           "issue_epic_v"]


def test_a_reorder_bumps_the_version(client):
    course = _make(client)
    r = _order(client, course, ["issue_epic_v"])
    assert r.json()["version"] == course["version"] + 1


def test_a_reorder_stamps_who_and_when(client, db_path):
    course = _make(client)
    _order(client, course, ["issue_epic_v"])
    stored = _courses(db_path)[0]
    assert stored["updated_by"] == "vivy-agent"
    assert stored["updated_at"] is not None


def test_a_reorder_is_recorded_with_its_snapshot_and_note(client, db_path):
    course = _make(client)
    _order(client, course, ["issue_epic_v"], note="the epic moved up")
    events = _events(db_path, course["id"])
    assert events[-1]["action"] == store.COURSE_ACTION_REORDERED
    assert json.loads(events[-1]["entries"]) == ["issue_epic_v"]
    assert events[-1]["note"] == "the epic moved up"


def test_clearing_the_order_still_records_a_snapshot(client, db_path):
    """**`reordered` CARRIES A SNAPSHOT EVEN WHEN THE NEW ORDER IS EMPTY.**
    Create writes NULL for an empty array because "the order did not move here"
    is the fact it records. Here the order DID move, and to nothing, so a NULL
    would lose the one event that explains why the plan is empty."""
    course = _make(client, entries=["issue_epic_v"])
    r = _order(client, course, [])
    assert r.status_code == 200
    assert r.json()["entries"] == []
    assert json.loads(_events(db_path, course["id"])[-1]["entries"]) == []


# --- the version check ---------------------------------------------------------


def test_a_stale_version_is_refused_with_what_to_rebase_onto(client):
    """A bare 409 leaves the caller re-fetching and guessing. The refusal carries
    the order it lost to, so the edit can be rebased in one round trip."""
    course = _make(client, entries=["issue_epic_v"])
    assert _order(client, course, ["issue_epic_v2"]).status_code == 200
    stale = _order(client, course, ["issue_epic_v"])  # still holding version 1
    assert stale.status_code == 409
    body = stale.json()
    assert body["your_version"] == 1
    assert body["current_version"] == 2
    assert body["current_entries"] == ["issue_epic_v2"]


def test_a_stale_reorder_changes_nothing(client, db_path):
    course = _make(client, entries=["issue_epic_v"])
    _order(client, course, ["issue_epic_v2"])
    _order(client, course, ["issue_epic_v"])
    stored = _courses(db_path)[0]
    assert json.loads(stored["entries"]) == ["issue_epic_v2"]
    assert stored["version"] == 2
    assert [e["action"] for e in _events(db_path, course["id"])].count(
        store.COURSE_ACTION_REORDERED) == 1


@pytest.mark.parametrize("version", [None, "1", 1.0, [1]])
def test_version_is_required_and_must_be_an_integer(client, version):
    course = _make(client)
    r = client.put(f"/courses/{course['id']}/order",
                   json={"version": version, "entries": []}, headers=_h())
    assert r.status_code == 400
    assert "version" in r.json()["error"]


def test_a_boolean_version_is_refused_rather_than_read_as_one(client):
    """`bool` is a subclass of `int`, so `True` would otherwise read as version 1
    and pass against a fresh Course. The one check that must never quietly
    interpret its input is the one built to catch a stale read."""
    course = _make(client)
    r = client.put(f"/courses/{course['id']}/order",
                   json={"version": True, "entries": []}, headers=_h())
    assert r.status_code == 400


def test_the_update_is_conditional_on_the_version_not_only_the_check(client,
                                                                     db_path):
    """**THE COMPARE-AND-SWAP IS THE REAL ENFORCEMENT, and the pre-check is for
    the message.** Blinding the pre-check the way a split check-then-act would
    must still refuse, because the UPDATE itself carries `WHERE version = ?`. A
    future refactor that moves or drops the `BEGIN IMMEDIATE` then degrades to a
    409 rather than to a silent lost update."""
    course = _make(client, entries=["issue_epic_v"])
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET version = 7")
    conn.commit()
    conn.close()

    import app as a
    real = a._load_course
    calls = {"n": 0}

    def stale_first_look(c, ident, cid):
        # ONLY THE PRE-CHECK IS BLINDED. The rollback path re-reads through the
        # same function, so staling every call would hide the fresh state the
        # refusal is supposed to report, and the test would be asserting on its
        # own monkeypatch rather than on the row.
        calls["n"] += 1
        row = real(c, ident, cid)
        if calls["n"] > 1 or row is None:
            return row
        return {**dict(row), "version": 1}

    a._load_course = stale_first_look
    try:
        r = _order(client, course, ["issue_epic_v2"], version=1)
    finally:
        a._load_course = real
    assert r.status_code == 409
    assert calls["n"] == 2, "the rollback path did not re-read the fresh row"
    assert r.json()["current_version"] == 7
    assert json.loads(_courses(db_path)[0]["entries"]) == ["issue_epic_v"]


def test_the_read_and_the_write_are_one_transaction(client, db_path):
    """The version check is a check-then-act, and a SELECT takes no lock. A
    second writer must not be able to commit between the two."""
    import app as a

    course = _make(client)
    seen = {}
    calls = {"n": 0}
    real = a._load_course

    def spy(conn, ident, cid):
        # FIRST CALL ONLY. The post-commit re-read goes through `_load_course`
        # too, and letting the racer run again after the commit would overwrite
        # `seen` with a result from a transaction that has already ended.
        calls["n"] += 1
        if calls["n"] > 1:
            return real(conn, ident, cid)
        seen["in_transaction"] = conn.in_transaction
        other = sqlite3.connect(db_path, timeout=0.2)
        try:
            other.execute("UPDATE courses SET version = 99 WHERE id = ?", (cid,))
            other.commit()
            seen["racer_committed"] = True
        except sqlite3.OperationalError:
            seen["racer_committed"] = False
        finally:
            other.close()
        return real(conn, ident, cid)

    a._load_course = spy
    try:
        r = _order(client, course, ["issue_epic_v"])
    finally:
        a._load_course = real
    assert seen["in_transaction"], "the version check ran outside a transaction"
    assert seen["racer_committed"] is False
    assert r.status_code == 200


# --- what a reorder refuses ----------------------------------------------------


def test_entries_is_required_rather_than_defaulting_to_empty(client, db_path):
    """**THE DIVERGENCE FROM ORIENT.** Under Orient's reading a client bug that
    drops the field sends `{"version": 3}` and empties the plan, which is the
    largest lost update this route can produce, arriving through the one door
    built to prevent lost updates."""
    course = _make(client, entries=["issue_epic_v"])
    r = client.put(f"/courses/{course['id']}/order",
                   json={"version": course["version"]}, headers=_h())
    assert r.status_code == 400
    assert "entries" in r.json()["error"]
    assert json.loads(_courses(db_path)[0]["entries"]) == ["issue_epic_v"]


def test_clearing_the_plan_is_still_one_request(client):
    """Requiring the field costs a caller that means to clear a Course nothing:
    send an empty array and mean it."""
    course = _make(client, entries=["issue_epic_v"])
    assert _order(client, course, []).status_code == 200


def test_a_reorder_takes_the_same_membership_rules_as_create(client):
    """`_validate_entries` is shared for exactly this reason: a rule enforced on
    create but not on reorder is a rule the second write quietly undoes. Both
    calls reuse the same Course, which a refused reorder leaves at version 1."""
    course = _make(client)
    assert _order(client, course, ["issue_task_v"]).json()["not_epics"] == \
        ["issue_task_v"]
    assert _order(client, course,
                  ["issue_epic_v", "issue_epic_v"]).json()["duplicates"] == \
        ["issue_epic_v"]


def test_a_reorder_cannot_smuggle_in_an_out_of_scope_epic(client):
    course = _make(client)
    r = _order(client, course, ["issue_epic_c"])
    assert r.status_code == 400
    assert "wrong_product" not in r.json()
    assert r.json()["unknown"] == ["issue_epic_c"]


def test_a_superseded_plan_cannot_be_reordered(client, db_path):
    course = _make(client)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = ?", (store.COURSE_SUPERSEDED,))
    conn.commit()
    conn.close()
    r = _order(client, course, ["issue_epic_v"])
    assert r.status_code == 409
    assert store.COURSE_SUPERSEDED in r.json()["error"]


def test_an_out_of_scope_reorder_is_404_and_matches_an_absent_one(client):
    other = _make(client, product="constance")
    real = _order(client, other, [], token=AGENT)
    invented = client.put("/courses/course_never_minted/order",
                          json={"version": 1, "entries": []}, headers=_h())
    assert real.status_code == invented.status_code == 404
    assert real.json() == invented.json()


def test_a_reorder_needs_the_capability(client, monkeypatch):
    course = _make(client)
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"course.write"}})
    assert _order(client, course, []).status_code == 403


def test_a_reorder_needs_a_credential(client):
    course = _make(client)
    r = client.put(f"/courses/{course['id']}/order",
                   json={"version": 1, "entries": []},
                   headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_a_reorder_refuses_while_writes_are_disabled(client, monkeypatch):
    course = _make(client)
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _order(client, course, []).status_code == 503


# --- the note ------------------------------------------------------------------


def test_a_note_that_is_not_a_string_says_so(client):
    """The two failures get different messages. Collapsing them leaves a caller
    holding a perfectly good note unable to tell whether to retype it or shorten
    it."""
    course = _make(client)
    r = _order(client, course, [], note=7)
    assert r.status_code == 400
    assert r.json()["error"] == "note must be a string"


def test_an_over_long_note_says_how_long_it_was(client):
    course = _make(client)
    r = _order(client, course, [], note="x" * (store.MAX_COURSE_NOTE + 1))
    assert r.status_code == 400
    assert r.json()["length"] == store.MAX_COURSE_NOTE + 1


def test_a_note_is_optional(client, db_path):
    course = _make(client)
    assert _order(client, course, []).status_code == 200
    assert _events(db_path, course["id"])[-1]["note"] is None


@pytest.mark.parametrize("body", [
    {"version": 1},                 # the key dropped entirely
    {"version": 1, "entries": None},  # the key present, holding null
])
def test_a_missing_or_null_order_is_refused_rather_than_emptying_the_plan(
        client, db_path, body):
    """**BOTH SHAPES OF A DROPPED FIELD, and the second one got through**
    (reviewer, PR #15). A client that drops `entries` may send an absent key or
    an explicit `null` depending on how it serialises, and the first version of
    this guard tested `"entries" not in payload` — so `null` passed, reached
    `_validate_entries`'s None-to-empty convenience, and emptied the plan with a
    200. That is the lost update this rule exists to refuse, walking through the
    rule."""
    course = _make(client, entries=["issue_epic_v"])
    r = client.put(f"/courses/{course['id']}/order", json=body, headers=_h())
    assert r.status_code == 400, r.text
    assert "entries" in r.json()["error"]
    assert json.loads(_courses(db_path)[0]["entries"]) == ["issue_epic_v"]
    assert _courses(db_path)[0]["version"] == 1


def test_create_still_treats_a_missing_order_as_an_empty_one(client):
    """The convenience moved to create's call site rather than being deleted:
    filing a plan and filling it in later is a real and common way to start
    one."""
    for body in (_body(), _body(entries=None)):
        r = client.post("/courses", json=body, headers=_h())
        if r.status_code == 409:  # the slot is taken by the previous iteration
            continue
        assert r.status_code == 201, r.text
        assert r.json()["entries"] == []


def test_a_non_list_order_is_refused_by_both_routes(client):
    course = _make(client)
    assert client.put(f"/courses/{course['id']}/order",
                      json={"version": 1, "entries": "issue_epic_v"},
                      headers=_h()).status_code == 400


# --- reading is a precondition of writing --------------------------------------


@pytest.mark.parametrize("missing", ["course.write", "course.read"])
def test_every_course_write_route_needs_both_capabilities(client, monkeypatch,
                                                          missing):
    """**A WRITE ROUTE THAT ANSWERS WITH A COURSE NEEDS THE CAPABILITY TO READ
    ONE** (security-reviewer, PR #15). Each of these returns a full Course on
    success, and the reorder route's version-mismatch 409 returns
    `current_entries` — the whole ordered list of epic ids — because a caller
    told only "you are stale" cannot rebase.

    Today both roles carry both capabilities, so the disclosure is inert. That is
    incidental to a two-row table: a narrower role granted `course.write` alone
    would find these the one path handing it the order it was deliberately not
    granted read access to. The check is what makes such a role trip a 403
    instead of silently reopening it.

    Parametrised over the two capabilities and swept over every route, because a
    rule applied to three routes and forgotten on the fourth is the failure this
    file has already had twice.
    """
    course = _make(client)
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {missing}})
    calls = [
        client.post("/courses", json=_body(title="another"), headers=_h()),
        client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                     headers=_h()),
        client.put(f"/courses/{course['id']}/order",
                   json={"version": 1, "entries": []}, headers=_h()),
        client.post(f"/courses/{course['id']}/supersede", json={}, headers=_h()),
    ]
    for r in calls:
        assert r.status_code == 403, r.text
        assert missing in r.json()["error"]


def test_a_stale_reorder_tells_a_read_capable_caller_what_to_rebase_onto(client):
    """The other half: requiring `course.read` must not cost the caller that has
    it the information the refusal exists to carry."""
    course = _make(client, entries=["issue_epic_v"])
    _order(client, course, ["issue_epic_v2"])
    stale = _order(client, course, ["issue_epic_v"])
    assert stale.status_code == 409
    assert stale.json()["current_entries"] == ["issue_epic_v2"]


# --- retiring a route ----------------------------------------------------------


def _supersede(client, course, token=AGENT, **body):
    return client.post(f"/courses/{course['id']}/supersede", json=body,
                       headers=_h(token))


def test_a_plan_is_marked_rather_than_deleted(client, db_path):
    """A bead's destination is temporarily final (ADR-0024), so the route to a
    previous one is history worth having. A deleted row cannot answer "what did
    we intend before"."""
    course = _make(client, entries=["issue_epic_v"])
    r = _supersede(client, course)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == store.COURSE_SUPERSEDED
    stored = _courses(db_path)
    assert len(stored) == 1
    # THE ORDER SURVIVES THE RETIREMENT. It is what the route used to be.
    assert json.loads(stored[0]["entries"]) == ["issue_epic_v"]


def test_superseding_is_recorded_with_its_reason(client, db_path):
    course = _make(client)
    _supersede(client, course, note="the bead moved")
    events = _events(db_path, course["id"])
    assert events[-1]["action"] == store.COURSE_ACTION_SUPERSEDED
    assert events[-1]["note"] == "the bead moved"
    # NULL: retiring a route does not move its order, and the order it retired
    # with is still on the row itself.
    assert events[-1]["entries"] is None


def test_superseding_stamps_who_and_when(client, db_path):
    course = _make(client)
    _supersede(client, course)
    stored = _courses(db_path)[0]
    assert stored["updated_by"] == "vivy-agent"
    assert stored["updated_at"] is not None


def test_a_retired_route_frees_the_products_live_slot(client):
    """The consequence that makes supersede worth having: a project that has
    changed its mind can plan again."""
    course = _make(client)
    assert client.post("/courses", json=_body(title="a second"),
                       headers=_h()).status_code == 409
    _supersede(client, course)
    assert client.post("/courses", json=_body(title="a second"),
                       headers=_h()).status_code == 201


def test_a_retired_route_is_frozen_against_every_write(client):
    """One `_frozen_course`, called by all three. A guard on some write routes
    and not others is how "frozen" stops being true."""
    course = _make(client)
    _supersede(client, course)
    assert client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                        headers=_h()).status_code == 409
    assert _order(client, course, ["issue_epic_v"]).status_code == 409


def test_a_retired_route_still_reads_back(client):
    """Marked rather than deleted means the read surface still serves it, under
    `?status=all` rather than in the default live listing."""
    course = _make(client)
    _supersede(client, course)
    assert client.get(f"/courses/{course['id']}",
                      headers=_h()).status_code == 200
    assert client.get("/courses", headers=_h()).json()["total"] == 0
    assert client.get("/courses?status=all", headers=_h()).json()["total"] == 1


# --- doing it twice ------------------------------------------------------------


def test_superseding_twice_answers_200_and_says_so(client):
    """The request asked for a state the Course is already in. Refusing that
    would make a retried call after a dropped response look like a failure."""
    course = _make(client)
    _supersede(client, course)
    again = _supersede(client, course)
    assert again.status_code == 200
    assert again.json()["idempotent"] is True


def test_the_second_supersede_records_nothing(client, db_path):
    """A second retirement in the history would say the plan was retired twice,
    and `updated_at` would claim it changed when nothing did."""
    course = _make(client)
    _supersede(client, course)
    first = _courses(db_path)[0]["updated_at"]
    _supersede(client, course)
    assert [e["action"] for e in _events(db_path, course["id"])].count(
        store.COURSE_ACTION_SUPERSEDED) == 1
    assert _courses(db_path)[0]["updated_at"] == first


def test_a_lost_supersede_race_is_idempotent_rather_than_a_conflict(client,
                                                                    db_path):
    """**THE COMPARE-AND-SWAP IS THE REAL CONTROL, and the pre-check is for the
    fast path.** Blinding the pre-check the way a split check-then-act would must
    still write exactly once: the UPDATE carries `WHERE status = ?`, so the
    loser's rowcount is zero and it answers with the state the winner left."""
    import app as a

    course = _make(client)
    _supersede(client, course)
    real = a._load_course
    calls = {"n": 0}

    def stale_first_look(conn, ident, cid):
        # FIRST CALL ONLY, the same gate the reorder route's equivalent test
        # uses and for the same reason (reviewer, PR #16). Blinding every call
        # would carry the fake `live` through the rollback re-read as well, and
        # the assertion below would be reading the monkeypatch rather than the
        # state the winner actually left.
        calls["n"] += 1
        row = real(conn, ident, cid)
        if calls["n"] > 1 or row is None:
            return row
        return {**dict(row), "status": store.COURSE_LIVE}

    a._load_course = stale_first_look
    try:
        r = _supersede(client, course)
    finally:
        a._load_course = real
    assert r.status_code == 200
    assert calls["n"] == 2, "the CAS-fail path did not re-read the fresh row"
    assert r.json()["idempotent"] is True
    # THE STATE THE WINNER LEFT, read off the row rather than off the blind.
    assert r.json()["status"] == store.COURSE_SUPERSEDED
    assert [e["action"] for e in _events(db_path, course["id"])].count(
        store.COURSE_ACTION_SUPERSEDED) == 1


# --- what supersede refuses ----------------------------------------------------


def test_an_out_of_scope_supersede_is_404_and_matches_an_absent_one(client):
    other = _make(client, product="constance")
    real = _supersede(client, other)
    invented = client.post("/courses/course_never_minted/supersede", json={},
                           headers=_h())
    assert real.status_code == invented.status_code == 404
    assert real.json() == invented.json()


def test_a_supersede_needs_the_capability(client, monkeypatch):
    course = _make(client)
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"course.write"}})
    assert _supersede(client, course).status_code == 403


def test_a_supersede_needs_a_credential(client):
    course = _make(client)
    r = client.post(f"/courses/{course['id']}/supersede", json={},
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_a_supersede_takes_no_body_at_all(client):
    """The note is the only field and it is optional, so an empty request is a
    complete one."""
    course = _make(client)
    r = client.post(f"/courses/{course['id']}/supersede", headers=_h())
    assert r.status_code == 200


def test_a_supersede_note_takes_the_same_rules_as_a_reorder_note(client):
    course = _make(client)
    assert _supersede(client, course, note=7).json()["error"] == \
        "note must be a string"
    assert _supersede(client, course,
                      note="x" * (store.MAX_COURSE_NOTE + 1)).status_code == 400


def test_a_supersede_refuses_while_writes_are_disabled(client, monkeypatch):
    course = _make(client)
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _supersede(client, course).status_code == 503


def test_a_corrupted_status_is_retired_rather_than_reported_already_done(
        client, db_path):
    """**A NULL STATUS MADE THE COMPARE-AND-SWAP A PERMANENT NO-OP THAT STILL
    ANSWERED 200** (reviewer, PR #16). SQL equality against NULL is never true,
    so `WHERE status = ?` bound to None matched no row, including the row whose
    status genuinely IS NULL. The route rolled back, re-read the untouched row,
    and reported `idempotent: true` on a Course it could never write. The old
    behaviour was a false success rather than a refusal, which is why the name
    says "reported already done" (reviewer, PR #16 round 2).

    Only reachable through already-corrupted data, since every write path here
    hardcodes the status and the column carries no NOT NULL. That is the argument
    for closing it: a response that lies costs more than the bug that produces
    it."""
    course = _make(client)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = NULL WHERE id = ?",
                 (course["id"],))
    conn.commit()
    conn.close()
    r = _supersede(client, course)
    assert r.status_code == 200, r.text
    assert r.json()["idempotent"] is False, (
        "the row was never written, and the response said it had already been")
    assert _courses(db_path)[0]["status"] == store.COURSE_SUPERSEDED
    assert [e["action"] for e in _events(db_path, course["id"])].count(
        store.COURSE_ACTION_SUPERSEDED) == 1


def test_retiring_a_corrupted_row_reports_the_anomaly_to_the_archives(
        client, db_path):
    """**THE RECOVERY MUST NOT LOOK LIKE AN ORDINARY RETIREMENT** (reviewer,
    PR #16 round 2). The `course_events` row a healed corruption leaves is
    indistinguishable from any other supersede, so an operator investigating a
    status anomaly later would be reading a history that says nothing unusual
    happened. The signal is what makes it visible.

    Retitle takes the opposite line on the same row and refuses it outright, and
    the asymmetry is deliberate: renaming a corrupted plan is a change nobody
    needs, and retiring one is what stops it holding a product's live slot."""
    course = _make(client)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = 'archived' WHERE id = ?",
                 (course["id"],))
    conn.commit()
    conn.close()
    assert _supersede(client, course).status_code == 200
    sigs = _signals(db_path)
    assert len(sigs) == 1, "the anomaly went unreported"
    assert sigs[0]["kind"] == "course_status_anomaly_retired"
    assert sigs[0]["product"] == "vivy"
    assert json.loads(sigs[0]["meta"])["prior_status"] == "archived"
    assert course["id"] in sigs[0]["body"]


def test_an_ordinary_retirement_reports_no_anomaly(client, db_path):
    """The other half. A signal on every supersede would make the anomaly signal
    worthless, which is the failure mode of reporting everything."""
    course = _make(client)
    assert _supersede(client, course).status_code == 200
    assert _signals(db_path) == []


def test_a_corrupted_plan_cannot_be_renamed(client, db_path):
    """`_frozen_course` refuses every status that is not `live`, NULL included,
    before the retitle CAS is ever reached. Asserted rather than assumed, since
    the two routes' opposite treatment of the same row is the thing a future
    reader will want proof of."""
    course = _make(client)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = NULL WHERE id = ?",
                 (course["id"],))
    conn.commit()
    conn.close()
    r = client.patch(f"/courses/{course['id']}", json={"title": "renamed"},
                     headers=_h())
    assert r.status_code == 409
    assert _courses(db_path)[0]["title"] == course["title"]


def test_a_successful_supersede_says_it_was_not_idempotent(client):
    """The field is on all three shapes rather than only the two where it is
    true, so a caller reading it does not have to know which shape it got."""
    course = _make(client)
    assert _supersede(client, course).json()["idempotent"] is False
    assert _supersede(client, course).json()["idempotent"] is True


def test_a_reorder_that_loses_to_a_supersede_refuses_and_says_frozen(
        client, db_path):
    """**THE RACE REORDER IS ACTUALLY EXPOSED TO IS A SUPERSEDE, AND THE VERSION
    CAS DID NOT COVER IT** (reviewer, PR #17). A supersede does not touch
    `version`, so a reorder that read `status = live` could commit its entries
    onto a Course another caller had just frozen. That is the silent lost update
    the compare-and-swap exists to rule out, in the one route where the swap
    guarded the wrong column.

    Invisible reviewing this route alone: until the supersede route landed, no
    write path could move a Course off `live`, so there was nothing to race.

    The refusal names the frozen state rather than a version mismatch, because a
    caller told its version is stale would go and rebase onto a plan that can
    never accept the write."""
    import app as a

    course = _make(client, entries=["issue_epic_v"])
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = ?", (store.COURSE_SUPERSEDED,))
    conn.commit()
    conn.close()
    real = a._load_course
    calls = {"n": 0}

    def live_first_look(c, ident, cid):
        # The frozen pre-check is blinded, the rollback re-read is not, which is
        # exactly the state a split check-then-act would leave.
        calls["n"] += 1
        row = real(c, ident, cid)
        if calls["n"] > 1 or row is None:
            return row
        return {**dict(row), "status": store.COURSE_LIVE}

    a._load_course = live_first_look
    try:
        r = _order(client, course, ["issue_epic_v2"])
    finally:
        a._load_course = real
    assert r.status_code == 409, r.text
    assert store.COURSE_SUPERSEDED in r.json()["error"], (
        "a reorder losing to a supersede was told its version was stale")
    assert json.loads(_courses(db_path)[0]["entries"]) == ["issue_epic_v"]
    assert _courses(db_path)[0]["version"] == 1


# --- the hand-off: no approved epic may be stranded by a supersede -------------
#
# The operator, 2026-09-21: "epics that would not be on the superseding course must
# either be added to the course, or closed-won't do." `POST /courses` already
# validates a successor's `entries` through `_validate_entries`; these tests
# cover the new refusal and the atomic hand-off, not entry validation itself,
# which the tests above already hold to the same rules.


def _approve(db_path, item_id):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE issue_items SET maturity = ? WHERE id = ?",
                 (store.ITEM_MATURITY_APPROVED, item_id))
    conn.commit()
    conn.close()


def _wontfix(db_path, item_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE issue_items SET state = 'wontfix', resolution = 'wontfix' "
        "WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def _item_row(db_path, item_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM issue_items WHERE id = ?",
                                 (item_id,)).fetchone())
    finally:
        conn.close()


def test_a_course_with_no_approved_epics_supersedes_with_no_successor(
        client, db_path):
    """The plain call keeps working: a successor is required only when
    refusing would otherwise strand something, and nothing here is approved."""
    course = _make(client, entries=["issue_epic_v", "issue_epic_v2"])
    r = _supersede(client, course)
    assert r.status_code == 200, r.text
    assert r.json()["idempotent"] is False
    assert "successor" not in r.json()
    assert _courses(db_path)[0]["status"] == store.COURSE_SUPERSEDED


def test_an_unapproved_epic_never_blocks_a_supersede(client, db_path):
    """Only `maturity = approved` triggers the rule. An epic that was never
    approved carries no commitment for this rule to protect."""
    course = _make(client, entries=["issue_epic_v"])
    assert _item_row(db_path, "issue_epic_v")["maturity"] is None
    assert _supersede(client, course).status_code == 200


def test_an_approved_epic_with_no_successor_strands_the_supersede(
        client, db_path):
    """**THE NEGATIVE CONTROL for the refusal.** An approved epic, on this
    Course's own entries, neither carried forward nor resolved: the supersede
    is refused by name and nothing is written."""
    course = _make(client, entries=["issue_epic_v", "issue_epic_v2"])
    _approve(db_path, "issue_epic_v")
    r = _supersede(client, course)
    assert r.status_code == 409, r.text
    assert r.json()["stranded"] == ["issue_epic_v"]
    assert _courses(db_path)[0]["status"] == store.COURSE_LIVE, (
        "a refused supersede must not retire the Course")
    assert [e["action"] for e in _events(db_path, course["id"])] == [
        store.COURSE_ACTION_CREATED], "a refused supersede must record nothing"


def test_an_approved_epic_carried_into_the_successor_is_not_stranded(
        client, db_path):
    """The other half of the rule: naming the epic in `successor.entries`
    satisfies it, the predecessor retires, and the successor becomes the
    product's new live Course holding it."""
    course = _make(client, entries=["issue_epic_v", "issue_epic_v2"])
    _approve(db_path, "issue_epic_v")
    r = _supersede(client, course, successor={
        "title": "the next route", "entries": ["issue_epic_v"]})
    assert r.status_code == 200, r.text
    assert r.json()["idempotent"] is False
    assert _courses(db_path)[0]["status"] == store.COURSE_SUPERSEDED

    successor = r.json()["successor"]
    assert successor["status"] == store.COURSE_LIVE
    assert successor["product"] == "vivy"
    assert successor["title"] == "the next route"
    assert successor["entries"] == ["issue_epic_v"]

    # THE NEW COURSE ACTUALLY HOLDS THE PRODUCT'S LIVE SLOT.
    live = client.get("/courses", headers=_h()).json()["courses"]
    assert len(live) == 1
    assert live[0]["id"] == successor["id"]

    events = _events(db_path, successor["id"])
    assert [e["action"] for e in events] == [store.COURSE_ACTION_CREATED]


def test_an_approved_epic_already_resolved_wontfix_does_not_block_it(
        client, db_path):
    """"Closed won't-do" satisfies the rule as well as carrying the epic
    forward does — no successor is needed to retire past it."""
    course = _make(client, entries=["issue_epic_v"])
    _approve(db_path, "issue_epic_v")
    _wontfix(db_path, "issue_epic_v")
    r = _supersede(client, course)
    assert r.status_code == 200, r.text
    assert _courses(db_path)[0]["status"] == store.COURSE_SUPERSEDED


def test_an_approved_epic_off_this_courses_entries_does_not_block_it(
        client, db_path):
    """Scoped to what THIS Course's own `entries` names, not to every approved
    epic of the product — an epic approved onto some other Course is that
    Course's commitment, not this retirement's to check."""
    course = _make(client, entries=["issue_epic_v2"])
    _approve(db_path, "issue_epic_v")  # approved, but never on this Course
    assert _supersede(client, course).status_code == 200


def test_an_already_superseded_course_needs_no_successor_even_when_stranded(
        client, db_path):
    """Idempotent supersede is decided before the hand-off rule is ever
    reached — a retried call must not start requiring a successor it never
    needed the first time, even against a Course an approved epic still
    names."""
    course = _make(client, entries=["issue_epic_v"])
    _approve(db_path, "issue_epic_v")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = ? WHERE id = ?",
                 (store.COURSE_SUPERSEDED, course["id"]))
    conn.commit()
    conn.close()
    r = _supersede(client, course)
    assert r.status_code == 200, r.text
    assert r.json()["idempotent"] is True
    assert "successor" not in r.json()


def test_a_corrupted_status_retires_even_with_a_stranded_approved_epic(
        client, db_path):
    """The anomaly path is not folded into the new refusal: it still recovers
    a corrupted row (reviewer, PR #16) whatever `issue_epic_v`'s maturity is,
    and it still requires no successor to do it."""
    course = _make(client, entries=["issue_epic_v"])
    _approve(db_path, "issue_epic_v")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE courses SET status = 'archived' WHERE id = ?",
                 (course["id"],))
    conn.commit()
    conn.close()
    r = _supersede(client, course)
    assert r.status_code == 200, r.text
    assert _courses(db_path)[0]["status"] == store.COURSE_SUPERSEDED
    assert _signals(db_path)[0]["kind"] == "course_status_anomaly_retired"


def test_a_successor_needs_a_title(client, db_path):
    course = _make(client, entries=["issue_epic_v"])
    r = _supersede(client, course, successor={"entries": []})
    assert r.status_code == 400
    assert r.json()["error"] == "successor.title is required"
    assert _courses(db_path)[0]["status"] == store.COURSE_LIVE


def test_a_successors_entries_must_be_an_array(client, db_path):
    course = _make(client, entries=["issue_epic_v"])
    r = _supersede(client, course,
                   successor={"title": "next", "entries": "issue_epic_v"})
    assert r.status_code == 400
    assert r.json()["error"] == "successor.entries must be an array of epic ids"
    assert _courses(db_path)[0]["status"] == store.COURSE_LIVE


def test_a_successors_entries_take_the_same_membership_rules_as_create(
        client, db_path):
    """`_validate_entries` is reused rather than re-implemented, so an unknown
    id in `successor.entries` is refused exactly as it would be at `POST
    /courses` — and nothing is written, the predecessor included."""
    course = _make(client, entries=["issue_epic_v"])
    r = _supersede(client, course, successor={
        "title": "next", "entries": ["issue_nope"]})
    assert r.status_code == 400
    assert r.json()["unknown"] == ["issue_nope"]
    assert _courses(db_path)[0]["status"] == store.COURSE_LIVE
    assert _courses(db_path, "vivy").__len__() == 1, (
        "a refused successor must not be created")


def test_a_successor_with_no_stranded_epic_is_still_created(client, db_path):
    """`successor` is a general hand-off, not only an escape hatch for the
    refusal — a caller may hand a plan to its replacement with nothing on it
    approved yet."""
    course = _make(client, entries=["issue_epic_v"])
    r = _supersede(client, course, successor={
        "title": "next", "entries": ["issue_epic_v2"]})
    assert r.status_code == 200, r.text
    assert r.json()["successor"]["entries"] == ["issue_epic_v2"]


# --- the hand-off commits atomically, or not at all -----------------------


def test_the_predecessor_stays_live_if_the_successor_write_fails(
        client, db_path):
    """**FORCING THE SECOND WRITE TO FAIL.** `_create_successor_course` runs
    after `_mark_predecessor_superseded` has already executed, uncommitted, in
    the same transaction. If it raises, the whole request fails and closing
    the connection with no `commit()` must discard the predecessor's write
    too — otherwise a Course could end up superseded with no successor ever
    created, the exact half-retirement this hand-off exists to rule out."""
    import app as a

    course = _make(client, entries=["issue_epic_v"])
    _approve(db_path, "issue_epic_v")
    real = a._create_successor_course

    def boom(*a_, **kw):
        raise RuntimeError("forced failure creating the successor")

    a._create_successor_course = boom
    try:
        with pytest.raises(Exception):
            _supersede(client, course, successor={
                "title": "next", "entries": ["issue_epic_v"]})
    finally:
        a._create_successor_course = real
    assert _courses(db_path)[0]["status"] == store.COURSE_LIVE, (
        "the predecessor's retirement was not rolled back with the failed "
        "successor write"
    )
    assert [e["action"] for e in _events(db_path, course["id"])] == [
        store.COURSE_ACTION_CREATED], "the supersede event was not rolled back"
    assert len(_courses(db_path)) == 1, "an orphan successor row was created"


def test_the_successor_is_not_created_if_the_predecessor_write_fails(
        client, db_path):
    """**FORCING THE FIRST WRITE TO FAIL, the other direction.** Nothing after
    `_mark_predecessor_superseded` runs when it raises, so no successor row
    exists either."""
    import app as a

    course = _make(client, entries=["issue_epic_v"])
    _approve(db_path, "issue_epic_v")
    real = a._mark_predecessor_superseded

    def boom(*a_, **kw):
        raise RuntimeError("forced failure retiring the predecessor")

    a._mark_predecessor_superseded = boom
    try:
        with pytest.raises(Exception):
            _supersede(client, course, successor={
                "title": "next", "entries": ["issue_epic_v"]})
    finally:
        a._mark_predecessor_superseded = real
    assert _courses(db_path)[0]["status"] == store.COURSE_LIVE
    assert len(_courses(db_path)) == 1, "a successor was created regardless"
