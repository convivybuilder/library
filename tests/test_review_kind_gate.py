"""The review-outcome kinds are gated on a capability `agent` does not hold.

`POST /signals` used to admit any kind to any `signal.create` holder, so an
`agent`-role credential — every co session, every dispatched agent — could write
a `review_verdict` row indistinguishable from the GitHub capture poller's. That
row is the raw material a conditions-based merge gate would read: who approved a
PR, at which head. If its writer can mint it, a coder writes its own approval,
and the gate reads a field its subject controls.

This pins the fix in both places: the capability shape in `ROLE_CAPS` (granted
to `system`, absent from `agent`, structural for the derived roles), and the
route behaviour (the kinds refuse for `agent` and `customer_reader`, admit for
`system`, and an unprotected kind moves nothing).
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

AGENT = "agent-token"
READER = "customer-reader-token"
SYSTEM = "system-token"


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
        AGENT: {"name": "an-agent", "products": ["vivy"]},
        READER: {"name": "commercial", "products": ["vivy"],
                 "kind": "customer_reader"},
        SYSTEM: {"name": "github-capture", "products": ["*"], "kind": "system"},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"_meta": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _review_body(kind):
    """A payload shaped like the GitHub capture poller's (`github_capture.py`)."""
    return {"source": "github_review", "kind": kind, "actor": "octocat",
            "product": "vivy", "body": "approved",
            "refs": {"pr": 1, "url": "https://github.com/convivy/x/pull/1"},
            "meta": {"state": "APPROVED"},
            "produced_at": "2026-08-01T00:00:00Z"}


# --- the capability shape, asserted directly ------------------------------------


def test_the_capability_is_granted_to_system_not_agent():
    """`system` holds the review write; `agent` does not. The derived roles fall
    out of the unions: `approved_client` derives from `system` (so it holds it),
    `customer_reader` from `agent` (so it does not)."""
    assert "signal.create_review" in identity.ROLE_CAPS["system"]
    assert "signal.create_review" in identity.ROLE_CAPS["approved_client"]
    assert "signal.create_review" not in identity.ROLE_CAPS["agent"]
    assert "signal.create_review" not in identity.ROLE_CAPS["customer_reader"]


# --- route behaviour: the protected kinds --------------------------------------


@pytest.mark.parametrize("kind", sorted(store.SIGNAL_REVIEW_KINDS))
@pytest.mark.parametrize("token", [AGENT, READER])
def test_an_agent_role_identity_is_refused_a_protected_kind(client, token, kind):
    """The identity the gate exists to exclude — and the reader role derived from
    it — cannot mint a review row. The refusal names the capability, so the
    caller's operator knows what to grant."""
    r = client.post("/signals", json=_review_body(kind), headers=_h(token))
    assert r.status_code == 403
    assert "signal.create_review" in r.json()["error"]


@pytest.mark.parametrize("kind", sorted(store.SIGNAL_REVIEW_KINDS))
def test_a_system_identity_is_accepted_on_a_protected_kind(client, kind):
    """The GitHub capture poller resolves to `kind: system`, so its writes keep
    landing — the live-ingest half of the fix."""
    r = client.post("/signals", json=_review_body(kind), headers=_h(SYSTEM))
    assert r.status_code == 201
    assert r.json()["kind"] == kind
    assert r.json()["ingested_by"] == "github-capture"


# --- padded and cased variants do not route around the gate --------------------


@pytest.mark.parametrize("kind", sorted(store.SIGNAL_REVIEW_KINDS))
@pytest.mark.parametrize("token", [AGENT, READER])
@pytest.mark.parametrize("variant", ["{k} ", " {k}", "\t{k}", "  {k}  "])
def test_a_padded_variant_of_a_protected_kind_is_still_refused(
        client, token, kind, variant):
    """The required-field check tests only the stripped form of `kind`, so a
    padded protected kind clears the presence check — the membership test must
    see the stripped value, or a trailing space is a spelling route around the
    capability."""
    r = client.post("/signals", json=_review_body(variant.format(k=kind)),
                    headers=_h(token))
    assert r.status_code == 403
    assert "signal.create_review" in r.json()["error"]


