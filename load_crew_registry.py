#!/usr/bin/env python3.11
"""load_crew_registry — the one-time operator command that loads the
current roster and per-project profiles into `roster_agents` and `profiles`
(issue_01M19QP8S0QPKM0MN7XHYWNGKY).

**Run on the machine that serves the Library, against its own database
directly** — the same shape `enroll_machine.py` and `bind_credential.py`
use: an administrative command with no network and no token, trusted
because it runs as the operator who already has a shell on the machine
the Library runs on.

**`roster` also loads the operator's audience marker**, read from the
operator config file (`_operator_marker`) rather than from
`ALLOWED_AUDIENCE`, and refuses before writing anything when that file
or its `operator_marker` key is missing.

**Reads two sibling checkouts the migration tree itself cannot** — see
`52afccc56e4f_roster_and_profiles.py`'s docstring for why the data load is
this standalone script rather than something `alembic upgrade head` runs:

    load_crew_registry.py roster \\
        ~/Convivy/agent-platform/lib/knowledge/knowledge.py \\
        --actor "operator, cutover 2026-09-06"
    load_crew_registry.py profiles ~/Convivy/knowledge \\
        --actor "operator, cutover 2026-09-06"

**`profiles` is idempotent** — `.profile_put` upserts, so re-running it
against a database that already holds the load only refreshes
`updated_at`/`updated_by`. **`roster` is not, on purpose** — see below.

**`profiles` also reconciles: a row whose file is gone gets deleted.**
`roster` has no equivalent pass — nothing here removes a name dropped from
`ALLOWED_AUDIENCE` — because this issue's drift was profile rows outliving
deleted files, not roster names. `--dry-run` prints what the reconcile
would remove and writes nothing. The Library runs the same reconcile on its
own when a profile file changes (`profile_sync.py`), so this command is for
an operator who wants the load now, not for a profile to take effect.

**Authenticates as `kind: orchestrator` unconditionally**, the same way
`enroll_machine.py` never presents a bearer token at all: this command IS
the trusted local operator path, not a client of one. A future HTTP write
surface authenticates its callers for real; this script is not that
surface.

**The `roster` subcommand now refuses to write against a populated table**
(issue_01M34STNFA2CSD78PW5Y9XHX1M). `PUT`/`DELETE /roster/{name}` exist, so
the Library's own `roster_agents` rows — not `ALLOWED_AUDIENCE` — are the
source of a crew's roster from here on; re-running this command against a
table an HTTP caller has since edited would upsert every name in
`ALLOWED_AUDIENCE` again, silently undoing a revocation an orchestrator
made over the wire. `--reseed` is the explicit override, and it adds every
name in `ALLOWED_AUDIENCE` to the table and removes nothing — a name the
table holds and `ALLOWED_AUDIENCE` does not is left alone, so `--reseed`
cannot undo a name an HTTP caller added, only re-add one an HTTP caller
removed.
"""
from __future__ import annotations

import argparse
import ast
import sys

import crew_profiles
import crew_registry
import identity
import operator_marker
import profile_sync
import store


def _roster_from_knowledge_py(path: str) -> list[str]:
    """Every name in `ALLOWED_AUDIENCE`, read via `ast.literal_eval` rather
    than by importing or exec'ing the file — the source is a sibling
    repository's module, and this process has no business running its
    code, only reading one literal out of it."""
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) \
                        and target.id == "ALLOWED_AUDIENCE":
                    return sorted(ast.literal_eval(node.value))
    raise SystemExit(
        f"load_crew_registry: no ALLOWED_AUDIENCE assignment found in "
        f"{path}")


