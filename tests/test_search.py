"""`GET /search`: one ranked list fused from the Reference Room, the Archives,
the Chart House and Reviews.

Every corpus row below carries the same nonsense phrase, `zylofoam gasket`,
chosen because it tokenizes past both the Reference Room's stopword filter and
the fake embedder's word-hash buckets (`conftest.fake_vector`) without
colliding with anything the fixture also uses to distinguish rows — the `kind`
value, the product, or the archived status.

**Vectors are seeded through `vectors.embed_item_by_id`, the same write path
`test_adjacency.py` uses**, rather than through the write routes: this file is
about the read side, and going through `POST /items` or `POST /signals` would
make every test's setup depend on the write surface being enabled and correct.

**`control.db` is built from the real migration**, the same reason
`test_review_record_reads.py` gives: Reviews reads `review_records` through
its uniqueness key, its CHECK constraint and its NOT NULL columns, and a
hand-written second copy of that shape is a copy that drifts. Review records
themselves are seeded through `store.write_review_record` and
`store.set_review_record_signal` — the real store layer, not a mock of it —
exactly as `test_review_record_reads.py`'s own `seed()` does.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import reference
import store
import vectors

REPO = Path(__file__).resolve().parent.parent

AGENT = "agent-token-vivy"
WIDE = "agent-token-wide"
NOREF = "agent-token-noref"
REVIEWER = "agent-token-reviewer"
NOSIGNAL = "agent-token-nosignal"
REPO_SCOPED = "agent-token-repo-scoped"
REPO_AND_CREW = "agent-token-repo-and-crew"

PHRASE = "zylofoam gasket"

DOCS = [
    # kind=spec, current, vivy — the one every "found by the phrase" assertion
    # anchors on.
    ("vivy/specs/gasket", "spec", "vivy", "current",
     f"the {PHRASE} spec describes the seal"),
    # Same phrase, another product — narrowed out by a `product` filter but
    # NOT by scope: the Reference Room is deliberately not product-scoped
    # (reference.py's own module docstring), so a vivy-scoped caller still
    # sees this one and the product-scope test below asserts exactly that.
    ("constance/specs/gasket", "spec", "constance", "current",
     f"a {PHRASE} appears here too"),
    # Archived — excluded by `reference.DEFAULT_STATUSES`, the same default
    # `/reference/search` applies. This is the "restricted document" case.
    ("vivy/decisions/old-gasket", "decision", "vivy", "archived",
     f"an old {PHRASE} decision nobody should see by default"),
]

ITEMS = [
    # The phrase is in the BODY ONLY — the title carries none of it, so this
    # is only findable through the text search this route adds.
    ("item_vivy_gasket", "vivy", "bug", "An open item",
     f"investigate the {PHRASE} failure"),
    ("item_constance_gasket", "constance", "task", "Something else",
     f"a {PHRASE} filed under constance"),
]

SIGNALS = [
    ("sig_vivy_gasket", "vivy", "note", "internal", f"observed a {PHRASE}"),
    ("sig_vivy_customer_gasket", "vivy", "note", "customer",
     f"a customer mentioned a {PHRASE}"),
    ("sig_constance_gasket", "constance", "note", "internal",
     f"a {PHRASE} over in constance"),
]

# Each of these is a review record's own linked signal — the only way Reviews
# reaches the Archives KNN (this route's own docstring). `kind=pr_comment`
# matches the real shape `review_record_backfill.py` links, for realism only;
# nothing here depends on `kind`.
REVIEW_SIGNALS = [
    ("sig_review_vivy_gasket", "vivy", "pr_comment", "internal",
     f"the {PHRASE} review looks solid"),
    ("sig_review_constance_gasket", "constance", "pr_comment", "internal",
     f"the {PHRASE} review needs another pass"),
]

# key, signal_id, product, repo_identity, pr, reviewed_ref, verdict, prose.
# The `constance` row's `pr` is `None`, so its title names `reviewed_ref`
# instead — the branch the route's title-building takes when there is no PR.
REVIEWS = [
    ("vivy", "sig_review_vivy_gasket", "vivy", "github:vivy/widgets", 42,
     "a" * 40, "APPROVE", f"the {PHRASE} review looks solid"),
    ("constance", "sig_review_constance_gasket", "constance",
     "github:constance/widgets", None, "b" * 40, "REQUEST_CHANGES",
     f"the {PHRASE} review needs another pass"),
]

# A second, nonsense phrase for the review record seeded below, distinct from
# PHRASE so it does not join the exact-membership assertions the vault=reviews
# tests above make against REVIEWS alone.
NEW_STYLE_PHRASE = "quonsett bellwarden"


def _ts(**delta):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


def _seed_review(*, signal_id, product, repo_identity, pr, reviewed_ref,
                 verdict, prose):
    """One review record, through the real write path
    (`store.write_review_record` then `store.set_review_record_signal`,
    exactly as `_record_review_signal` calls them from `POST
    /review-records`), returning the minted id."""
    row = {
        "repo_identity": repo_identity, "product": product, "pr": pr,
        "reviewed_ref": reviewed_ref, "role": "reviewer", "run_id": None,
        "verdict": verdict, "findings": [], "prose": prose,
        "paths": [], "doc_ids": [], "item_ids": [],
        "auth": "unverified", "auth_reason": "no run",
        "source_comment_id": None,
    }
    _, _, out = store.write_review_record(row)
    store.set_review_record_signal(out["id"], out["version"], signal_id)
    return out["id"]


def _seed_new_style_review(*, product, repo_identity, pr, reviewed_ref,
                           verdict, prose):
    """One review record whose linked signal is written through
    `app._record_review_signal`, the real function `POST /review-records`
    calls — `kind=review_record`, the shape every record gets today, rather
    than the backfilled `pr_comment` shape `REVIEW_SIGNALS`/`_seed_review`
    above set up by hand. Embedded through `vectors.embed_item_by_id` exactly
    as the route's own `_embed_after_commit` does, so it is reachable only
    when `review_record` is itself embedded (`vectors.EMBEDDED_SIGNAL_KINDS`,
    convivy/library#177, knowledge#692)."""
    row = {
        "repo_identity": repo_identity, "product": product, "pr": pr,
        "reviewed_ref": reviewed_ref, "role": "reviewer", "run_id": None,
        "verdict": verdict, "findings": [], "prose": prose,
        "paths": [], "doc_ids": [], "item_ids": [],
        "auth": "unverified", "auth_reason": "no run",
        "source_comment_id": None,
    }
    _, _, out = store.write_review_record(row)
    ident = identity.Identity(actor="test-seed", kind="system")
    repo_name = repo_identity.split("/", 1)[-1]
    signal_id = libapp._record_review_signal(ident, record=out,
                                             repo_name=repo_name)
    store.set_review_record_signal(out["id"], out["version"], signal_id)
    vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, signal_id)
    return out["id"]


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, type, title, body, state, "
        "severity, needs, created_by_kind, created_by, created_at, refs, "
        "topics) VALUES "
        "(?,?,?,?,?,'open','medium','agent','agent','agent',?,'[]','[]')",
        [(item_id, product, type_, title, body, _ts(days=2))
         for item_id, product, type_, title, body in ITEMS])
    conn.executemany(
        "INSERT INTO signals (id, product, kind, privacy_class, body, "
        "ingested_at, source, actor, ingested_by, refs, meta) "
        "VALUES (?,?,?,?,?,?,'test','test','test','[]','{}')",
        [(sid, prod, kind, priv, body, _ts(days=1))
         for sid, prod, kind, priv, body in SIGNALS + REVIEW_SIGNALS])
    conn.commit()
    conn.close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            id, title, body, topics, content='docs', content_rowid='rowid');
    """)
    for doc_id, kind, project, status, body in DOCS:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, body_hash, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, "[]", "[]", body, "deadbeef", "2026-08-01", "2026-08-02"))
    iconn.execute("INSERT INTO docs_fts(rowid, id, title, body, topics) "
                 "SELECT rowid, id, title, body, topics FROM docs")
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        # **`review_record.read` WITHHELD**, the same grants/withholds
        # mechanism `NOREF` below uses for `reference.read`: `agent` is
        # granted it by default (`identity.ROLE_CAPS`), and this file uses
        # `WIDE` as the "may read every product, but not Reviews" caller —
        # `REVIEWER` below is the "*"-scoped caller that DOES hold it.
        WIDE: {"name": "wide-agent", "products": ["*"],
              "withholds": ["review_record.read"]},
        # `reference.read` withheld through the same grants/withholds
        # mechanism every other identity test in this repo uses
        # (identity._entry_capabilities) — not a second implementation of
        # the capability check.
        NOREF: {"name": "noref-agent", "products": ["*"],
                "withholds": ["reference.read"]},
        REVIEWER: {"name": "reviewer-agent", "products": ["*"]},
        # Holds `review_record.read` (the default) but not `signal.read` —
        # the caller `test_vault_reviews_alone_needs_no_signal_read` below
        # uses to show Reviews does not need the Archives vault's own
        # capability, only the KNN it rides.
        NOSIGNAL: {"name": "nosignal-agent", "products": ["*"],
                  "withholds": ["signal.read"]},
        # The two callers finding 1 needs: one scoped to a review record's
        # linked signal's own product but not the record's, one scoped to
        # both. `_seed_mismatched_review` below seeds the row they read.
        REPO_SCOPED: {"name": "repo-scoped-agent",
                     "products": ["agent-platform"]},
        REPO_AND_CREW: {"name": "repo-and-crew-agent",
                       "products": ["agent-platform", "bosun"]},
    }))

    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    for item_id, *_ in ITEMS:
        vectors.embed_item_by_id(vectors.VAULT_CHART_HOUSE, item_id)
    for sid, *_ in SIGNALS + REVIEW_SIGNALS:
        vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, sid)

    review_ids = {}
    for key, signal_id, product, repo_identity, pr, reviewed_ref, verdict, \
            prose in REVIEWS:
        review_ids[key] = _seed_review(
            signal_id=signal_id, product=product, repo_identity=repo_identity,
            pr=pr, reviewed_ref=reviewed_ref, verdict=verdict, prose=prose)

    c = TestClient(libapp.app)
    c.review_ids = review_ids
    return c


def _h(token=WIDE):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _search(client, token=WIDE, **params):
    params.setdefault("q", PHRASE)
    return client.get("/search", params=params, headers=_h(token))


def _keys(body):
    return {(r["vault"], r["id"]) for r in body["results"]}


# --- the merge itself -----------------------------------------------------


def test_a_document_an_item_and_a_signal_sharing_a_phrase_all_come_back(client):
    body = _search(client).json()
    keys = _keys(body)
    assert ("reference", "vivy/specs/gasket") in keys
    assert ("chart-house", "item_vivy_gasket") in keys
    assert ("archives", "sig_vivy_gasket") in keys
    assert {"searched"} <= set(body)
    assert set(body["searched"]) == {"reference", "archives", "chart-house"}
    # `WIDE` withholds `review_record.read` (see the fixture) so the other
    # three vaults' `not_searched=={}` baseline survives Reviews joining the
    # default search unchanged — the one line this route's fourth vault
    # necessarily touches, since some identity in this suite has to trip its
    # capability gate for the omission tests below to mean anything.
    assert body["not_searched"] == {
        "reviews": "this identity lacks 'review_record.read'"}


def test_a_document_a_work_item_and_a_review_record_sharing_a_phrase_all_come_back(
        client):
    """The unified-search epic's own definition-of-done clause
    (issue_01M2F52F2RV2BCWCKMN3SVMT0K) — `REVIEWER` holds
    `review_record.read`, so its linked signal is absent (the one-prose-once
    rule below) and the review record itself takes its place."""
    body = _search(client, token=REVIEWER).json()
    keys = _keys(body)
    assert ("reference", "vivy/specs/gasket") in keys
    assert ("chart-house", "item_vivy_gasket") in keys
    assert ("reviews", client.review_ids["vivy"]) in keys
    assert ("archives", "sig_review_vivy_gasket") not in keys


def test_every_result_carries_the_documented_fields(client):
    body = _search(client).json()
    for r in body["results"]:
        assert {"vault", "type", "id", "title", "snippet", "score",
                "read_route"} <= set(r)


def test_a_work_item_is_found_by_a_phrase_only_in_its_body(client):
    body = _search(client).json()
    hit = next(r for r in body["results"]
              if r["vault"] == "chart-house" and r["id"] == "item_vivy_gasket")
    assert PHRASE not in hit["title"].lower()
    assert hit["read_route"] == "/items/item_vivy_gasket"


def test_the_same_query_twice_returns_the_same_order(client):
    first = _search(client).json()["results"]
    second = _search(client).json()["results"]
    assert [(r["vault"], r["id"]) for r in first] == \
        [(r["vault"], r["id"]) for r in second]


# --- filters ----------------------------------------------------------------


def test_the_vault_filter_narrows_to_one_vault(client):
    body = _search(client, token=AGENT, vault="archives").json()
    assert body["searched"] == ["archives"]
    assert all(r["vault"] == "archives" for r in body["results"])
    assert _keys(body) == {("archives", "sig_vivy_gasket")}


def test_the_vault_filter_narrows_to_reviews_alone(client):
    body = _search(client, token=REVIEWER, vault="reviews").json()
    assert body["searched"] == ["reviews"]
    assert all(r["vault"] == "reviews" for r in body["results"])
    assert _keys(body) == {("reviews", client.review_ids["vivy"]),
                           ("reviews", client.review_ids["constance"])}
    vivy = next(r for r in body["results"]
               if r["id"] == client.review_ids["vivy"])
    assert "github:vivy/widgets" in vivy["title"]
    assert "42" in vivy["title"]
    assert "APPROVE" in vivy["title"]
    assert vivy["read_route"] == f"/review-records/{client.review_ids['vivy']}"
    assert PHRASE in vivy["snippet"]
    constance = next(r for r in body["results"]
                     if r["id"] == client.review_ids["constance"])
    # `pr` is `None` on this one, so the title names `reviewed_ref` instead.
    assert ("b" * 40) in constance["title"]
    assert "REQUEST_CHANGES" in constance["title"]


def test_a_review_records_kind_review_record_signal_is_found_by_its_prose(
        client):
    """`_seed_new_style_review`'s record links a signal written the real way
    `_record_review_signal` writes it today, `kind=review_record` rather than
    the backfilled `pr_comment` `REVIEWS` above carries — found the same way,
    because `review_record` is itself embedded. Seeded inside this test
    alone, the same way `_seed_mismatched_review` is below, so the extra row
    never reaches the exact-membership assertions the other `vault=reviews`
    tests make against `REVIEWS` alone."""
    review_id = _seed_new_style_review(
        product="vivy", repo_identity="github:vivy/widgets", pr=43,
        reviewed_ref="c" * 40, verdict="APPROVE",
        prose=f"the {NEW_STYLE_PHRASE} review looks solid")
    body = _search(client, token=REVIEWER, vault="reviews",
                   q=NEW_STYLE_PHRASE).json()
    assert ("reviews", review_id) in _keys(body)


def test_vault_reviews_alone_needs_no_signal_read(client):
    """`NOSIGNAL` withholds `signal.read` but holds `review_record.read` —
    `vault=reviews` alone still runs, since Reviews rides the Archives KNN
    (`fetch_archives_knn`'s own `reviews_wanted and reviews_ok` disjunct)
    without needing the Archives vault's own capability, and no Archives
    signal comes back because `vault=reviews` was the only vault asked
    for."""
    body = _search(client, token=NOSIGNAL, vault="reviews").json()
    assert body["searched"] == ["reviews"]
    assert _keys(body) == {("reviews", client.review_ids["vivy"]),
                           ("reviews", client.review_ids["constance"])}
    assert not any(r["vault"] == "archives" for r in body["results"])


def test_an_unrecognised_vault_is_refused_and_names_the_accepted_values(client):
    r = _search(client, vault="nonsense")
    assert r.status_code == 400
    for v in ("reference", "archives", "chart-house"):
        assert v in r.json()["error"]


def test_an_unrecognised_query_parameter_is_refused(client):
    r = _search(client, sort="newest")
    assert r.status_code == 400
    assert r.json()["unknown"] == ["sort"]
    assert r.json()["supported"] == sorted(
        ["kind", "limit", "offset", "product", "q", "vault"])


def test_the_product_filter_narrows_every_vault_at_once(client):
    """The Reference Room is deliberately NOT product-scoped
    (`reference.py`'s own module docstring) — `project` is a caller-chosen
    filter there, exactly as it is here, so this asserts the `product` filter
    narrows it exactly the way it narrows the other two: by request, not by
    identity."""
    body = _search(client, product="vivy").json()
    keys = _keys(body)
    assert ("reference", "vivy/specs/gasket") in keys
    assert ("chart-house", "item_vivy_gasket") in keys
    assert ("archives", "sig_vivy_gasket") in keys
    assert ("reference", "constance/specs/gasket") not in keys
    assert ("chart-house", "item_constance_gasket") not in keys
    assert ("archives", "sig_constance_gasket") not in keys


def test_kind_is_refused_with_no_vault_filter(client):
    """No `vault` searches all four, the Archives, the Chart House and
    Reviews among them, so this is the same refusal as `vault=archives`
    below."""
    r = _search(client, kind="spec")
    assert r.status_code == 400
    assert "kind" in r.json()["error"]
    assert "/signals" in r.json()["hint"] and "/items" in r.json()["hint"]


def test_kind_is_refused_against_archives(client):
    r = _search(client, kind="note", vault="archives")
    assert r.status_code == 400
    assert "kind" in r.json()["error"]


def test_kind_is_refused_against_chart_house(client):
    r = _search(client, kind="bug", vault="chart-house")
    assert r.status_code == 400
    assert "kind" in r.json()["error"]


def test_kind_is_refused_against_reviews(client):
    r = _search(client, token=REVIEWER, kind="pr_comment", vault="reviews")
    assert r.status_code == 400
    assert "kind" in r.json()["error"]
    assert "/review-records" in r.json()["hint"]


def test_kind_is_accepted_and_narrows_against_reference(client):
    """The one vault where `kind` filters inside the FTS `WHERE`, before
    `LIMIT` — exact, not windowed — so it stays allowed here. `kind=spec`
    matches the document's `kind` column and narrows to the two specs."""
    body = _search(client, kind="spec", vault="reference").json()
    assert _keys(body) == {("reference", "vivy/specs/gasket"),
                           ("reference", "constance/specs/gasket")}


# --- authorization: reused from each vault's own route, never re-implemented -


def test_without_reference_read_no_documents_come_back(client):
    body = _search(client, token=NOREF).json()
    assert not any(r["vault"] == "reference" for r in body["results"])
    assert "reference" not in body["searched"]
    assert "reference" in body["not_searched"]
    assert "reference.read" in body["not_searched"]["reference"]


def test_without_review_record_read_reviews_is_silently_omitted(client):
    body = _search(client, token=WIDE).json()
    assert not any(r["vault"] == "reviews" for r in body["results"])
    assert "reviews" not in body["searched"]
    assert "reviews" in body["not_searched"]
    assert "review_record.read" in body["not_searched"]["reviews"]


def test_a_product_scoped_caller_sees_nothing_from_another_product_in_the_scoped_vaults(
        client):
    """`AGENT` is scoped to `vivy` alone. The Chart House and the Archives are
    product-scoped through `_scope_clause`, the exact function `GET /items`
    and `GET /signals/search` already call, so a constance row from either is
    absent — not merely unranked. The Reference Room is unaffected, matching
    its own route (see the product-filter test above for why)."""
    body = _search(client, token=AGENT).json()
    keys = _keys(body)
    assert ("chart-house", "item_constance_gasket") not in keys
    assert ("archives", "sig_constance_gasket") not in keys
    assert ("chart-house", "item_vivy_gasket") in keys
    assert ("archives", "sig_vivy_gasket") in keys


def test_a_product_scoped_caller_sees_no_other_products_review_record(client):
    """`AGENT` is scoped to `vivy` alone and holds `review_record.read` by
    default (`identity.ROLE_CAPS["agent"]`) — Reviews is scoped by
    `_scope_clause` on `review_records.product`, the exact function every
    other vector vault above already uses, so the `constance` review record
    is absent rather than merely unranked."""
    body = _search(client, token=AGENT, vault="reviews").json()
    assert _keys(body) == {("reviews", client.review_ids["vivy"])}


def test_without_signal_customer_no_customer_class_signal_comes_back(client):
    body = _search(client, token=AGENT).json()
    assert ("archives", "sig_vivy_customer_gasket") not in _keys(body)


def test_a_restricted_document_is_absent(client):
    """`reference.DEFAULT_STATUSES` excludes `archived`, exactly as
    `/reference/search` excludes it by default — reused, not re-decided
    here."""
    body = _search(client).json()
    assert ("reference", "vivy/decisions/old-gasket") not in _keys(body)


def _seed_mismatched_review(client):
    """A review record's own product need not match its linked signal's —
    the real shape the backfill left behind: a repository name on the
    signal, the crew that owns that repository on the record.
    `agent-platform`/`bosun` names the largest such pair in the live data
    (2,326 of 10,669 linked rows, measured 2026-09-30 00:25 MST). Returns
    the minted review record id."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO signals (id, product, kind, privacy_class, body, "
        "ingested_at, source, actor, ingested_by, refs, meta) "
        "VALUES (?,?,?,?,?,?,'test','test','test','[]','{}')",
        ("sig_review_repo_gasket", "agent-platform", "pr_comment", "internal",
         f"the {PHRASE} review looks solid from ci", _ts(days=1)))
    conn.commit()
    conn.close()
    vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_review_repo_gasket")
    return _seed_review(
        signal_id="sig_review_repo_gasket", product="bosun",
        repo_identity="github:agent-platform/foo", pr=None,
        reviewed_ref="c" * 40, verdict="APPROVE",
        prose=f"the {PHRASE} review looks solid from ci")


def test_a_caller_scoped_to_the_signals_product_alone_keeps_the_signal_and_not_the_record(
        client):
    """Finding 1: `REPO_SCOPED` reads `agent-platform` (the signal's
    product) but not `bosun` (the record's), so `_scope_clause` on
    `review_records.product` does not admit the record — the swap does not
    apply, and the signal stays in the Archives half exactly as it would
    with no Reviews vault at all."""
    _seed_mismatched_review(client)
    body = _search(client, token=REPO_SCOPED).json()
    keys = _keys(body)
    assert ("archives", "sig_review_repo_gasket") in keys
    assert not any(r["vault"] == "reviews" for r in body["results"])


def test_a_caller_scoped_to_both_products_gets_the_record_not_the_signal(
        client):
    """Finding 1, the other side: `REPO_AND_CREW` reads both
    `agent-platform` (the signal's product) and `bosun` (the record's), so
    `_scope_clause` admits the record and the swap applies — the review
    record replaces the signal in the merged results."""
    review_id = _seed_mismatched_review(client)
    body = _search(client, token=REPO_AND_CREW).json()
    keys = _keys(body)
    assert ("archives", "sig_review_repo_gasket") not in keys
    assert ("reviews", review_id) in keys


# --- one prose once: a review record's linked signal is not also a result --


def test_with_review_record_read_the_linked_signal_is_absent_and_the_record_present(
        client):
    body = _search(client, token=REVIEWER, product="vivy").json()
    keys = _keys(body)
    assert ("archives", "sig_review_vivy_gasket") not in keys
    assert ("reviews", client.review_ids["vivy"]) in keys


def test_without_review_record_read_the_signal_is_present_and_no_record_comes_back(
        client):
    body = _search(client, token=WIDE, product="vivy").json()
    keys = _keys(body)
    assert ("archives", "sig_review_vivy_gasket") in keys
    assert not any(r["vault"] == "reviews" for r in body["results"])


def test_the_swap_applies_to_vault_archives_alone_even_without_vault_reviews(
        client):
    """The docstring's own claim: `vault=archives` changes for a
    `review_record.read` caller whether or not that caller also asked for
    `vault=reviews`."""
    body = _search(client, token=REVIEWER, vault="archives",
                   product="vivy").json()
    assert body["searched"] == ["archives"]
    assert ("archives", "sig_review_vivy_gasket") not in _keys(body)
    assert ("archives", "sig_vivy_gasket") in _keys(body)


# --- the vector index being unavailable is reported, not silently dropped --


def test_an_unavailable_vector_index_is_reported_as_not_searched(
        client, monkeypatch):
    monkeypatch.setattr(vectors, "available",
                        lambda *a, **k: (False, "no vectors database"))
    body = _search(client).json()
    assert body["not_searched"]["archives"] == "no vectors database"
    assert body["not_searched"]["chart-house"] == "no vectors database"
    assert "archives" not in body["searched"]
    assert "chart-house" not in body["searched"]
    # The Reference Room does not depend on the vector index at all.
    assert "reference" in body["searched"]
    assert ("reference", "vivy/specs/gasket") in _keys(body)


def test_an_unavailable_vector_index_also_reports_reviews_as_not_searched(
        client, monkeypatch):
    """Reviews has no vector index of its own, but it reads its candidates
    through the Archives KNN (this route's own docstring), so the Archives
    index being unavailable takes Reviews down with it — reported the same
    way, naming why."""
    monkeypatch.setattr(vectors, "available",
                        lambda *a, **k: (False, "no vectors database"))
    body = _search(client, token=REVIEWER).json()
    assert body["not_searched"]["reviews"] == "no vectors database"
    assert "reviews" not in body["searched"]


def test_scores_are_sorted_and_a_tie_is_broken_by_vault_then_id(client):
    """Rank-1 in one vault always ties rank-1 in another under RRF — both
    score `1 / (SEARCH_RRF_K + 1)` regardless of the native retrieval score
    behind them — so an unfiltered query against a corpus with a hit in
    every vault reliably produces a real tie to break, every run."""
    results = _search(client).json()["results"]
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    for _, group in itertools.groupby(results, key=lambda r: r["score"]):
        tied = list(group)
        keys = [(r["vault"], r["id"]) for r in tied]
        assert keys == sorted(keys)


# --- filtered_out: a product filter's own removals, not the window's, and --
# --- never the scope wall's --------------------------------------------


def test_filtered_out_is_zero_for_every_vector_vault_with_no_product_filter(
        client):
    body = _search(client).json()
    assert body["filtered_out"] == {"archives": 0, "chart-house": 0}


def _ranked_archives_window(client, monkeypatch):
    """Replaces the fixture's own hash-embedded Archives vectors with three
    whose distance from the query is exact rather than a coincidence of
    which words happen to hash together — the same technique
    `test_read_surface.py`'s `vec` fixture uses. Two rows sit inside a
    size-2 KNN window (`OVERSAMPLE` patched to 1, so `?limit=1` gives a
    window of `candidate_n(2) * 1`); the third sits outside it."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    main = sqlite3.connect(store.DB_PATH)
    main.executemany(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at, source, actor, ingested_by, refs, meta) "
        "VALUES (?,?,?,?,?,?,'test','test','test','[]','{}')",
        [("sig_near_vivy", "vivy", "note", "a near row", "internal",
          _ts(days=1)),
         ("sig_near_constance", "constance", "note", "a near row too",
          "internal", _ts(days=1)),
         ("sig_far_rare", "rare", "note", "a far row", "internal",
          _ts(days=1))])
    main.commit()
    main.close()

    # The vec0 table lives in its own attached file (`vector_index`'s
    # `<db>.vectors`, pointed to by `vectors.VECTORS_DB_PATH`), not in the
    # main database above — the split `test_split_vectors.py` covers.
    vconn = sqlite3.connect(vectors.VECTORS_DB_PATH)
    vconn.enable_load_extension(True)
    sqlite_vec.load(vconn)
    vconn.enable_load_extension(False)
    vconn.execute(f"DELETE FROM {vectors.VEC_TABLE} WHERE vault = ?",
                 (vectors.VAULT_ARCHIVES,))

    def vector_for(seed):
        return struct.pack(f"{vectors.EMBED_DIM}f",
                           *([seed] + [0.0] * (vectors.EMBED_DIM - 1)))

    # Ascending distance from an all-zero query vector: the two near rows
    # fall inside a size-2 window, the far one does not.
    for sid, seed in (("sig_near_vivy", 0.0), ("sig_near_constance", 0.1),
                      ("sig_far_rare", 0.9)):
        vconn.execute(
            f"INSERT INTO {vectors.VEC_TABLE}(embedding, vault, item_id) "
            "VALUES (?,?,?)",
            (vector_for(seed), vectors.VAULT_ARCHIVES, sid))
    vconn.commit()
    vconn.close()
    monkeypatch.setattr(vectors, "_embedder",
                        lambda texts: [[0.0] * vectors.EMBED_DIM])
    monkeypatch.setattr(vectors, "OVERSAMPLE", 1)


def test_a_product_filter_matching_only_a_row_outside_the_window_reports_filtered_out(
        client, monkeypatch):
    """`?product=rare&limit=1` gives a window of 2, which holds
    `sig_near_vivy` and `sig_near_constance` and never reaches
    `sig_far_rare` at all. Neither window row is `rare`, so the page is
    empty — and `filtered_out` says that is the caller's own filter, not
    an empty corpus."""
    _ranked_archives_window(client, monkeypatch)
    body = _search(client, token=WIDE, vault="archives", limit=1,
                  product="rare").json()
    assert body["count"] == 0
    assert body["filtered_out"] == {"archives": 2}, (
        "the window holds two rows and neither is 'rare', so the caller's "
        "own product filter is what emptied the page")
    exists = client.get("/signals?product=rare", headers=_h(WIDE)).json()
    assert [s["id"] for s in exists["signals"]] == ["sig_far_rare"], (
        "the row the search could not reach exists outside the window, so "
        "the assertion above is about a window filtered to nothing rather "
        "than an empty table")


def test_a_scoped_callers_filtered_out_excludes_rows_the_scope_wall_removed(
        client, monkeypatch):
    """AGENT is scoped to `vivy` alone, so `sig_near_constance` never
    reaches its scoped window at all — the scope clause removes it before
    `filtered_out` starts counting, leaving one row for the caller's own
    `product` filter to remove. WIDE sees both near rows, so its `product`
    filter removes both: the control that shows the assertion above is
    about the wall and not a window that only ever held one row."""
    _ranked_archives_window(client, monkeypatch)
    scoped = _search(client, token=AGENT, vault="archives", limit=1,
                     product="rare").json()
    wide = _search(client, token=WIDE, vault="archives", limit=1,
                   product="rare").json()
    assert scoped["filtered_out"] == {"archives": 1}
    assert wide["filtered_out"] == {"archives": 2}


def _ranked_chart_house_window(client, monkeypatch):
    """The Chart House twin of `_ranked_archives_window` above: same
    technique, `issue_items` in place of `signals`, and no customer wall,
    since work items carry no `privacy_class`."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    main = sqlite3.connect(store.DB_PATH)
    main.executemany(
        "INSERT INTO issue_items (id, product, type, title, body, state, "
        "severity, needs, created_by_kind, created_by, created_at, refs, "
        "topics) VALUES "
        "(?,?,?,?,?,'open','medium','agent','agent','agent',?,'[]','[]')",
        [("item_near_vivy", "vivy", "bug", "a near item", "a near item",
          _ts(days=1)),
         ("item_near_constance", "constance", "bug", "a near item too",
          "a near item too", _ts(days=1)),
         ("item_far_rare", "rare", "bug", "a far item", "a far item",
          _ts(days=1))])
    main.commit()
    main.close()

    vconn = sqlite3.connect(vectors.VECTORS_DB_PATH)
    vconn.enable_load_extension(True)
    sqlite_vec.load(vconn)
    vconn.enable_load_extension(False)
    vconn.execute(f"DELETE FROM {vectors.VEC_TABLE} WHERE vault = ?",
                 (vectors.VAULT_CHART_HOUSE,))

    def vector_for(seed):
        return struct.pack(f"{vectors.EMBED_DIM}f",
                           *([seed] + [0.0] * (vectors.EMBED_DIM - 1)))

    # Ascending distance from an all-zero query vector: the two near rows
    # fall inside a size-2 window, the far one does not.
    for iid, seed in (("item_near_vivy", 0.0), ("item_near_constance", 0.1),
                      ("item_far_rare", 0.9)):
        vconn.execute(
            f"INSERT INTO {vectors.VEC_TABLE}(embedding, vault, item_id) "
            "VALUES (?,?,?)",
            (vector_for(seed), vectors.VAULT_CHART_HOUSE, iid))
    vconn.commit()
    vconn.close()
    monkeypatch.setattr(vectors, "_embedder",
                        lambda texts: [[0.0] * vectors.EMBED_DIM])
    monkeypatch.setattr(vectors, "OVERSAMPLE", 1)


def test_a_product_filter_matching_only_a_chart_house_row_outside_the_window_reports_filtered_out(
        client, monkeypatch):
    """The Chart House twin of the archives test above: `?product=rare&
    limit=1` gives a window of 2, holding `item_near_vivy` and
    `item_near_constance` and never reaching `item_far_rare`. Neither window
    row is `rare`, so the page is empty and `filtered_out` says that is the
    caller's own filter, not an empty corpus."""
    _ranked_chart_house_window(client, monkeypatch)
    body = _search(client, token=WIDE, vault="chart-house", limit=1,
                  product="rare").json()
    assert body["count"] == 0
    assert body["filtered_out"] == {"chart-house": 2}, (
        "the window holds two rows and neither is 'rare', so the caller's "
        "own product filter is what emptied the page")
    exists = client.get("/items?product=rare", headers=_h(WIDE)).json()
    assert [i["id"] for i in exists["items"]] == ["item_far_rare"], (
        "the row the search could not reach exists outside the window, so "
        "the assertion above is about a window filtered to nothing rather "
        "than an empty table")


def test_a_scoped_callers_chart_house_filtered_out_excludes_rows_the_scope_wall_removed(
        client, monkeypatch):
    """AGENT is scoped to `vivy` alone, so `item_near_constance` never
    reaches its scoped window at all — the scope clause removes it before
    `filtered_out` starts counting, leaving one row for the caller's own
    `product` filter to remove. WIDE sees both near rows, so its `product`
    filter removes both: the control that shows the assertion above is
    about the wall and not a window that only ever held one row."""
    _ranked_chart_house_window(client, monkeypatch)
    scoped = _search(client, token=AGENT, vault="chart-house", limit=1,
                     product="rare").json()
    wide = _search(client, token=WIDE, vault="chart-house", limit=1,
                   product="rare").json()
    assert scoped["filtered_out"] == {"chart-house": 1}
    assert wide["filtered_out"] == {"chart-house": 2}


def _ranked_reviews_window(client, monkeypatch):
    """The Reviews twin of `_ranked_archives_window` above: reuses that
    exact Archives KNN window (three signals at controlled distances,
    `sig_near_vivy`/`sig_near_constance` inside a size-2 window and
    `sig_far_rare` outside it) and links one review record to each, so the
    same near/near/far shape governs which review records fall inside the
    window — Reviews has no vector of its own and takes its candidates from
    exactly this KNN."""
    _ranked_archives_window(client, monkeypatch)
    for signal_id, product, ref in (("sig_near_vivy", "vivy", "d" * 40),
                                    ("sig_near_constance", "constance",
                                     "e" * 40),
                                    ("sig_far_rare", "rare", "f" * 40)):
        _seed_review(signal_id=signal_id, product=product,
                     repo_identity=f"github:{product}/windowed", pr=None,
                     reviewed_ref=ref, verdict="APPROVE",
                     prose=f"a review of {signal_id}")


def test_a_product_filter_matching_only_a_review_outside_the_window_reports_filtered_out(
        client, monkeypatch):
    """The Reviews twin of the archives and chart-house tests above:
    `?product=rare&limit=1` gives a window of 2, holding the reviews linked
    to `sig_near_vivy` and `sig_near_constance` and never reaching the one
    linked to `sig_far_rare`. Neither window row is `rare`, so the page is
    empty and `filtered_out` says that is the caller's own filter, not an
    empty corpus."""
    _ranked_reviews_window(client, monkeypatch)
    body = _search(client, token=REVIEWER, vault="reviews", limit=1,
                  product="rare").json()
    assert body["count"] == 0
    assert body["filtered_out"] == {"reviews": 2}, (
        "the window holds two reviews and neither is 'rare', so the "
        "caller's own product filter is what emptied the page")


def test_a_scoped_callers_reviews_filtered_out_excludes_rows_the_scope_wall_removed(
        client, monkeypatch):
    """AGENT is scoped to `vivy` alone, so the review linked to
    `sig_near_constance` never reaches its scoped window at all — the scope
    clause removes it before `filtered_out` starts counting, leaving one row
    for the caller's own `product` filter to remove. REVIEWER, scoped to
    `*`, sees both near reviews, so its `product` filter removes both."""
    _ranked_reviews_window(client, monkeypatch)
    scoped = _search(client, token=AGENT, vault="reviews", limit=1,
                     product="rare").json()
    wide = _search(client, token=REVIEWER, vault="reviews", limit=1,
                   product="rare").json()
    assert scoped["filtered_out"] == {"reviews": 1}
    assert wide["filtered_out"] == {"reviews": 2}
