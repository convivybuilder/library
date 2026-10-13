"""The schema and registry a hand-built test database needs once it reaches
`crew_registry.roster_add`.

`roster_add` withholds a black-box crew's agent names from the roster.
That reads `black_box_registrations` and `tool_policy`, which a migrated
vault always has and a hand-written fixture schema does not, and it reads
the project registry, which fails closed when unreadable. CI runners have
no real registry, so a fixture that left `projects.REGISTRY_PATH` alone
would pass on the operator's machine and refuse every roster write in CI. Run a file with `PROJECTS_JSON=/nonexistent` to see
what CI sees.
"""
from __future__ import annotations

import json

import projects

# Matches the two tables' migrations, minus the constraints these tests
# never reach.
DDL = """
    CREATE TABLE black_box_registrations (
        id INTEGER PRIMARY KEY,
        crew TEXT NOT NULL UNIQUE,
        model TEXT NOT NULL,
        gateway TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL
    );
    CREATE TABLE tool_policy (
        id INTEGER PRIMARY KEY,
        agent TEXT NOT NULL,
        crew TEXT NOT NULL,
        model TEXT NOT NULL,
        target TEXT NOT NULL,
        decision TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (agent, crew, model, target)
    );
"""

# One harmless entry: `projects.private_repos()` reads an empty dict as an
# unreadable registry.
ORDINARY_REGISTRY = {"_meta": {}, "vivy": {"repos": []}}


def use_registry(tmp_path, monkeypatch, entries=None):
    """Point `projects` at a synthetic registry for the rest of the test.
    `projects._snapshot` re-reads whenever the registry file's stat
    signature moves, so the next test's own path replaces this one without
    a teardown reload."""
    path = tmp_path / "projects-fixture.json"
    path.write_text(json.dumps(ORDINARY_REGISTRY if entries is None else entries))
    monkeypatch.setattr(projects, "REGISTRY_PATH", path)
    projects.reload()
