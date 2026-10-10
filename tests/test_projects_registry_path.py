"""projects.py's module-level `REGISTRY_PATH`: PROJECTS_JSON, then the
deprecated AGENT_PLATFORM_PROJECTS alias, then registry_root.root() — the
order the steward fixed on 2026-10-02 so the Library and agent-platform read
the same registry file on every install, not only one rooted at
$HOME/Convivy.

`REGISTRY_PATH` is computed once, at import, so each test here loads its own
fresh copy of the module rather than monkeypatching the shared one — the
shared `projects` module other test files import is never touched.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import pwd
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_counter = itertools.count()


def _load_registry_root():
    """A fresh `registry_root` module instance, re-executed from source so its
    module-level FALLBACK and CONFIG (computed at import time, matching
    `builder_root.py`) are recomputed against whatever passwd the caller has
    already patched, rather than reused from whichever process-wide import
    happened first."""
    name = f"registry_root_under_test_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "registry_root.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_projects(monkeypatch):
    """A fresh `projects` module instance, re-executed from source so its
    module-level REGISTRY_PATH is recomputed against whatever env/passwd the
    caller has already patched.

    `registry_root` is installed fresh into `sys.modules` first — via
    `monkeypatch` so the real one is restored after the test — because
    `projects.py`'s plain `import registry_root` would otherwise hand back
    whatever copy a prior import in this process already cached, FALLBACK
    and CONFIG baked in against that copy's own, possibly real, passwd home.
    """
    monkeypatch.setitem(sys.modules, "registry_root", _load_registry_root())
    name = f"projects_under_test_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "projects.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home" / "someone"
    h.mkdir(parents=True)
    return h


@pytest.fixture
def passwd_home(monkeypatch, home):
    """Every test here runs with the passwd-reported home pointed at a fresh
    tmp_path, so a real machine's ~/.config/convivy/session-agent.env can never
    leak into a test registry_root.root() falls through to."""
    monkeypatch.setattr(pwd, "getpwuid",
                         lambda uid: types.SimpleNamespace(pw_dir=str(home)))
    return home


def _write_registry(path, product):
    path.write_text(json.dumps({product: {"aliases": [], "repos": []}}))


def test_projects_json_outside_home_convivy_is_what_resolve_reads(
        passwd_home, tmp_path, monkeypatch):
    registry = tmp_path / "elsewhere" / "registry.json"
    registry.parent.mkdir()
    _write_registry(registry, "zzyzx")
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    monkeypatch.delenv("AGENT_PLATFORM_PROJECTS", raising=False)
    mod = _load_projects(monkeypatch)
    assert mod.REGISTRY_PATH == registry
    assert mod.resolve("zzyzx") == "zzyzx"
    assert mod.resolve("some-other-product-not-in-this-file") is None


def test_projects_json_wins_over_the_deprecated_alias(passwd_home, tmp_path, monkeypatch):
    winner = tmp_path / "winner.json"
    loser = tmp_path / "loser.json"
    _write_registry(winner, "winner-product")
    _write_registry(loser, "loser-product")
    monkeypatch.setenv("PROJECTS_JSON", str(winner))
    monkeypatch.setenv("AGENT_PLATFORM_PROJECTS", str(loser))
    mod = _load_projects(monkeypatch)
    assert mod.REGISTRY_PATH == winner
    assert mod.resolve("winner-product") == "winner-product"
    assert mod.resolve("loser-product") is None


def test_the_deprecated_alias_is_used_when_projects_json_is_unset(
        passwd_home, tmp_path, monkeypatch):
    registry = tmp_path / "legacy.json"
    _write_registry(registry, "legacy-product")
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    monkeypatch.setenv("AGENT_PLATFORM_PROJECTS", str(registry))
    mod = _load_projects(monkeypatch)
    assert mod.REGISTRY_PATH == registry


def test_crews_root_from_the_config_file_is_used_when_neither_is_set(
        passwd_home, tmp_path, monkeypatch):
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    monkeypatch.delenv("AGENT_PLATFORM_PROJECTS", raising=False)
    crews_root = tmp_path / "crews-root"
    crews_root.mkdir()
    config = passwd_home / ".config" / "convivy"
    config.mkdir(parents=True)
    (config / "session-agent.env").write_text(f"CREWS_ROOT={crews_root}\n")
    mod = _load_projects(monkeypatch)
    assert mod.REGISTRY_PATH == crews_root / "agent-platform" / "config" / "projects.json"


@pytest.mark.parametrize("config_text", [
    None,  # no config file at all
    "",
    "OTHER=/x\n",
    "CREWS_ROOT=\n",
    "CREWS_ROOT=relative/path\n",
    "CREWS_ROOT=/\n",  # the filesystem root: too wide to act as a root
])
def test_each_fail_safe_case_falls_back_to_home_builder(
        passwd_home, tmp_path, monkeypatch, config_text):
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    monkeypatch.delenv("AGENT_PLATFORM_PROJECTS", raising=False)
    if config_text is not None:
        config_dir = passwd_home / ".config" / "convivy"
        config_dir.mkdir(parents=True)
        (config_dir / "session-agent.env").write_text(config_text)
    mod = _load_projects(monkeypatch)
    assert mod.REGISTRY_PATH == \
        passwd_home / "builder" / "agent-platform" / "config" / "projects.json"


def test_home_env_does_not_move_the_result(passwd_home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "spoofed-home"))
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    monkeypatch.delenv("AGENT_PLATFORM_PROJECTS", raising=False)
    crews_root = tmp_path / "crews-root"
    crews_root.mkdir()
    config_dir = passwd_home / ".config" / "convivy"
    config_dir.mkdir(parents=True)
    (config_dir / "session-agent.env").write_text(f"CREWS_ROOT={crews_root}\n")
    mod = _load_projects(monkeypatch)
    assert mod.REGISTRY_PATH == crews_root / "agent-platform" / "config" / "projects.json"
