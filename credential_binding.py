"""The credential-to-machine binding: platform/decisions/0066 stage C.

The mutual-TLS listener authenticates a machine (`machine_identity.
MachineIdentityGate`) and identifies a caller (`identity.resolve`,
per-route) separately, and neither says the caller's *credential* is one
the operator meant to be usable from that machine. This closes that gap:
`credential_bindings` maps the SHA-256 of a bearer token to the one machine
it may be presented from, and this gate refuses a request whose token
either has no such row (unbound) or names a different machine than the
handshake just proved.

**Runs after `machine_identity.MachineIdentityGate` in the middleware
chain, and depends on it having already resolved `scope["state"]
["machine_identity"]`.** See `app.py`'s registration comment for the
ordering that guarantees this.

**Read fresh on every request, never cached**, the same shape
`machine_identity._registry_status` and `identity._tokens()` already use.

**The raw token never reaches this module's own logic beyond the one hash
call, and never reaches the database at all.** `sha256_token` is computed
once per request, and once by `bind_credential.py` when a binding is
created, and only the digest is ever used past that point —
nothing here logs, stores, or compares the raw string.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import sqlite3

import machine_identity
import mtls
import refusals
import store


def sha256_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bind(credential_sha256: str, machine: str, *,
         note: str | None = None, now=None) -> dict:
    """Bind `credential_sha256` to `machine`, overwriting any existing
    binding for that hash. Returns the stored row.

    **The write path `bind_credential.py` is built on** — this is the one
    place a `credential_bindings` row is ever created or changed, whether
    the caller is that local admin command or, later, an approved-client
    HTTP route (not built in this PR; see its own module docstring).

    **An upsert on `credential_sha256`, not an insert.** The migration's
    `UNIQUE` constraint on that column is what makes rebinding a credential
    already bound to some machine — the lockout recovery path platform/
    decisions/0066 specifies — overwrite the existing row rather than
    collide against it. There is exactly one row per credential at all
    times, matching what `_bound_machine` looks up.

    Goes through `store.connect_rw()`, so this refuses exactly like every
    other write path in the service while the Library is in its read-only
    shadow (`store.WritesDisabled`) — the local admin command is not a way
    around that gate.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat(" ")
    conn = store.connect_rw()
    try:
        conn.execute(
            "INSERT INTO credential_bindings "
            "(credential_sha256, machine, note, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (credential_sha256) DO UPDATE SET "
            "  machine = excluded.machine, "
            "  note = excluded.note, "
            "  created_at = excluded.created_at",
            (credential_sha256, machine, note, now_iso))
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM credential_bindings WHERE credential_sha256 = ?",
            (credential_sha256,)).fetchone()
        return dict(stored)
    finally:
        conn.close()


