"""The library card — issued once per agent by `POST /tokens/redeem` (ADR-0062).

issue_01M12NE13P3FEY3HF7732QX03F, Phase 5. `call_slips.py` is the credential
checkout mints; this is what redemption exchanges it for. A call slip is
single-use and short-lived; a library card is neither — it is good for the
agent's lifetime or a refresh window, and it authorizes the Library's own
resources and nothing else.

**`actor` binds the card to the identity that redeemed the slip —
`identity.resolve`'s `ident.actor` on the redemption request — and
deliberately not `call_slips.claim_owner_label`.** `call_slips.py`'s module
docstring and issue_01M12NE13P3FEY3HF7732QX03F's comments 1699 and 1700 both
name `claim_owner_label` as a caller-supplied coordination label, populated
straight from a request body field any product-scoped caller can set, and
unfit for anything an authorization decision reads. That reasoning is about
ADR-0062's actor cross-check at redemption
(`credential_binding.redemption_machine`) — but the same caller-
supplied-label problem applies to what this module records as the card's own
identity, not only to that comparison, so `ident.actor` is what `issue`
takes rather than anything off the slip or the request body.

**This binds "the identity that redeemed", which is the Individuator's own
credential, not yet a distinct per-agent identity.** ADR-0062: "the
Individuator holds it and makes every Library call on the agent's behalf."
Until a per-agent credential exists on this path, the value recorded here is
the Individuator's — the closest authenticated fact available today, and a
placeholder until that per-agent credential exists.

**`state` moves exactly once, through `revoke`, below.** `POST
/tokens/revoke` (issue_01M12NE155ABV0K2K50P6CBWKJ) calls it; this module
still issues every card `active`.

**`issue` mints the card's own secret and stores only its hash
(library/specs/library-card-credential-and-revocation-plan §1).** The
`card_id` this module has always returned is a wire-visible identifier —
served in `card_dict`, named in revoke requests, signals and logs — so it
cannot double as the bearer value. The secret `issue` generates now is
returned once, by the caller, to the redeemer alone; this module never
returns it again and the table never stores it, only
`credential_binding.sha256_token`'s digest.
"""
from __future__ import annotations

import datetime as _dt
import json
import secrets

import call_slips
import credential_binding
from store import new_ulid

STATE_ACTIVE = "active"
STATE_REVOKED = "revoked"

# library/specs/library-card-credential-and-revocation-plan §4's table.
# `CAUSE_HARD_CAP` is written by `store.record_spend` alone — `revoke`
# stores whatever cause its caller passes, and `POST /tokens/revoke` always
# passes `CAUSE_REQUESTED`, unconditionally, so a caller cannot claim the
# other cause for its own revocation.
CAUSE_REQUESTED = "requested"
CAUSE_HARD_CAP = "hard_cap"


class CardIssueFailed(RuntimeError):
    """Raised by `issue` when the insert itself fails.

    Matches `call_slips.MintFailed`'s reasoning exactly: a caller reaching
    this has the property no other write-path failure does — the transaction
    that also consumed the call slip hasn't committed, so nothing is held
    and nothing needs undoing. `POST /tokens/redeem` translates this into a
    named, retry-safe refusal rather than an unhandled 500, the same shape
    `_checkout` already gives `MintFailed`.
    """


