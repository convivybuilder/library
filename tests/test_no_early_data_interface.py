"""**TLS 1.3 0-RTT early data is replayable by construction** (RFC 8446 §8)
and would void the replay resistance mutual TLS was chosen for
(platform/decisions/0066, platform/decisions/0065). Nothing in this stack
turns it on today — CPython's `ssl` module exposes no early-data interface
at all, so there is no flag to flip — but a note saying so only fires if
whoever swaps the pinned interpreter or ASGI server reads it. This is the
mechanical check standing beside that note, in `requirements.txt`, run on
every push and every PR (`.github/workflows/ci.yml`), which is every
dependency bump this repo makes.

**Enumeration, not a fixed list of names to check for absence.** A future
interface might not be spelled `early_data` — the measurement this pins
searched every attribute of the live objects a 0-RTT interface would have to
appear on, and so does this.
"""
from __future__ import annotations

import inspect
import ssl

import uvicorn

_EARLY_DATA_MARKERS = ("early", "0rtt", "zerortt", "0_rtt")


def _matching(names) -> list[str]:
    return [name for name in names
            if any(marker in name.lower() for marker in _EARLY_DATA_MARKERS)]


def _suspects(obj) -> list[str]:
    return _matching(dir(obj))


def test_the_ssl_module_exposes_no_early_data_interface():
    assert _suspects(ssl) == []


def test_sslcontext_exposes_no_early_data_interface():
    assert _suspects(ssl.SSLContext) == []
    live = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    assert _suspects(live) == []


def test_sslsocket_exposes_no_early_data_interface():
    assert _suspects(ssl.SSLSocket) == []


def test_sslobject_exposes_no_early_data_interface():
    """The in-memory BIO object the TLS state machine drives — the ASGI-
    server-facing surface, and the one a future uvicorn version would call
    into first if it ever grew 0-RTT support."""
    assert _suspects(ssl.SSLObject) == []


def test_uvicorns_pinned_config_exposes_no_early_data_option():
    """The dependency-swap risk this whole test file exists for: not
    CPython's `ssl` module changing, but uvicorn (or whatever ASGI server
    replaces it) growing a 0-RTT knob on `Config` that this service's own
    listener setup would then have to refuse to pass through.

    Checked against the constructor's own parameter names, not `dir(Config)`
    — `dir()` on a class finds its methods and class attributes, not the
    keyword arguments its `__init__` accepts, which is where a new `uvicorn`
    option would actually appear.
    """
    assert _suspects(uvicorn.Config) == []
    params = inspect.signature(uvicorn.Config.__init__).parameters
    assert _matching(params) == []
