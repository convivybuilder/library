"""Protocol version negotiation — the first-build item, built first.

ADR-0021 makes version negotiation a first-build item rather than an enterprise
follow-on, because **a protocol that gains versioning later has already shipped
one unversioned generation it must support forever**. So this module exists
before any endpoint does, and every request passes through it.

The contract is `platform/contracts/library-protocol` §1. Two rules matter here:

**The version travels in a header, and the path carries none.** Two version
channels drift, and then a reader has to know which one wins. A header is also
the channel negotiation can move — at Phase 5 the redeemed token pins a version
for its lifetime and this header must agree with it — and a path cannot be
negotiated, only rewritten.

**Within a version the Library only ADDS.** A new endpoint, a new optional
request field, a new field on a response. Removing a response field, changing a
field's type, tightening what a request must contain, or turning a refusal into a
permission are all version 2. Clients are required to ignore response fields they
do not recognise, which is what makes the additive half free.
"""
from __future__ import annotations

HEADER = "Library-Protocol"

# Every version this build can serve, newest last. A second entry appears here
# only alongside code that can actually serve it — an advertised version nobody
# implemented is worse than an absent one, because a client will select it.
SUPPORTED = (1,)
CURRENT = SUPPORTED[-1]


def negotiate(raw: str | None) -> tuple[int | None, dict | None]:
    """The requested version, or a refusal body naming what this build serves.

    REQUIRED RATHER THAN DEFAULTED, and that is the whole point of building this
    first. Defaulting a missing header to `CURRENT` would let a client ship with
    no version at all, work fine, and then break on the day version 2 arrives —
    which is precisely the unversioned generation ADR-0021 exists to prevent. The
    cost is one header on every request; the alternative is a coordinated upgrade
    across every machine.
    """
    if raw is None or not raw.strip():
        return None, {
            "error": f"every request must carry the {HEADER} header",
            "supported": list(SUPPORTED),
            "hint": f"send {HEADER}: {CURRENT}. It is required rather than "
                    f"defaulted, so that a client cannot ship unversioned and "
                    f"discover it on the day a second version exists.",
        }
    try:
        version = int(raw.strip())
    except ValueError:
        return None, {
            "error": f"{HEADER} must be an integer, got {raw.strip()!r}",
            "supported": list(SUPPORTED),
        }
    if version not in SUPPORTED:
        # NAMES WHAT IT CAN SERVE, which is the behaviour every other hard
        # refusal in the platform has. A bare "unsupported" leaves the client
        # guessing between downgrade and upgrade.
        return None, {
            "error": f"this Library cannot serve protocol version {version}",
            "supported": list(SUPPORTED),
            "hint": ("downgrade to a version in `supported`, or upgrade the "
                     "Library — the two halves are deployed separately and are "
                     "expected to drift (ADR-0021)"),
        }
    return version, None