def issue(conn, *, slip, actor: str, protocol_version: int, bound_machine: str,
          now=None) -> tuple[str, str]:
    """Insert a new library card for the just-redeemed `slip` row, bound to
    `actor` and to `bound_machine`, and return `(card_id, secret)`.

    Takes the slip row rather than separate `item_id`/`product` arguments so
    there is exactly one place — `call_slips.redeem`'s return value — that
    can supply them, and a caller cannot issue a card for a slip it never
    actually consumed. The same row is where `crew` and the granted
    `profile` come from: `slip["crew"]` is what `call_slips.mint` recorded
    from the checkout credential, and `profile` is `grant.bundle.profile`
    off the grant the slip was minted with, through `call_slips.grant_of` —
    both are read off the slip rather than taken as separate arguments, for
    the same one-source-of-truth reason.

    **The secret is generated here and returned once.** `card_<ULID>`
    stays the wire-visible `card_id`; the secret is a second, unrelated
    value — `"card." + secrets.token_urlsafe(32)` — and only its SHA-256
    (`credential_binding.sha256_token`) is written to `secret_sha256`. A
    caller that loses the returned secret has lost the only copy; the card
    is unusable and the right recovery is to check the item out again for a
    new slip, not to read this one back.

    `bound_machine` is the caller's job to have already proved, through
    `credential_binding.redemption_machine` on the request that is
    redeeming — this function stores whatever string it is given and does
    not itself check a handshake.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    card_id = f"card_{new_ulid()}"
    secret = "card." + secrets.token_urlsafe(32)
    secret_sha256 = credential_binding.sha256_token(secret)
    crew = slip["crew"]
    grant = call_slips.grant_of(slip)
    profile = (grant or {}).get("bundle", {}).get("profile")
    conn.execute(
        "INSERT INTO library_cards (id, actor, product, item_id, slip_id, "
        "protocol_version, state, issued_at, secret_sha256, bound_machine, "
        "crew, profile) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (card_id, actor, slip["product"], slip["item_id"], slip["id"],
         protocol_version, STATE_ACTIVE, now.isoformat(" "),
         secret_sha256, bound_machine, crew, profile))
    return card_id, secret


def get(conn, card_id: str):
    """The raw row for `card_id`, or `None`."""
    return conn.execute("SELECT * FROM library_cards WHERE id = ?",
                        (card_id,)).fetchone()


def revoke(conn, card_id: str, *, cause: str, by: str,
           detail: dict | None = None, now=None) -> bool:
    """Move `card_id` to `revoked` and return whether this call was the one
    that did it.

    **A CONDITIONAL UPDATE, NOT READ-THEN-WRITE — the shape `call_slips.
    redeem` uses, for the same reason.** Two concurrent revocations (a
    caller and a hard-cap crossing racing each other) must produce exactly
    one winner and leave the first cause standing
    (library/specs/library-card-credential-and-revocation-plan §4). Folding
    the check into the `WHERE` clause means only the `UPDATE` that actually
    flips an active row can affect one; the loser's matches zero rows
    rather than racing to overwrite the winner's `revoked_cause`.

    Returns `True` when this call's `UPDATE` matched the row (`cur.rowcount
    == 1`) and wrote `cause`/`by`/`detail`; `False` when the row was already
    `revoked` — the caller reads the row back either way to learn which
    cause actually stands, matching `store.void_spend_entry`'s
    already-happened idempotence.

    **CALL ONLY INSIDE A `BEGIN IMMEDIATE` THE CALLER ALREADY HOLDS**, the
    same requirement `store.void_spend_entry` and `record_spend` state for
    their own conditional writes — this module opens no transaction of its
    own.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    cur = conn.execute(
        "UPDATE library_cards SET state = ?, revoked_at = ?, "
        "revoked_cause = ?, revoked_by = ?, revocation_detail = ? "
        "WHERE id = ? AND state = ?",
        (STATE_REVOKED, now.isoformat(" "), cause, by,
         json.dumps(detail) if detail is not None else None,
         card_id, STATE_ACTIVE))
    return cur.rowcount == 1


def card_dict(row) -> dict:
    """The wire shape of a library card. `card_id` rather than `id`, matching
    `slip_id`'s own naming on the checkout response — a client already reads
    that field name off the grant `POST /items/{id}/checkout` returns.

    **`crew`, `revoked_at`, `revoked_cause`, `revoked_by` and
    `revocation_detail` are served on every card, active or revoked**
    (library/specs/library-card-credential-and-revocation-plan §4): an
    active card simply carries `None` in the four revocation fields, the
    same "absent is `None`, not an omitted key" shape `grant_of` uses
    elsewhere on this credential.
    """
    detail = row["revocation_detail"]
    return {
        "card_id": row["id"],
        "actor": row["actor"],
        "product": row["product"],
        "item_id": row["item_id"],
        "protocol_version": row["protocol_version"],
        "state": row["state"],
        "issued_at": row["issued_at"],
        "crew": row["crew"],
        "revoked_at": row["revoked_at"],
        "revoked_cause": row["revoked_cause"],
        "revoked_by": row["revoked_by"],
        "revocation_detail": json.loads(detail) if detail is not None else None,
    }
