"""POST /tokens/redeem: the call slip's counterpart (ADR-0062,
issue_01M12NE13P3FEY3HF7732QX03F).

Mechanics, and tests that bite:

- Single-use, enforced against a race rather than a read — a second
  redemption of an already-redeemed slip must fail while the first stands.
- The same race, constructed with real concurrent threads against a real
  on-disk database rather than asserted from the single-threaded path alone.
- The slip's TTL, enforced here even though the slip was never redeemed.
- The transport actor cross-check (ADR-0062): a redemption is accepted only
  over the mutual-TLS listener, from a registered, unrevoked machine, with a
  credential bound to that machine. Refused on the plain listener, on a
  credential bound elsewhere or nowhere, and on a revoked or unregistered
  certificate. The refusals that need a real handshake run a real listener.
- The capability gate: a credential missing `token.redeem` gets 403.

`client` drives the whole app, middleware included, through `_OverMTLS`, a
wrapper that stamps a real generated certificate into the scope the way
`mtls.MTLSH11Protocol` does; the cross-check runs unstubbed. `plain_client`
is the same app with nothing stamped, which is what a request on
127.0.0.1:8100 looks like.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import sqlite3
import ssl
import threading

import pytest
from fastapi.testclient import TestClient

import app as libapp
import call_slips
import credential_binding
import identity
import machine_identity
import mtls
import protocol
import store
import tls_ca
from mtls_server import RunningServer, tls_config

AGENT = "agent-token-vivy"
OTHER_SCOPE = "agent-token-constance-only"
MACHINE = "macbook"
OTHER_MACHINE = "keelboat"


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


def _register(db, machine, der, *, status="active"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO machine_certificates "
        "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
        "VALUES (?, ?, '2099-01-01', ?, '2026-01-01', 'hardware')",
        (machine, hashlib.sha256(der).hexdigest(), status))
    conn.commit()
    conn.close()


def _bind(db, token, machine):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO credential_bindings "
        "(credential_sha256, machine, note, created_at) "
        "VALUES (?, ?, NULL, '2026-01-01')",
        (credential_binding.sha256_token(token), machine))
    conn.commit()
    conn.close()


def _der(cert_path):
    return ssl.PEM_cert_to_DER_cert(cert_path.read_text())


class _OverMTLS:
    """Stamps `peer_cert`/`peer_cert_der` into every HTTP scope, as
    `mtls.MTLSH11Protocol` does at `connection_made`. The certificate is a
    real one and the app's own `MachineIdentityGate` resolves it against the
    registry; only the TLS handshake is absent."""

    def __init__(self, app, der, uri):
        self.app = app
        self.info = {"peer_cert": {"subjectAltName": (("URI", uri),)},
                     "peer_cert_der": der}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "extensions": {
                **scope.get("extensions", {}), mtls.EXTENSION_KEY: self.info}}
        await self.app(scope, receive, send)


def _uri(machine):
    return f"{machine_identity.MACHINE_URI_PREFIX}{machine}"


@pytest.fixture
def base_client(tmp_path, monkeypatch):
    """The app with its database, tokens and CA, and the plain listener's
    client. `base_client.pki` carries what the TLS-shaped clients need."""
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, type TEXT,
            state TEXT, needs TEXT, created_by_kind TEXT, parent_id TEXT,
            created_at TEXT, refs TEXT);
        CREATE TABLE call_slips (
            id TEXT PRIMARY KEY, item_id TEXT, product TEXT,
            claim_owner_label TEXT, issued_at TEXT, expires_at TEXT,
            redeemed_at TEXT);
        CREATE TABLE library_cards (
            id TEXT PRIMARY KEY, actor TEXT, product TEXT, item_id TEXT,
            slip_id TEXT UNIQUE, protocol_version INTEGER, state TEXT,
            issued_at TEXT, revoked_at TEXT);
        CREATE TABLE machine_certificates (
            id INTEGER PRIMARY KEY,
            machine TEXT NOT NULL,
            fingerprint_sha256 TEXT NOT NULL UNIQUE,
            not_after TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked')),
            revoked_at TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            custody TEXT NOT NULL CHECK (custody IN ('hardware', 'file'))
        );
        CREATE TABLE credential_bindings (
            id INTEGER PRIMARY KEY,
            credential_sha256 TEXT NOT NULL UNIQUE,
            machine TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, parent_id, created_at, refs) VALUES "
        "('task_a', 'vivy', 'Task A', 'task', 'claimed', 'agent', 'agent', "
        "NULL, ?, '{}')", (_ts(days=1),))
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        OTHER_SCOPE: {"name": "constance-agent", "products": ["constance"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    c = TestClient(libapp.app)
    c.pki = (ca_cert, ca_key)
    c.tmp = tmp_path
    return c


def _machine_cert(c, machine):
    return tls_ca.make_cert(c.tmp, machine, *c.pki, uri_names=(_uri(machine),))


@pytest.fixture
def client(base_client):
    """A client on the mutual-TLS listener as MACHINE, with AGENT and
    OTHER_SCOPE both bound to it."""
    cert, _ = _machine_cert(base_client, MACHINE)
    _register(store.DB_PATH, MACHINE, _der(cert))
    _bind(store.DB_PATH, AGENT, MACHINE)
    _bind(store.DB_PATH, OTHER_SCOPE, MACHINE)
    return TestClient(_OverMTLS(libapp.app, _der(cert), _uri(MACHINE)))


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}", protocol.HEADER: str(protocol.CURRENT)}


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _mint_slip(*, now=None, item_id="task_a", product="vivy"):
    conn = _db()
    slip_id = call_slips.mint(conn, item_id=item_id, product=product,
                              claim_owner_label="checkout-owner", now=now)
    conn.commit()
    conn.close()
    return slip_id


def _redeem(client, slip_id, token=AGENT):
    return client.post("/tokens/redeem", json={"slip_id": slip_id}, headers=_h(token))


def _cards():
    return _db().execute("SELECT * FROM library_cards").fetchall()


def _slip_row(slip_id):
    return _db().execute("SELECT * FROM call_slips WHERE id = ?", (slip_id,)).fetchone()


# --- happy path, underlying every mechanic test below ---------------------


def test_redeeming_a_slip_issues_a_card_and_consumes_it(client):
    slip_id = _mint_slip()
    r = _redeem(client, slip_id)
    assert r.status_code == 200
    body = r.json()
    assert body["card_id"].startswith("card_")
    assert body["actor"] == "vivy-agent"
    assert body["product"] == "vivy"
    assert body["protocol_version"] == protocol.CURRENT
    assert body["state"] == "active"
    assert _slip_row(slip_id)["redeemed_at"] is not None
    assert len(_cards()) == 1


# --- mechanic 1: single-use, enforced against a race -----------------------


def test_a_second_redemption_of_the_same_slip_fails_and_the_first_stands(client):
    """Catches a mutation that drops `redeemed_at IS NULL` from `redeem`'s
    `UPDATE` (or replaces it with a read-then-write) — either would let a
    second call to an already-consumed slip succeed and mint a second card.
    """
    slip_id = _mint_slip()
    first = _redeem(client, slip_id)
    assert first.status_code == 200
    first_card_id = first.json()["card_id"]

    second = _redeem(client, slip_id)
    assert second.status_code == 409
    assert "already" in second.json()["error"]

    # The first redemption's card is untouched by the second's refusal.
    cards = _cards()
    assert len(cards) == 1
    assert cards[0]["id"] == first_card_id
    assert cards[0]["state"] == "active"


# --- mechanic 1, continued: the race itself ---------------------------------


def test_concurrent_redemptions_of_one_slip_produce_exactly_one_card(client):
    """Constructed with real threads against a real on-disk database (the
    `client` fixture's db is a file, not `:memory:`), matching
    `test_reference_checkout.py`'s own race test. Catches a mutation that
    turns `call_slips.redeem`'s conditional `UPDATE` into a read-then-write:
    single-threaded, that mutation is invisible — both `test_...fails`
    above and this test would still pass one call at a time — and only
    shows up under real concurrency, which is why this test exists
    alongside the sequential one rather than instead of it.
    """
    slip_id = _mint_slip()
    results = []

    def go():
        r = _redeem(client, slip_id)
        results.append(r.status_code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200] + [409] * 7, (
        "every loser must get a clean 409, never a 500, and exactly one winner")
    assert len(_cards()) == 1, "exactly one card row must have been inserted"


# --- mechanic 2: the slip's TTL --------------------------------------------


def test_an_expired_unredeemed_slip_is_refused(client):
    """Catches a mutation that skips or inverts the `call_slips.is_redeemable`
    check before consuming a slip — without it, an expired-but-never-used
    slip would still redeem successfully, which is exactly the leaked-
    credential window the TTL exists to bound.
    """
    stale = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
             - dt.timedelta(seconds=call_slips.TTL_SECONDS + 60))
    slip_id = _mint_slip(now=stale)

    r = _redeem(client, slip_id)
    assert r.status_code == 409
    assert "expired" in r.json()["error"]
    assert _slip_row(slip_id)["redeemed_at"] is None, (
        "an expired slip must be refused, not silently marked redeemed")
    assert _cards() == []


# --- mechanic 3: the transport actor cross-check ---------------------------


def _assert_refused_and_nothing_written(slip_id):
    assert _slip_row(slip_id)["redeemed_at"] is None
    assert _cards() == []


def test_the_plain_listener_refuses_redemption(base_client):
    """No peer certificate is stamped, so no machine is authenticated.
    Catches a mutation that lets an absent `mtls.EXTENSION_KEY` fall through
    to the slip lookup."""
    slip_id = _mint_slip()
    r = _redeem(base_client, slip_id)
    assert r.status_code == 403
    assert "mutual-TLS" in r.json()["error"]
    _assert_refused_and_nothing_written(slip_id)


def test_a_credential_bound_to_another_machine_is_refused(base_client):
    """The attack the epic names: AGENT is bound to MACHINE and is presented
    from OTHER_MACHINE, whose certificate is valid and registered. The
    refusal names neither machine."""
    cert, _ = _machine_cert(base_client, OTHER_MACHINE)
    _register(store.DB_PATH, OTHER_MACHINE, _der(cert))
    _bind(store.DB_PATH, AGENT, MACHINE)
    slip_id = _mint_slip()
    c = TestClient(_OverMTLS(libapp.app, _der(cert), _uri(OTHER_MACHINE)))

    r = _redeem(c, slip_id)
    assert r.status_code == 401
    text = r.text
    assert MACHINE not in text and OTHER_MACHINE not in text
    assert "vivy-agent" not in text
    _assert_refused_and_nothing_written(slip_id)


def test_an_unbound_credential_is_refused(base_client):
    cert, _ = _machine_cert(base_client, MACHINE)
    _register(store.DB_PATH, MACHINE, _der(cert))
    slip_id = _mint_slip()
    c = TestClient(_OverMTLS(libapp.app, _der(cert), _uri(MACHINE)))

    assert _redeem(c, slip_id).status_code == 401
    _assert_refused_and_nothing_written(slip_id)


def test_a_revoked_certificate_is_refused(base_client):
    cert, _ = _machine_cert(base_client, MACHINE)
    _register(store.DB_PATH, MACHINE, _der(cert), status="revoked")
    _bind(store.DB_PATH, AGENT, MACHINE)
    slip_id = _mint_slip()
    c = TestClient(_OverMTLS(libapp.app, _der(cert), _uri(MACHINE)))

    assert _redeem(c, slip_id).status_code == 401
    _assert_refused_and_nothing_written(slip_id)


def test_an_unregistered_certificate_is_refused(base_client):
    cert, _ = _machine_cert(base_client, MACHINE)
    _bind(store.DB_PATH, AGENT, MACHINE)
    slip_id = _mint_slip()
    c = TestClient(_OverMTLS(libapp.app, _der(cert), _uri(MACHINE)))

    assert _redeem(c, slip_id).status_code == 401
    _assert_refused_and_nothing_written(slip_id)


def test_the_check_holds_without_the_middleware(base_client):
    """`redemption_binding_holds` reads the certificate itself rather than
    `scope["state"]["machine_identity"]`, so a scope no gate has touched still
    refuses a wrong machine and admits the right one."""
    cert, _ = _machine_cert(base_client, MACHINE)
    _register(store.DB_PATH, MACHINE, _der(cert))
    _bind(store.DB_PATH, AGENT, MACHINE)
    info = {"peer_cert": {"subjectAltName": (("URI", _uri(MACHINE)),)},
            "peer_cert_der": _der(cert)}
    headers = [(b"authorization", f"Bearer {AGENT}".encode())]

    def scope(**kw):
        return {"extensions": {mtls.EXTENSION_KEY: info}, "headers": headers,
                **kw}

    assert credential_binding.redemption_binding_holds(scope())
    assert not credential_binding.redemption_binding_holds(
        {"extensions": {}, "headers": headers})
    assert not credential_binding.redemption_binding_holds(
        scope(headers=[]))
    assert not credential_binding.redemption_binding_holds(
        scope(headers=[(b"authorization", b"Bearer someone-else")]))
    assert not credential_binding.redemption_binding_holds(
        {"extensions": {mtls.EXTENSION_KEY: {**info, "peer_cert_der": b""}},
         "headers": headers})


def test_the_route_runs_the_check_itself_with_no_middleware(base_client):
    """Calls `redeem_token` directly, so neither `MachineIdentityGate` nor
    `CredentialBindingGate` runs. Catches a mutation that drops the route's
    own call to `redemption_binding_holds`, which every test that goes
    through the middleware chain would survive because the gates refuse the
    same requests first."""
    import asyncio

    from starlette.requests import Request

    cert, _ = _machine_cert(base_client, OTHER_MACHINE)
    _register(store.DB_PATH, OTHER_MACHINE, _der(cert))
    _bind(store.DB_PATH, AGENT, MACHINE)  # bound elsewhere
    slip_id = _mint_slip()
    body = json.dumps({"slip_id": slip_id}).encode()

    def call(extensions):
        scope = {
            "type": "http", "method": "POST", "path": "/tokens/redeem",
            "query_string": b"", "extensions": extensions,
            "headers": [(b"authorization", f"Bearer {AGENT}".encode()),
                        (b"content-type", b"application/json")],
            "state": {"protocol_version": protocol.CURRENT},
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return asyncio.run(libapp.redeem_token(Request(scope, receive)))

    wrong_machine = call({mtls.EXTENSION_KEY: {
        "peer_cert": {"subjectAltName": (("URI", _uri(OTHER_MACHINE)),)},
        "peer_cert_der": _der(cert)}})
    assert wrong_machine.status_code == 401
    assert call({}).status_code == 403
    _assert_refused_and_nothing_written(slip_id)


def _post_redeem(port, ctx, slip_id, token=AGENT):
    conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=5,
                                       context=ctx)
    conn.request("POST", "/tokens/redeem",
                 body=json.dumps({"slip_id": slip_id}),
                 headers={"Authorization": f"Bearer {token}",
                          protocol.HEADER: str(protocol.CURRENT),
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def _live_listener(c):
    ca_cert, ca_key = c.pki
    server_cert, server_key = tls_ca.make_cert(
        c.tmp, "server", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1",), ext_key_usage="serverAuth")
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(server_cert), str(server_key))
    sctx.load_verify_locations(str(ca_cert))
    sctx.verify_mode = ssl.CERT_REQUIRED
    sctx.minimum_version = ssl.TLSVersion.TLSv1_3
    server = RunningServer(tls_config(libapp.app, sctx))
    return server, server.start()


def _client_ctx(c, cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(str(c.pki[0]))
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def test_over_a_real_handshake_the_bound_machine_redeems_and_another_is_refused(
        base_client):
    """The whole path with no stamped scope: a real listener, real client
    certificates, one slip. The wrong machine is refused first and leaves the
    slip untouched; the bound machine then redeems it."""
    good, good_key = _machine_cert(base_client, MACHINE)
    other, other_key = _machine_cert(base_client, OTHER_MACHINE)
    _register(store.DB_PATH, MACHINE, _der(good))
    _register(store.DB_PATH, OTHER_MACHINE, _der(other))
    _bind(store.DB_PATH, AGENT, MACHINE)
    slip_id = _mint_slip()
    server, port = _live_listener(base_client)
    try:
        status, body = _post_redeem(
            port, _client_ctx(base_client, other, other_key), slip_id)
        assert status == 401, body
        _assert_refused_and_nothing_written(slip_id)

        status, body = _post_redeem(
            port, _client_ctx(base_client, good, good_key), slip_id)
        assert status == 200, body
        assert json.loads(body)["actor"] == "vivy-agent"
    finally:
        server.stop()
    assert len(_cards()) == 1


def test_over_a_real_handshake_a_revoked_certificate_is_refused(base_client):
    cert, key = _machine_cert(base_client, MACHINE)
    _register(store.DB_PATH, MACHINE, _der(cert), status="revoked")
    _bind(store.DB_PATH, AGENT, MACHINE)
    slip_id = _mint_slip()
    server, port = _live_listener(base_client)
    try:
        status, _ = _post_redeem(
            port, _client_ctx(base_client, cert, key), slip_id)
    finally:
        server.stop()
    assert status == 401
    _assert_refused_and_nothing_written(slip_id)


# --- mechanic 4: the capability gate ----------------------------------------


def test_missing_token_redeem_capability_is_refused(client, monkeypatch):
    """Every fixture token otherwise resolves to a role that already holds
    `token.redeem` (identity.py grants it to both `agent` and `system`, and
    `customer_reader`/`approved_client` inherit it from there), so there is
    no token file entry that reaches this branch on its own. Strip the
    capability from `agent` for this test only — the same shape
    `test_customer_reader_role.py` uses to isolate one capability — to pin
    that `redeem_token`'s `identity.authorized(ident, "token.redeem")` check
    actually gates this route rather than being dead code.
    """
    monkeypatch.setitem(identity.ROLE_CAPS, "agent",
                         identity.ROLE_CAPS["agent"] - {"token.redeem"})
    slip_id = _mint_slip()

    r = _redeem(client, slip_id)
    assert r.status_code == 403
    assert "token.redeem" in r.json()["error"]
    assert _cards() == []