def _operator_marker() -> str:
    """The operator's audience marker, read as data from the operator config
    via `operator_marker.read_marker` — the module `app.py`'s admission
    routes read the same marker through, so the path and the failure wording
    are defined in one place rather than twice.

    **NOT FROM `ALLOWED_AUDIENCE`.** agent-platform 729be139 put the marker
    into that set as a name read at runtime, which `ast.literal_eval` cannot
    evaluate, so the literal now holds the agent names alone and the marker
    comes from the file the Bosun reads it from. It has to reach the roster
    all the same: knowledge admission refuses any `audience` value that is
    not in `GET /roster`, so a fresh seed without it refuses every document
    addressed to the operator.

    **No default.** A missing file or key exits naming the path, because a
    guessed marker would seed a roster name no document uses."""
    try:
        return operator_marker.read_marker()
    except operator_marker.OperatorMarkerUnavailable as e:
        raise SystemExit(f"load_crew_registry: {e}")


def _profiles_from_knowledge_repo(root: str) -> list[dict]:
    """Every profile under `root`, as `{project, variant, include,
    claude_md_extras}`, for a caller that wants the stop-on-first-error
    reading. `_profiles` below reports every failure instead.

    Exits on the first file that does not load, including a `profile:` key
    that disagrees with its path (`profile_sync` checks that against the
    path, which is the source of truth for `project` and `variant`).
    """
    try:
        rows, failed = profile_sync.load(root)
    except profile_sync.ProfileRootError as e:
        raise SystemExit(f"load_crew_registry: {e}")
    if failed:
        raise SystemExit(f"load_crew_registry: {failed[0]}")
    return rows


def _roster(args) -> int:
    # **REFUSES A POPULATED TABLE UNLESS `--reseed` IS PASSED**
    # (issue_01M34STNFA2CSD78PW5Y9XHX1M). `PUT`/`DELETE /roster/{name}` are
    # the roster's write surface now, so a nonempty `roster_agents` means an
    # orchestrator may already have edited it over HTTP — re-running this
    # command would upsert every name in `ALLOWED_AUDIENCE` again, silently
    # restoring a name a revocation just removed.
    conn = store.connect()
    try:
        existing = crew_profiles.roster(conn)
    finally:
        conn.close()
    if existing and not args.reseed:
        print(
            "load_crew_registry: roster_agents already holds "
            f"{len(existing)} row(s); the Library's roster is now edited "
            "over HTTP (PUT/DELETE /roster/{name}), not by re-running this "
            "command. Pass --reseed to overwrite it from ALLOWED_AUDIENCE "
            "anyway.", file=sys.stderr)
        return 1

    names = sorted(set(_roster_from_knowledge_py(args.knowledge_py))
                   | {_operator_marker()})
    # **EVERY NAME IS CHECKED BEFORE ANY IS WRITTEN** (reviewer, PR #255
    # round 1). `roster_add` commits each name on its own, so an unreadable
    # registry met halfway through would leave a partial roster; here it
    # stops the load before the first write. A withheld name (a black-box or
    # private crew's) is skipped and named, since `roster_add` would refuse
    # it anyway.
    try:
        skipped = {name for name in names if crew_registry.withheld(name)}
    except crew_registry.RegistryUnreadable as e:
        print(f"load_crew_registry: {e}; nothing was loaded", file=sys.stderr)
        return 1
    ident = identity.Identity(actor=args.actor, kind="orchestrator")
    loaded = 0
    for name in names:
        if name in skipped:
            print(f"roster: {name} withheld, a black-box or private crew's name")
            continue
        crew_registry.roster_add(ident, name)
        print(f"roster: {name}")
        loaded += 1
    print(f"loaded {loaded} roster name(s)")
    return 0