def _bound_machine(credential_sha256: str) -> str | None:
    """The machine `credential_sha256` is bound to, or `None` if this
    credential has no binding row at all.

    Fails closed on anything that stops this from reading the database — a
    missing file, a misconfigured `LIBRARY_DB`, a locked or corrupt
    database, anything `store.connect` or the query itself raises on. An
    unreadable binding table must refuse every credential it would
    otherwise vouch for, not admit them on the strength of never having
    looked.

    **`sqlite3.Error` is caught around the QUERY as well as the connect,
    and that second half is the one worth stating** (security-reviewer, on
    convivy/library#89). Catching only at `store.connect()` leaves a locked
    or corrupt database raising out of raw ASGI middleware, which Starlette
    turns into a 500 rather than this gate's deliberate 401. It still
    refuses — nothing downstream runs — but by accident rather than by
    decision, and a docstring promising fail-closed while one read path
    fails by exception is the shape this crew keeps finding. The identical
    fix is applied to `machine_identity._registry_status`, which had the
    same gap.

    **`OSError` rather than `FileNotFoundError`, and the reason is a
    version difference in `pathlib`** (security-reviewer, round 2 on
    convivy/library#89). `store.connect()` calls `DB_PATH.is_file()`
    first, and on Python 3.11 `pathlib` swallows only ENOENT, ENOTDIR,
    EBADF and ELOOP there — an EACCES from a directory whose permissions
    tightened, or a degraded network mount, propagates as `PermissionError`,
    which is an `OSError` and neither a `FileNotFoundError` nor a
    `sqlite3.Error`. On 3.14 the same call returns `False` instead.
    **Both CI and the deployed service run 3.11**, so this is the branch
    that is actually taken rather than a divergence between them. The
    Library ran 3.14 in its venv and its deployment until 2026-09-04, when
    Builder was pulled back to one interpreter; `OSError` covers both
    versions and is what the catch is written against.
    """
    try:
        conn = store.connect()
    except (store.MisconfiguredDatabase, OSError, sqlite3.Error):
        return None
    try:
        row = conn.execute(
            "SELECT machine FROM credential_bindings "
            "WHERE credential_sha256 = ?", (credential_sha256,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row else None


def _bearer_token(scope) -> str | None:
    """The raw bearer token off an `Authorization` header in a raw ASGI
    `scope`, or `None` when the header is absent, malformed, or not a
    `Bearer` scheme.

    Reads `scope["headers"]` directly rather than building a `Request` —
    the same style `RequestBodySizeLimit` already uses to read
    `content-length` before anything downstream parses the request, and for
    the same reason: this runs as raw ASGI middleware, ahead of any route's
    own `Request` object.
    """
    for key, value in scope.get("headers", ()):
        if key != b"authorization":
            continue
        try:
            text = value.decode("latin-1")
        except UnicodeDecodeError:
            return None
        if not text.startswith("Bearer "):
            return None
        return text[len("Bearer "):].strip()
    return None


def _refuse() -> refusals.JSONResponse:
    return refusals.refuse(
        401, "this credential is not bound to the presenting machine",
        hint="bind the credential to this machine with the local admin "
             "command, or present it from the machine it is bound to")


class CredentialBindingGate:
    """Refuse a request over the mutual-TLS listener whose bearer
    credential is not bound to the machine the handshake just proved.

    **Never runs on the plain listener**, keyed on `mtls.EXTENSION_KEY`
    exactly like `machine_identity.MachineIdentityGate` — a request over
    127.0.0.1:8100 carries no such key and passes straight through,
    unchanged from how it behaved before this file existed.

    **A request presenting no bearer token at all passes through
    unchanged.** There is no credential here to check a binding against;
    the ordinary per-route `_identify` refusal (401, "no resolvable
    identity") is what answers a missing or unrecognised token, and it
    must keep being the thing that answers it rather than this gate
    inventing a second wording for the same case.

    **Fails closed on everything else**: no binding row for the presented
    token's hash, or a row naming a machine other than the one
    `machine_identity.MachineIdentityGate` resolved, both refuse. So does
    a request that somehow reaches this gate with no machine identity on
    the scope at all — that shape should be impossible given the
    registration order in `app.py`, and refusing rather than assuming
    admits nothing on the strength of a middleware ordering this file
    cannot itself verify at runtime.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if mtls.EXTENSION_KEY not in scope.get("extensions", {}):
            await self.app(scope, receive, send)
            return

        token = _bearer_token(scope)
        if token is None:
            await self.app(scope, receive, send)
            return

        machine = scope.get("state", {}).get("machine_identity")
        if not machine:
            await _refuse()(scope, receive, send)
            return

        if _bound_machine(sha256_token(token)) != machine:
            await _refuse()(scope, receive, send)
            return

        await self.app(scope, receive, send)


def redemption_binding_holds(scope) -> bool:
    """Whether the request in `scope` may redeem: it arrived over the
    mutual-TLS listener, the client certificate resolves to a registered,
    unrevoked machine, and the bearer credential presented is bound to that
    same machine. `POST /tokens/redeem` refuses on `False`, before it reads
    the body or looks up a slip, and this is the only check standing
    between a request and a card.

    **Nothing here reads state another component set.** The machine comes
    from the peer certificate through `machine_identity.resolve_machine`,
    not from `scope["state"]["machine_identity"]`, so the check holds
    whether or not the middleware chain ran, and the credential comes from
    the same `Authorization` header `identity.resolve` reads. Every path
    that does not reach the final comparison returns `False`: a plain
    listener (no `mtls.EXTENSION_KEY`), an unresolvable certificate, no
    bearer token, an unbound credential (`_bound_machine` is `None`, which
    equals no machine name).
    """
    mtls_info = scope.get("extensions", {}).get(mtls.EXTENSION_KEY)
    if mtls_info is None:
        return False
    machine = machine_identity.resolve_machine(mtls_info)
    token = _bearer_token(scope)
    if machine is None or token is None:
        return False
    return _bound_machine(sha256_token(token)) == machine
