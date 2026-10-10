"""The shape of a refusal, and the status codes that carry it.

Contract §8. **Every refusal names what failed**, and the ones a caller can act
on also name what to do about it. That is not politeness: the platform's own
lease refusal is the worked example — "epic E is held by co-vivy-0912 until
05:30, covering these 4 items" lets an orchestrator route to other work, where a
bare "no" leaves it with block or retry and both waste the run.

The status codes are fixed by the contract so a client can branch on them without
reading prose:

| 400 | malformed request, or a protocol version this Library cannot serve |
| 401 | no resolvable identity |
| 403 | the identity resolved and lacks the capability |
| 404 | no such item, or none this identity may see |
| 409 | a live lease fences the subtree, or a precondition failed |
| 413 | the request body is larger than the Library will read |
| 422 | the request parsed and a validation refused it |

**413 AND 400 BOTH ANSWER "TOO LARGE", AND THE DIFFERENCE IS WHETHER ANYTHING
READ IT.** 413 comes from `RequestBodySizeLimit`, outside every other layer, and
means the request was refused before it was parsed — the caller is told the
envelope limit and nothing about its fields, because nothing looked. A 400 naming
a field, such as `content is larger than 20000000 bytes`, means the body parsed
and one value inside it was over. A caller seeing 413 has no field to fix and
should send less; a caller seeing 400 has exactly one.
"""
from __future__ import annotations

from fastapi.responses import JSONResponse


def refuse(status: int, error: str, *, hint: str | None = None, **fields) -> JSONResponse:
    """A refusal body: `error`, an optional `hint`, and whatever typed fields the
    specific refusal needs. `**fields` is how a lease conflict carries the whole
    lease without every refusal growing a lease-shaped hole."""
    body = {"error": error}
    if hint:
        body["hint"] = hint
    body.update(fields)
    return JSONResponse(body, status_code=status)


def unauthorized() -> JSONResponse:
    return refuse(401, "no resolvable identity",
                  hint="present a bearer token the Library knows")


def forbidden(capability: str, scope: str | None = None) -> JSONResponse:
    """403 NAMES THE CAPABILITY, and deliberately not the resource.

    Saying which capability is missing tells the caller's operator what to grant.
    Saying anything about the resource would confirm it exists to an identity
    that may not see it, which is what `not_found` is for."""
    where = f" on {scope}" if scope else ""
    return refuse(403, f"this identity lacks {capability!r}{where}")


def not_found() -> JSONResponse:
    """404 COVERS BOTH "no such item" AND "not yours to see", in one wording.

    Splitting them would turn the pair into an existence oracle: an out-of-scope
    caller could tell a real id from a made-up one by which refusal came back.
    Orient's read paths take the same line."""
    return refuse(404, "no such item, or none this identity may see")
