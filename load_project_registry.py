#!/usr/bin/env python3.11
"""load_project_registry — the operator command that loads the project
registry file into the Chart House's `project_registry` table (CQ·241,
platform/decisions/0098's 2026-10-25 amendment: "an install has one crew
registry, held by the Library in its Chart House"). Modelled on
`load_crew_registry.py`.

**Run on the machine that serves the Library, against its own database
directly** — the same trusted-local-operator shape `load_crew_registry.py`,
`enroll_machine.py` and `bind_credential.py` all take: no network, no
token, run by the operator who already has a shell on the machine the
Library runs on.

**It loads an empty table, and refuses a populated one unless `--replace`
is passed.** An install runs this once to seed the registry, and once the
table is the registry, a crew can be registered into it that the seed file
does not hold. A second plain run must not erase that crew, so it refuses
and writes nothing. `--replace` makes the table equal the file and prints
the slugs it added and removed. Only the Library's co passes it, to load a
row edit made in the file. See `project_registry.replace` for why a replace
is a full replace, not an upsert.

**`--replace` removes only the slugs `--remove` names** (CQ·247). A crew
registered through `POST /registry/crews` is in the table and not in the
file, so a replace that would remove a row the operator did not name with
`--remove SLUG` refuses with exit 4 and writes nothing, and so does a
`--remove` naming a slug the replace would keep. A replace that only adds
rows or changes fields needs no `--remove`.

**`--replace` removes only the fields `--drop-field` names** (CQ·250). A
field is removed when the stored row holds a non-null value at it and the
file's row for that slug lacks it or sets it null. In a crew's row a field
is a top-level key, so a map-valued field that loses one of its own keys
has changed rather than been removed. In a `_`-prefixed entry a field is
the dotted path of map keys down to a value that is not a map, at any
depth, so `_meta.fields.agent_prefix` is one field and `--drop-field
_meta.fields.agent_prefix` names it; a `_`-prefixed entry whose own value
is not a map, such as the list `_kb_legacy`, has no fields, and losing it
is a slug removal `--remove` governs. The removed set must equal the named
set exactly, by the same rule `--remove` uses, and the refusal prints each
unnamed field as `<slug>.<field>`, never its value. A changed value needs
no flag.

**The exit codes are a contract, and Shipwright's install reads them**
(CQ·241's thread, comments 5907 and 5908):

- 0: loaded into an empty table, or replaced with `--replace`.
- 1: the file is unreadable or invalid, writes are disabled, or another
  error occurred. Nothing is written.
- 2: a usage error. argparse uses this code, so nothing else does.
- 3: the table is already populated and `--replace` was not passed.
  Nothing is written. An install reads it as "already loaded".
- 4: `--replace` would remove a slug `--remove` does not name, or
  `--remove` names one it would not remove; or it would remove a field no
  `--drop-field` names, or `--drop-field` names one it would not remove.
  Nothing is written. Only a `--replace` run can exit 4, and an install's
  run-once step never passes `--replace`.

**Refuses, and changes nothing, on an unreadable or invalid file.** The
document is opened, parsed and validated in full before the first row is
deleted — the same "stop before any write" shape `load_crew_registry.py`'s
own `roster` subcommand takes, so a bad file never leaves the table half-
replaced.

    load_project_registry.py ~/Convivy/install/projects.json \\
        --actor "operator, cutover 2026-10-25"
"""
from __future__ import annotations

import argparse
import json
import sys

import project_registry
import store

# Read by Shipwright's install step; see the module docstring.
EXIT_POPULATED = 3
# Only a `--replace` run returns this; see the module docstring.
EXIT_WOULD_REMOVE = 4


def _load_document(path: str) -> dict:
    """Open, parse and validate `path` as a registry document: the same
    checks `projects.py`'s own read applies (the file must open and parse
    as JSON) plus `project_registry.validate_document`'s shape check.
    Raises `SystemExit` naming the problem, rather than letting a caller
    see a traceback for a file this command cannot use."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError as e:
        raise SystemExit(f"load_project_registry: {path} not found") from e
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(
            f"load_project_registry: cannot read {path} "
            f"({type(e).__name__}: {e})") from e
    try:
        project_registry.validate_document(raw)
    except project_registry.InvalidRegistryDocument as e:
        raise SystemExit(f"load_project_registry: {e}") from e
    return raw


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("registry_json",
                    help="path to the install's registry file")
    p.add_argument("--actor", required=True)
    p.add_argument(
        "--replace", action="store_true",
        help="replace a populated table with the file; without it a "
             "populated table refuses with exit 3")
    p.add_argument(
        "--remove", action="append", default=[], metavar="SLUG",
        help="with --replace, a slug the replace may remove; repeat it for "
             "each. A replace that would remove any slug not named here "
             "refuses with exit 4")
    p.add_argument(
        "--drop-field", action="append", default=[], metavar="SLUG.FIELD",
        help="with --replace, a stored field the replace may remove, "
             "named as SLUG.FIELD; repeat it for each. A replace that "
             "would remove any field not named here refuses with exit 4")
    args = p.parse_args(argv)
    if args.remove and not args.replace:
        p.error("--remove is valid only with --replace")
    if args.drop_field and not args.replace:
        p.error("--drop-field is valid only with --replace")

    document = _load_document(args.registry_json)
    try:
        added, removed = project_registry.replace(
            document, actor=args.actor, allow_populated=args.replace,
            remove=args.remove, drop_fields=args.drop_field)
    except project_registry.RegistryPopulated as e:
        print(f"load_project_registry: {e}", file=sys.stderr)
        return EXIT_POPULATED
    except project_registry.RegistryWouldRemove as e:
        print(f"load_project_registry: {e}", file=sys.stderr)
        return EXIT_WOULD_REMOVE
    except store.WritesDisabled as e:
        print(f"load_project_registry: {e}", file=sys.stderr)
        return 1

    for slug in added:
        print(f"added: {slug}")
    for slug in removed:
        print(f"removed: {slug}")
    print(f"added {len(added)} slug(s), removed {len(removed)} slug(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
