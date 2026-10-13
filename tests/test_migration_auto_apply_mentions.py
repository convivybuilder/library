"""No migration names `LIBRARY_AUTO_APPLY` except to set it.

`bin/install-library` refuses to migrate when a pending revision mentions the
identifier anywhere other than a plain module-level `LIBRARY_AUTO_APPLY = True`
(`_auto_apply_blocks_migration`). It fails closed on purpose, so a mention in
a docstring or a comment stops the deploy as surely as `= False` does. On
2026-10-12 `ad6a0a903c09`'s docstring said it "Sets no `LIBRARY_AUTO_APPLY`",
and the deploy watcher refused library#260 and #261 on the live vault for it.

This catches that in CI instead. An assignment of `True` or `False` is a
choice the installer is meant to honour; any other mention is an accident.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_NAME = re.compile(r"\bLIBRARY_AUTO_APPLY\b")
_ASSIGNMENT = re.compile(
    r"^LIBRARY_AUTO_APPLY(\s*:\s*bool)?\s*=\s*(True|False)\s*$"
)


def _revisions() -> list[Path]:
    return sorted(VERSIONS.glob("*.py"))


def test_the_tree_has_revisions_to_check():
    assert len(_revisions()) > 30


@pytest.mark.parametrize("path", _revisions(), ids=lambda p: p.name)
def test_a_revision_names_auto_apply_only_to_set_it(path):
    stray = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _NAME.search(line) and not _ASSIGNMENT.match(line)
    ]
    assert not stray, (
        f"{path.name} mentions LIBRARY_AUTO_APPLY outside an assignment, "
        f"so bin/install-library will refuse to migrate it: {stray}"
    )


@pytest.mark.parametrize("text, refused", [
    ('"""Sets no `LIBRARY_AUTO_APPLY`."""\n', True),
    ("# LIBRARY_AUTO_APPLY is not set here\n", True),
    ("    LIBRARY_AUTO_APPLY = True\n", True),
    ("LIBRARY_AUTO_APPLY = True\n", False),
    ("LIBRARY_AUTO_APPLY: bool = False\n", False),
    ("revision = 'abc'\n", False),
])
def test_the_check_tells_a_stray_mention_from_an_assignment(tmp_path, text, refused):
    f = tmp_path / "rev.py"
    f.write_text(text)
    stray = [
        line for line in f.read_text().splitlines()
        if _NAME.search(line) and not _ASSIGNMENT.match(line)
    ]
    assert bool(stray) is refused
