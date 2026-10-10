#!/usr/bin/env python3.11
"""cert_expiry_check — warns when the Library's own server certificate, or
any ACTIVE registered machine certificate, is inside
`CERT_EXPIRY_WARNING_DAYS` of expiry (issue_01M3XANTMTAED0P592KVX32D41,
platform/decisions/0066's Handoff: "Expiry warning: 21 days, covering the
server certificate as well as every registered machine").

**Two independent things are checked, not one.** Every ACTIVE row in
`machine_certificates` is the registry's own record of what SHOULD be
trusted; the certificate at `mtls.TLS_CERT` is what the mutual-TLS listener
ACTUALLY serves right now, read from the same path setting that listener
itself resolves (`LIBRARY_TLS_CERT`, via `mtls.py`) — the two can diverge if
the TLS files were ever replaced without a matching registry write, which is
exactly the drift this second check exists to catch. The served certificate
is checked whether or not any registry row names it.

**Writes directly to `LIBRARY_DB` through `store.insert_signal`, the same
shape `retire_decisions.py` uses, rather than posting over HTTP the way
`bin/ci_result_fetch.py` does.** This job runs on the machine that serves the
Library and reads the Library's own TLS directory — there is no remote actor
here for an HTTP round trip to authenticate, only a local process that
already has the database open.

**No alert when the TLS files are absent.** That is a missing mutual-TLS
listener, and `serve.sh`/`serve_dual.py` already report it loudly in
`serve.log` — this job does not duplicate that report as a certificate
warning for a certificate that does not exist.

**One alert per certificate per UTC calendar day, by construction of
`dedup_key`.** A run on a day that already alerted for a given fingerprint
collides on that key and `store.insert_signal` answers the existing row
rather than minting a second one; a run on a later day gets a fresh key and
alerts again — this is the mechanism behind "keep warning on every run until
the certificate is renewed," given a job that runs at least daily
(`launchd/com.convivy.library.cert-expiry-check.plist`).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import ssl
import sys
from pathlib import Path

# `store` and its siblings sit at the repo root — `pytest.ini`'s own
# `pythonpath = . bin` solves the same problem for the test run; a script
# invoked directly (as launchd does) has no pytest.ini to read it from, so
# it adds the root itself, matching `bin/ci_result_fetch.py`'s own pattern.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import machine_identity  # noqa: E402
import machine_registry  # noqa: E402
import mtls  # noqa: E402
import store  # noqa: E402

CERT_EXPIRY_WARNING_DAYS = 21

SOURCE = "cert-expiry-check"
KIND = "ops-alert"
PRODUCT = "library"
STREAM = "cert-expiry"

RUNBOOK_ID = "platform/runbooks/minting-the-library-ca-root"

UNREGISTERED_LABEL = ("the Library's served certificate (no "
                      "machine_certificates row matches this fingerprint)")


def due_for_warning(not_after: _dt.datetime, now: _dt.datetime, *,
                    warning_days: int = CERT_EXPIRY_WARNING_DAYS) -> bool:
    """Whether `not_after` is already past, or inside `warning_days` of
    `now` — platform/decisions/0066's Handoff: "Expiry warning: 21 days"."""
    return not_after - now <= _dt.timedelta(days=warning_days)


def alert_dedup_key(fingerprint_sha256: str, now: _dt.datetime) -> str:
    """See the module docstring's "one alert per certificate per UTC
    calendar day" — `now.date()` is what makes a same-day re-run idempotent
    and a next-day run fresh."""
    return f"cert-expiry:{fingerprint_sha256}:{now.date().isoformat()}"


def alert_body(name: str, fingerprint_sha256: str, not_after: _dt.datetime
              ) -> str:
    return (f"certificate for {name} (SHA-256 {fingerprint_sha256}) expires "
            f"{not_after.isoformat()} — renew it following {RUNBOOK_ID}, "
            f"steps 4 to 11")


def build_alert_row(*, name: str, fingerprint_sha256: str,
                    not_after: _dt.datetime, now: _dt.datetime) -> dict:
    """One `machine_certificates` (or served-certificate) expiry, as a row
    ready for `store.insert_signal` — the same field shape `app.py`'s
    `create_signal` route builds, minus everything that route validates on a
    caller-supplied payload this job never has one of."""
    return {
        "id": f"sig_{store.new_ulid()}",
        "source": SOURCE,
        "kind": KIND,
        "actor": SOURCE,
        "ingested_by": SOURCE,
        "acting_human": None,
        "product": PRODUCT,
        "stream": STREAM,
        "body": store.clip(alert_body(name, fingerprint_sha256, not_after),
                           store.MAX_TEXT),
        "refs": json.dumps({
            "machine": name,
            "fingerprint_sha256": fingerprint_sha256,
            "not_after": not_after.isoformat(),
            "runbook": RUNBOOK_ID,
        }),
        "meta": json.dumps({}),
        "privacy_class": "internal",
        "dedup_key": alert_dedup_key(fingerprint_sha256, now),
        "produced_at": store.parse_timestamp(now.isoformat()),
    }


