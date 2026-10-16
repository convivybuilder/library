"""root-ca's `sign --custody` set must equal the registry's custody set.

`root-ca sign` passes `--custody` through verbatim to the
`enroll_machine.py issue --custody` command it prints, and `enroll_machine.py`
accepts exactly `machine_registry.CUSTODY_VALUES`. library#290 added
`root-file` to the registry and left root-ca refusing it, which stopped the
Individuator's install at its signing step on 2026-10-16. No CI job builds the
Swift package, so this reads the constant from the source.
"""
from __future__ import annotations

import re
from pathlib import Path

import machine_registry

MAIN_SWIFT = (Path(__file__).resolve().parent.parent
              / "root-ca" / "Sources" / "root-ca" / "main.swift")


def _leaf_custody_values() -> set[str]:
    match = re.search(r"^let leafCustodyValues = \[([^\]]*)\]$",
                      MAIN_SWIFT.read_text(), re.MULTILINE)
    assert match, "leafCustodyValues not found in root-ca's main.swift"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_root_ca_sign_accepts_exactly_the_registry_s_custody_values():
    assert _leaf_custody_values() == set(machine_registry.CUSTODY_VALUES)


def test_root_ca_sign_s_usage_line_names_the_same_custody_values():
    usage = re.search(r"--custody ([a-z|-]+) --root-cert", MAIN_SWIFT.read_text())
    assert usage, "sign's usage line not found in root-ca's main.swift"
    assert set(usage.group(1).split("|")) == set(machine_registry.CUSTODY_VALUES)