@pytest.mark.parametrize("kind", sorted(store.SIGNAL_REVIEW_KINDS))
@pytest.mark.parametrize("token", [AGENT, READER])
@pytest.mark.parametrize("variant", [str.upper, str.capitalize],
                         ids=["uppercased", "capitalized"])
def test_a_cased_variant_of_a_protected_kind_is_still_refused(
        client, token, kind, variant):
    """A cased variant of a protected kind is still the protected kind at the
    gate, which matches case-insensitively — otherwise `Review_Verdict` is a
    spelling route around the capability for the identity the gate excludes."""
    r = client.post("/signals", json=_review_body(variant(kind)),
                    headers=_h(token))
    assert r.status_code == 403
    assert "signal.create_review" in r.json()["error"]


@pytest.mark.parametrize("kind", sorted(store.SIGNAL_REVIEW_KINDS))
def test_a_system_identity_storing_a_padded_protected_kind_stores_the_stripped_kind(
        client, kind):
    """Normalisation happens at storage as well as at the gate — a padded kind a
    capability holder writes must not land in the column padded, or the stored
    row would not equal the canonical kind a merge gate looks for."""
    r = client.post("/signals", json=_review_body(f"  {kind}  "),
                    headers=_h(SYSTEM))
    assert r.status_code == 201
    assert r.json()["kind"] == kind


# --- route behaviour: an unprotected kind is unaffected ------------------------


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_an_unprotected_kind_is_unaffected(client, token):
    """The gate keys on the kind, not on `signal.create_review` broadly — a note
    or changelog capture still needs only the general capability, which both
    roles hold."""
    body = _review_body("note")
    body["source"] = "github"
    body["meta"] = {}
    r = client.post("/signals", json=body, headers=_h(token))
    assert r.status_code == 201
    assert r.json()["kind"] == "note"


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_a_padded_unprotected_kind_is_stored_stripped(client, token):
    """Stripping is not the protected kinds' special case — a padded open kind
    was previously stored with its padding, a row a filter matching
    `kind == "note"` would silently miss."""
    body = _review_body("  note  ")
    body["source"] = "github"
    body["meta"] = {}
    r = client.post("/signals", json=body, headers=_h(token))
    assert r.status_code == 201
    assert r.json()["kind"] == "note"


# --- characters outside printable ASCII are refused, for every identity ------
#
# `.strip()` removes only Unicode whitespace, so a zero-width character (U+200B
# and friends, category Cf) is not whitespace: a protected kind carrying one
# cleared the presence check, survived the strip, failed the exact membership
# test, and stored a kind that reads as `review_verdict` to any terminal, log
# viewer or substring match. The route refuses any character outside printable
# ASCII rather than growing the set normalisation removes, and the refusal
# applies to every identity — a malformed kind is a caller error, not a
# capability matter, which is why a `system` token is refused as well as an
# `agent` one.


@pytest.mark.parametrize("kind", sorted(store.SIGNAL_REVIEW_KINDS) + ["note"])
@pytest.mark.parametrize("ch", [chr(0x200B), chr(0x200C), chr(0xFEFF),
                                chr(0x00AD)])
@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_a_kind_carrying_a_zero_width_character_is_refused(
        client, token, kind, ch):
    """Zero-width and other format characters survive `.strip()`, so a kind
    carrying one would land reading as a different kind than it compares as.
    Refused with a 400 for both the protected and the open kinds."""
    r = client.post("/signals", json=_review_body(kind + ch), headers=_h(token))
    assert r.status_code == 400
    assert "kind" in r.json()["error"]


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_a_zero_width_character_spliced_into_a_protected_kind_is_refused(
        client, token):
    """The character can sit anywhere in the string, not only at the edge —
    splicing U+200B between `review` and `_verdict` reads identically in a
    terminal. Refused the same way."""
    r = client.post(
        "/signals",
        json=_review_body("review" + chr(0x200B) + "_verdict"),
        headers=_h(token))
    assert r.status_code == 400


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_a_kind_carrying_a_combining_mark_is_refused(client, token):
    """A combining mark is the same class as a zero-width character — invisible
    in a terminal, absent from an exact match — so it is refused rather than
    normalised away, even for an identity that holds the review capability."""
    r = client.post(
        "/signals",
        json=_review_body("review" + chr(0x0301) + "_verdict"),
        headers=_h(token))
    assert r.status_code == 400


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_a_kind_carrying_a_non_ascii_letter_is_refused(client, token):
    """The boundary is printable ASCII rather than a block-list of format and
    mark categories: a Cyrillic homoglyph letter is printable and category-clean
    (not Cf/Cc/Co/Cs/Mn), yet still makes the kind read as something it does
    not byte-for-byte equal. Only the ASCII line closes the confusable class."""
    r = client.post(
        "/signals",
        json=_review_body("review_verdict" + chr(0x0435)),  # Cyrillic small letter ie, a homoglyph of the Latin e
        headers=_h(token))
    assert r.status_code == 400