def _profiles(args) -> int:
    try:
        rows, failed = profile_sync.load(args.knowledge_root)
    except profile_sync.ProfileRootError as e:
        raise SystemExit(f"load_crew_registry: {e}")

    # **A FILE THAT DOES NOT LOAD STOPS THE WHOLE COMMAND BEFORE ANY WRITE**
    # (security-reviewer, PR #131 round 1). A dry run and a real run check the
    # same things, so a clean dry run is evidence a real run would get as far
    # as writing. The automatic reload in `profile_sync.refresh` makes the
    # other choice, keeping the good files, because a read must not stop on
    # one bad file. An operator asking for the load wants the whole load or
    # none of it.
    if failed:
        for error in failed:
            print(f"load_crew_registry: {error}", file=sys.stderr)
        return 1

    ident = identity.Identity(actor=args.actor, kind="orchestrator")

    conn = store.connect()
    try:
        existing = crew_profiles.profiles(conn)
    finally:
        conn.close()
    gone = profile_sync.stale(existing, rows, failed)

    # **`--dry-run` COVERS THE UPSERTS TOO, NOT ONLY THE DELETES.** A flag that
    # suppressed the removals while still writing every row on disk would be a
    # dry run of half the command, and the half it still performed is the one
    # that rewrites 23 rows' `updated_at`/`updated_by`. Someone reaching for
    # `--dry-run` before a reconcile they are unsure of expects an inspection,
    # and an inspection that mutates is the failure the flag exists to avoid.
    if not args.dry_run:
        profile_sync.apply(ident, rows, gone)
    load_verb = "would load" if args.dry_run else "loaded"
    for row in rows:
        print(f"profile: {row['project']}/{row['variant']}")
    print(f"{load_verb} {len(rows)} profile(s)")

    # **THE REFUSAL IN `profile_sync.load` COVERS AN EMPTY CHECKOUT, NOT A
    # PARTIAL ONE, AND THAT LIMIT IS DELIBERATE** (security-reviewer, PR #131
    # round 2). A sparse or half-synced checkout carrying one project's
    # profiles yields a nonempty `rows`, so every other project's live row is
    # computed as stale here and deleted. The same operator mistakes reach it,
    # and it is just as destructive. The automatic reload runs this same
    # reconcile on every profile change, so the exposure is there too, and it
    # heals on the next change to the checkout.
    #
    # **The obvious scoping fix was measured and does not work.** Reconciling
    # only within projects whose directory exists under `<root>/projects/`
    # would answer it without inventing a number — but `convivy-lab` has no
    # directory at all at `origin/main`, its documents having been removed
    # wholesale on 2026-09-11, and `convivy-lab/daytime` is one of the five
    # orphaned rows this command exists to clean up. That rule would strand it.
    #
    # **What remains is a threshold** — refuse when `stale` exceeds some share
    # of `existing` — and nobody has run this against a partial checkout, so
    # the number would be invented to defend a case no one has hit. The
    # reviewer that raised this declined to propose one for the same reason.
    # Recorded here, where whoever meets it will be reading, rather than built.
    verb = "would remove" if args.dry_run else "removed"
    for project, variant in gone:
        print(f"{verb} profile: {project}/{variant}")
    # Printed even at zero, so a no-op reconcile is visible as one rather
    # than looking like a run that never reached this pass at all.
    print(f"{verb} {len(gone)} profile(s)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    roster_p = sub.add_parser(
        "roster", help="load every name in ALLOWED_AUDIENCE, plus the "
                       "operator marker from the operator config")
    roster_p.add_argument(
        "knowledge_py",
        help="path to agent-platform's lib/knowledge/knowledge.py")
    roster_p.add_argument("--actor", required=True)
    roster_p.add_argument(
        "--reseed", action="store_true",
        help="add every ALLOWED_AUDIENCE name to a populated roster_agents "
             "table, removing nothing; without it a populated table refuses")
    roster_p.set_defaults(func=_roster)

    profiles_p = sub.add_parser(
        "profiles", help="load every projects/*/profiles/*.yaml")
    profiles_p.add_argument(
        "knowledge_root", help="path to the knowledge repository's root")
    profiles_p.add_argument("--actor", required=True)
    profiles_p.add_argument(
        "--dry-run", action="store_true",
        help="print what the reconcile pass would remove; write nothing")
    profiles_p.set_defaults(func=_profiles)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (crew_registry.InvalidRosterRow,
            crew_registry.RosterWriteForbidden,
            store.WritesDisabled) as e:
        print(f"load_crew_registry: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
