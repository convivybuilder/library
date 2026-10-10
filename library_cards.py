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
(`credential_binding.redemption_binding_holds`) — but the same caller-
supplied-label problem applies to what this module records as the card's own
identity, not only to that comparison, so `ident.actor` is what `issue`
takes rather than anything off the slip or the request body.

**This binds "the identity that redeemed", which is the Individuator's own
credential, not yet a distinct per-agent identity.** ADR-0062: "the
Individuator holds it and makes every Library call on the agent's behalf."
Until a per-agent credential exists on this path, the value recorded here is
the Individuator's — the closest authenticated fact available today, and a
placeholder for the same reason `_call_slip_grant`'s `model` field is.

**`state` is a column existing runtime code never moves.** `POST
/tokens/revoke` (issue_01M12NE155ABV0K2K50P6CBWKJ) is a separate task; this
module issues cards `active` and gives that task a state to move rather than
a table to alter.
"""
from __future__ import annotations

import datetime as _dt

from store import new_ulid

STATE_ACTIVE = "active"
STATE_REVOKED = "revoked"


class CardIssueFailed(RuntimeError):
    """Raised by `issue` when the insert itself fails.

    Matches `call_slips.MintFailed`'s reasoning exactly: a caller reaching
    this has the property no other write-path failure does — the transaction
    that also consumed the call slip hasn't committed, so nothing is held
    and nothing needs undoing. `POST /tokens/redeem` translates this into a
    named, retry-safe refusal rather than an unhandled 500, the same shape
    `_checkout` already gives `MintFailed`.
    """


def issue(conn, *, slip, actor: str, protocol_version: int, now=None) -> str:
    """Insert a new library card for the just-redeemed `slip` row, bound to
    `actor`, and return its id.

    Takes the slip row rather than separate `item_id`/`product` arguments so
    there is exactly one place — `call_slips.redeem`'s return value — that
    can supply them, and a caller cannot issue a card for a slip it never
    actually consumed.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    card_id = f"card_{new_ulid()}"
    conn.execute(
        "INSERT INTO library_cards (id, actor, product, item_id, slip_id, "
        "protocol_version, state, issued_at) VALUES (?,?,?,?,?,?,?,?)",
        (card_id, actor, slip["product"], slip["item_id"], slip["id"],
         protocol_version, STATE_ACTIVE, now.isoformat(" ")))
    return card_id


def get(conn, card_id: str):
    """The raw row for `card_id`, or `None`."""
    return conn.execute("SELECT * FROM library_cards WHERE id = ?",
                        (card_id,)).fetchone()


def card_dict(row) -> dict:
    """The wire shape of a library card. `card_id` rather than `id`, matching
    `slip_id`'s own naming on the checkout response — a client already reads
    that field name off the grant `POST /items/{id}/checkout` returns."""
    return {
        "card_id": row["id"],
        "actor": row["actor"],
        "product": row["product"],
        "item_id": row["item_id"],
        "protocol_version": row["protocol_version"],
        "state": row["state"],
        "issued_at": row["issued_at"],
    }
