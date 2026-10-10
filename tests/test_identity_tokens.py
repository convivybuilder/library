"""Where `identity.TOKENS` resolves, and what the 0-credentials warning says.

issue_01M3JKQGP1TV1PT9HWK27RKTG8: `identity.TOKENS` used to default to
`~/Convivy/convivy-orient/api-tokens.json`, a path only a Convivy development
machine has — a fresh Linux install loaded no credentials and refused every
agent/system request 401. The Library now owns the location of the
credentials it verifies.
"""
from __future__ import annotations

import identity


def test_the_default_resolves_under_home_when_the_variable_is_unset(
        monkeypatch, tmp_path):
    monkeypatch.delenv("LIBRARY_API_TOKENS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert identity.resolve_tokens() == tmp_path / ".library" / "api-tokens.json"


def test_library_api_tokens_overrides_the_default(monkeypatch, tmp_path):
    override = tmp_path / "somewhere-else" / "api-tokens.json"
    monkeypatch.setenv("LIBRARY_API_TOKENS", str(override))
    assert identity.resolve_tokens() == override


def test_the_zero_credentials_warning_names_the_resolved_path_and_the_override(
        monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "TOKENS", tmp_path / "no-such-api-tokens.json")
    monkeypatch.setattr(identity, "CLIENT_TOKENS",
                        tmp_path / "no-such-client-tokens.json")
    line = identity.describe_credentials()
    assert str(identity.TOKENS) in line
    assert "LIBRARY_API_TOKENS" in line
