"""A credential that may change crew composition without holding every
capability `system` does.

`orchestrator` is a delta over `agent` (`identity.ROLE_CAPS`), the same
shape `customer_reader` already is, added on the operator's 2026-09-04 ruling that a
crew's roster must be malleable without a pull request while an agent's
definition stays canon. These tests pin the delta itself and that a token
file can name the new kind, matching `test_customer_reader_role.py`'s own
"the capability delta, asserted directly" section — the write behaviour it
gates is `crew_registry`'s, covered in `test_crew_registry.py`.
"""
from __future__ import annotations

import json

import identity


def test_the_role_adds_exactly_one_capability_over_agent():
    """A union over `agent` rather than a copy, so the two cannot drift. If
    this ever reports more than one added capability, something widened the
    role by accident."""
    added = identity.ROLE_CAPS["orchestrator"] - identity.ROLE_CAPS["agent"]
    assert added == {"roster.write"}


def test_an_ordinary_agent_lacks_roster_write():
    assert "roster.write" not in identity.ROLE_CAPS["agent"]
    assert "roster.write" not in identity.ROLE_CAPS["system"]


def test_the_orchestrator_keeps_the_ordinary_capabilities_agent_has():
    """It is an agent that may additionally change crew composition, not a
    separate credential that lost everything else."""
    assert identity.ROLE_CAPS["agent"] <= identity.ROLE_CAPS["orchestrator"]


def test_orchestrator_is_nameable_by_a_token_file(tmp_path, monkeypatch):
    """Presenting a credential, not reading the table: a token file naming
    `kind: orchestrator` resolves to that kind and to `roster.write`."""
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({
        "tok": {"name": "co-library", "products": ["*"],
                "kind": "orchestrator"}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "absent.json")
    ident = identity.resolve("Bearer tok")
    assert ident.kind == "orchestrator"
    assert identity.authorized(ident, "roster.write") is True


def test_an_agent_credential_is_refused_roster_write(tmp_path, monkeypatch):
    """The other half of the same presentation: an ordinary agent token
    resolves fine and is refused the one capability it does not hold."""
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({
        "tok": {"name": "a-worker", "products": ["*"]}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "absent.json")
    ident = identity.resolve("Bearer tok")
    assert ident.kind == "agent"
    assert identity.authorized(ident, "roster.write") is False


def test_the_nameable_set_still_excludes_the_client_role():
    """`orchestrator` joins `system` and `customer_reader` as nameable by an
    ordinary token file; `approved_client` still is not, and still only
    ever comes from the separate client-token file (ADR-0038)."""
    assert "orchestrator" in identity.NAMEABLE_KINDS
    assert "approved_client" not in identity.NAMEABLE_KINDS
    assert identity.NAMEABLE_KINDS <= set(identity.ROLE_CAPS)