def test_the_refusal_echoes_the_kind_escaped_not_invisible(client):
    """The refusal names what was refused without letting the invisible
    character travel into a log unmarked: the echoed kind is
    unicode_escape-encoded, so the body carries the six characters
    `\u200b`, never a raw zero-width space."""
    r = client.post(
        "/signals",
        json=_review_body("review_verdict" + chr(0x200B)),
        headers=_h(AGENT))
    assert r.status_code == 400
    assert chr(0x200B) not in r.text
    assert r.json()["kind"] == ("review_verdict" + chr(0x200B)).encode(
        "unicode_escape").decode("ascii")


# --- a protected kind is stored under its canonical lowercase spelling --------


@pytest.mark.parametrize("kind", sorted(store.SIGNAL_REVIEW_KINDS))
@pytest.mark.parametrize("variant", [str.upper, str.capitalize],
                         ids=["uppercased", "capitalized"])
def test_a_system_identity_storing_a_cased_protected_kind_stores_the_canonical(
        client, kind, variant):
    """The protected set is closed and lowercase, so a kind that matched it is
    stored as its canonical spelling — otherwise an authorised `Review_Verdict`
    write lands in the column as a spelling the exact-match filters and a merge
    gate would silently miss."""
    r = client.post("/signals", json=_review_body(variant(kind)),
                    headers=_h(SYSTEM))
    assert r.status_code == 201
    assert r.json()["kind"] == kind


# --- the dedup echo answers only a caller that could have created the row -----


def test_a_review_row_is_not_echoed_to_an_identity_that_could_not_write_it(
        client):
    """A `dedup_key` collision with a review-kind row refuses a caller without
    `signal.create_review` — the same "could have created it" rule the write
    gate applies — so a bare `note` write cannot read a `review_verdict` row's
    content back through its own dedup key."""
    dedup = "shared-dedup-key"
    body = _review_body("review_verdict")
    body["dedup_key"] = dedup
    first = client.post("/signals", json=body, headers=_h(SYSTEM))
    assert first.status_code == 201

    probe = _review_body("note")
    probe["source"] = "github"
    probe["meta"] = {}
    probe["dedup_key"] = dedup
    r = client.post("/signals", json=probe, headers=_h(AGENT))
    assert r.status_code == 409
    assert "idempotent" not in r.json()
    assert "body" not in r.json()  # no row content disclosed


def test_a_review_row_is_echoed_to_an_identity_that_could_write_it(client):
    """The capability holder keeps dedup idempotency on the review kinds: the
    second identical capture answers 200 with the existing row rather than a
    duplicate."""
    dedup = "shared-dedup-key"
    body = _review_body("review_verdict")
    body["dedup_key"] = dedup
    first = client.post("/signals", json=body, headers=_h(SYSTEM))
    assert first.status_code == 201
    second = client.post("/signals", json=body, headers=_h(SYSTEM))
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    assert second.json()["kind"] == "review_verdict"


def test_an_open_kind_row_is_still_echoed_to_its_author(client):
    """The review-kind leg does not narrow the dedup echo for rows the caller
    could have written: an agent's own `note` row still answers idempotently."""
    dedup = "authors-own-key"
    body = _review_body("note")
    body["source"] = "github"
    body["meta"] = {}
    body["dedup_key"] = dedup
    first = client.post("/signals", json=body, headers=_h(AGENT))
    assert first.status_code == 201
    second = client.post("/signals", json=body, headers=_h(AGENT))
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
