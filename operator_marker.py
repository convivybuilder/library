"""Reads the operator's audience marker from the operator config file.

Shared by `load_crew_registry.py`'s one-time roster load and `app.py`'s two
admission routes, so the path, the precedence and the failure wording are
defined once rather than twice. `$CONVIVY_OPERATOR_CONFIG` if set, else
`$HOME/.config/convivy/operator.json` — the same file and the same
precedence `agent-platform`'s `operator_config.py` reads.

**No default marker.** A guessed one would seed a roster name no document
uses, or compare `binds_agents` against a name that is not actually the
operator's.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class OperatorMarkerUnavailable(Exception):
    """The operator config file, its JSON, or its `operator_marker` key
    could not be read. Carries `path` so a caller can report where it
    looked without re-deriving it."""

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"operator config at {path}: {reason}")


def config_path() -> Path:
    env = os.environ.get("CONVIVY_OPERATOR_CONFIG")
    return Path(env) if env else \
        Path.home() / ".config" / "convivy" / "operator.json"


def read_marker() -> str:
    """The operator's audience marker, read fresh from the config file on
    every call — no caching, so a config edit takes effect on the very next
    read with nothing restarted.

    Raises `OperatorMarkerUnavailable` when the file is not readable, its
    contents are not valid JSON, or it has no usable `operator_marker`
    string key.
    """
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise OperatorMarkerUnavailable(path, f"not readable: {e}")
    except json.JSONDecodeError as e:
        raise OperatorMarkerUnavailable(path, f"not valid JSON: {e}")
    marker = data.get("operator_marker") if isinstance(data, dict) else None
    if not isinstance(marker, str) or not marker.strip():
        raise OperatorMarkerUnavailable(path, "no usable 'operator_marker'")
    return marker
