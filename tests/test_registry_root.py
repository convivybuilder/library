"""registry_root.py, the Library's own copy of agent-platform's
builder_root.root() (see that module and this one's docstring for why there
are two). Mirrors tests/test_builder_root.py in agent-platform; a change to
either resolver's behaviour should be made, and tested, in both.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import registry_root as RR

ROOT = Path(__file__).resolve().parent.parent


def test_registry_root_imports_only_the_standard_library():
    """The one invariant this module exists for: the Library must not depend on
    agent-platform's code at runtime (registry_root.py's own docstring). Mirrors
    agent-platform's tests/test_builder_root.py::test_builder_root_imports_only_the_standard_library."""
    import ast
    import sys
    tree = ast.parse((ROOT / "registry_root.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names - set(sys.stdlib_module_names) == set(), names


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home" / "someone"
    h.mkdir(parents=True)
    return h


def _config(tmp_path, text):
    c = tmp_path / "session-agent.env"
    c.write_text(text)
    return c


def test_a_pinned_crews_root_resolves_to_exactly_that_root(tmp_path, home):
    target = tmp_path / "Convivy"
    config = _config(tmp_path, f"CREWS_ROOT={target}\n")
    assert RR.configured_raw(config) == str(target)
    assert RR.root(config, RR.fallback_for(home), home) == target


def test_no_config_means_the_fallback(tmp_path, home):
    assert RR.root(tmp_path / "absent.env", RR.fallback_for(home), home) == home / "builder"


@pytest.mark.parametrize("text", ["", "OTHER=/x\n", "# CREWS_ROOT=/x\n", "CREWS_ROOT=\n",
                                  "CREWS_ROOT=relative/path\n"])
def test_an_unusable_value_falls_back(tmp_path, home, text):
    assert RR.root(_config(tmp_path, text), RR.fallback_for(home), home) == home / "builder"


def test_quotes_are_tolerated(tmp_path, home):
    target = tmp_path / "builder"
    config = _config(tmp_path, f'CREWS_ROOT="{target}"\n')
    assert RR.root(config, RR.fallback_for(home), home) == target


def test_a_plain_file_is_not_a_root(tmp_path, home):
    f = tmp_path / "a-file"
    f.write_text("x")
    assert RR.root(_config(tmp_path, f"CREWS_ROOT={f}\n"), RR.fallback_for(home), home) \
        == home / "builder"


@pytest.mark.parametrize("which", ["filesystem-root", "home", "home-parent", "tilde"])
def test_a_root_too_wide_to_contain_anything_falls_back(tmp_path, home, which):
    value = {"filesystem-root": "/", "home": str(home), "home-parent": str(home.parent),
             "tilde": "~"}[which]
    assert RR.root(_config(tmp_path, f"CREWS_ROOT={value}\n"), RR.fallback_for(home), home) \
        == home / "builder"


def test_tilde_expands_against_the_passwd_home_not_home_env(tmp_path, home, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "spoofed"))
    assert RR.expanded("~/builder", home) == home / "builder"


def test_the_account_home_comes_from_passwd(tmp_path, monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_dir=str(tmp_path)))
    monkeypatch.setenv("HOME", "/somewhere/else")
    assert RR.real_account_home() == tmp_path


def test_a_usable_crews_root_is_accepted_without_consulting_home_env(tmp_path, home, monkeypatch):
    """$HOME set to something else must not move a resolved, usable root — the
    config and passwd home are the only inputs root() may read."""
    monkeypatch.setenv("HOME", str(tmp_path / "spoofed"))
    target = tmp_path / "crews-root"
    target.mkdir()
    config = _config(tmp_path, f"CREWS_ROOT={target}\n")
    assert RR.root(config, RR.fallback_for(home), home) == target