def _as_utc(value: _dt.datetime) -> _dt.datetime:
    """`machine_certificates.not_after` is stored naive, in this schema's
    universal naive-UTC convention (`machine_registry.issue`'s own
    `isoformat(" ")`) — this stamps that convention explicitly rather than
    comparing a naive and an aware datetime and letting Python raise."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc)


def active_registry_candidates(conn) -> list[dict]:
    """Every ACTIVE row in `machine_certificates`, as `{machine,
    fingerprint_sha256, not_after}`."""
    rows = conn.execute(
        "SELECT machine, fingerprint_sha256, not_after FROM "
        "machine_certificates WHERE status = ?",
        (machine_registry.STATUS_ACTIVE,)).fetchall()
    return [{"machine": row["machine"],
             "fingerprint_sha256": row["fingerprint_sha256"],
             "not_after": _as_utc(_dt.datetime.fromisoformat(row["not_after"]))}
            for row in rows]


def served_certificate_candidate(conn) -> dict | None:
    """The certificate the mutual-TLS listener actually serves, as
    `{machine, fingerprint_sha256, not_after}`, or `None` when the TLS
    material is absent.

    **The same three-file presence check `mtls.build_tls_ssl_context` makes
    before binding the listener at all** — a missing listener is not this
    job's alert to raise (see the module docstring).

    `machine` is resolved by looking the fingerprint up in the registry
    under ANY status, not only `active` — "the served certificate may not
    match any row" at all (issue body), so this reports whichever row does
    name it, revoked or not, and falls back to `UNREGISTERED_LABEL` when
    none does.
    """
    missing = [p for p in (mtls.TLS_CERT, mtls.TLS_KEY, mtls.TLS_CA)
              if not p.is_file()]
    if missing:
        return None
    der = ssl.PEM_cert_to_DER_cert(mtls.TLS_CERT.read_text())
    fingerprint = machine_identity.sha256_der(der)
    not_after = _as_utc(machine_identity.not_after_utc(der))
    row = conn.execute(
        "SELECT machine FROM machine_certificates WHERE "
        "fingerprint_sha256 = ?", (fingerprint,)).fetchone()
    machine = row["machine"] if row is not None else UNREGISTERED_LABEL
    return {"machine": machine, "fingerprint_sha256": fingerprint,
           "not_after": not_after}


def due_candidates(conn, now: _dt.datetime
                   ) -> tuple[list[dict], Exception | None]:
    """Every candidate — active registry rows plus the served certificate,
    if present — whose `not_after` is due for a warning against `now`, and
    the exception `served_certificate_candidate` raised while reading the
    served certificate, if any.

    **A present-but-unreadable or malformed `server.crt` is this job's own
    finding to report, not a reason to skip the registry rows this call
    already read.** The exception is caught here, at the one call site that
    can raise one, rather than letting it propagate out of `run()` before
    the registry candidates below it are ever checked.
    """
    candidates = active_registry_candidates(conn)
    served_error = None
    try:
        served = served_certificate_candidate(conn)
    except (OSError, ValueError, IndexError) as e:
        served_error = e
        served = None
    if served is not None:
        candidates.append(served)
    due = [c for c in candidates if due_for_warning(c["not_after"], now)]
    return due, served_error


def run(*, now: _dt.datetime | None = None) -> int:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    conn = store.connect()
    try:
        due, served_error = due_candidates(conn, now)
    finally:
        conn.close()
    failed = served_error is not None
    if served_error is not None:
        print(f"cert_expiry_check: could not read the served certificate: "
             f"{served_error}", file=sys.stderr)
    # **Every candidate is tried, even after one fails.** A refusal or a
    # disabled-writes error is this job's own finding to report for that one
    # candidate, not a reason to stop checking the rest of `due` — the design
    # is that every OTHER certificate is still checked in the same run.
    for candidate in due:
        row = build_alert_row(
            name=candidate["machine"],
            fingerprint_sha256=candidate["fingerprint_sha256"],
            not_after=candidate["not_after"], now=now)
        try:
            # `lambda _ex: True`: a same-day rerun's `dedup_key` collision is
            # this job's own prior alert for the same certificate, never
            # another caller's row, so the existing row is always safe to
            # return — the same idempotent-writer idiom `app.py`'s own
            # `insert_signal` call for `review_record` signals uses.
            status, out = store.insert_signal(row, lambda _ex: True)
        except store.WritesDisabled as e:
            print(f"cert_expiry_check: alert for "
                 f"{candidate['machine']!r} not recorded: {e}",
                 file=sys.stderr)
            failed = True
            continue
        if status not in (200, 201):
            print(f"cert_expiry_check: alert for "
                 f"{candidate['machine']!r} refused unexpectedly: "
                 f"{status} {out}", file=sys.stderr)
            failed = True
            continue
        verb = "alerted" if status == 201 else "already alerted today for"
        print(f"cert_expiry_check: {verb} {candidate['machine']!r} "
             f"(expires {candidate['not_after'].isoformat()})")
    print(f"cert_expiry_check: {len(due)} certificate(s) due for warning")
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
