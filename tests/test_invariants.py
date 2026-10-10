"""Structural checks on invariants the rest of the code argues FROM.

Three lease-disclosure sites are safe only because no edge in the item tree can
cross products, and that is enforced in `_validate_parent`. Every safety argument
about ancestor walks and descendant counts rests on it, and it used to rest on
every caller remembering to pass a real `product` — the check was skipped when
`product` was None, so the escape hatch failed open. It is unconditional now
(security-reviewer and reviewer, PR #12), which is the real defence; these tests
are the tripwire above it.

**These read the source rather than call it, deliberately.** The failure they
guard is a FUTURE writer — an import script, a data-fix route, a migration —
and a behavioural test cannot see a call site that does not exist yet. Reading
the tree catches the next one on the day it is written, which is the only moment
the fix is cheap.

**WHAT THEY DO NOT CATCH, stated plainly rather than left to be discovered.** A
source-reading guard sees syntax, so it is beaten by anything that moves the fact
out of the syntax it reads:

- **A `None` arriving through a variable.** `product=maybe_none` reads as an
  expression, not as a literal `None`. This is the one blind spot that stopped
  mattering: `_validate_parent` now refuses unconditionally, so a runtime None
  fails closed whatever the call site looks like.
- **SQL assembled rather than written.** `"UPDATE items SET " + "parent_id = ?"`
  splits the two keywords across separate constants, and an f-string that
  interpolates the column name keeps it out of the literal segments. Both pass.
- **A disclosure shaped differently.** These look for the ancestor walk and the
  serialiser by name. A handler that reaches a lease some third way, or builds a
  refusal dict by hand from fields it read itself, is not in the population.

So: a tripwire for the next accidental repeat of a known shape, not a defence
against a differently-shaped one. The behavioural tests are the backstop for the
sites that exist today.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_SRC = (ROOT / "app.py").read_text()
APP = ast.parse(APP_SRC)

# **`store.py` IS SCANNED TOO.** A query string defined there and executed from a
# route is invisible to a scan of `app.py` alone, which is a two-file version of
# the same evasion (security-reviewer, PR #12).
SQL_SOURCES = {name: ast.parse((ROOT / name).read_text())
               for name in ("app.py", "store.py")}

# `ast.AsyncFunctionDef` is a SIBLING of `ast.FunctionDef` rather than a subclass,
# so matching only the latter makes every `async def` invisible. Every route in
# this service is async, so a disclosure moving into one is not exotic.
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _calls_to(name: str):
    for node in ast.walk(APP):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == name):
            yield node


def test_every_parent_write_resolves_a_real_product():
    """Belt to `_validate_parent`'s braces. The function refuses unconditionally
    now, so a None can no longer skip the check — this catches the call site that
    means to pass one, at the moment it is written rather than at the moment it
    refuses everything in production."""
    calls = list(_calls_to("_validate_parent"))
    assert calls, "no _validate_parent call sites found; this test has gone blind"
    for call in calls:
        product = next((k.value for k in call.keywords if k.arg == "product"),
                       None)
        assert product is not None, (
            f"app.py:{call.lineno}: _validate_parent called without `product=`. "
            f"Pass the item's own product; the same-product check is what stops "
            f"an edge crossing products, and three lease disclosures rest on it")
        assert not (isinstance(product, ast.Constant) and product.value is None), (
            f"app.py:{call.lineno}: _validate_parent called with product=None. "
            f"That now refuses every parent rather than skipping the check — "
            f"pass the item's real product")


def test_parent_id_is_written_only_through_the_validated_path():
    """A second writer is how the invariant above stops being true. Any new SQL
    naming `parent_id` in a write has to be read against `_validate_parent`, so
    this fails until someone looks."""
    scanned, writes = 0, []
    for name, tree in SQL_SOURCES.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            sql = node.value.upper()
            if "UPDATE " not in sql and "INSERT " not in sql:
                continue
            scanned += 1
            if "PARENT_ID" in sql:
                writes.append(f"{name}:{node.lineno}")
    # A GO-BLIND GUARD, matching its siblings. `assert writes == []` passes
    # identically whether the scan found nothing or matched nothing, and a future
    # change in how SQL is built would make it the second (reviewer, PR #12).
    assert scanned > 5, (
        f"only {scanned} write statements found across {list(SQL_SOURCES)}; this "
        f"scan has gone blind and is no longer checking anything")
    assert writes == [], (
        f"{writes} write parent_id in literal SQL. If that is a new writer, it "
        f"must resolve its parent through `_validate_parent` with the item's own "
        f"product, or every ancestor-walk safety argument breaks. "
        f"`store.insert_item` is the one sanctioned writer and builds its columns "
        f"from ITEM_INSERT_COLUMNS rather than a literal")


def test_every_lease_disclosure_checks_the_roots_product():
    """Three routes disclose a blocking lease, and the check belongs at each
    disclosure rather than in the argument above it. All three got it by review
    rather than by construction; this is what stops a fourth.

    The population is any handler that reaches a lease it did not take — through
    the ancestor walk, or through the conflict a failed take raises — since the
    caller proved scope over the item it named and never over the row the lease
    sits on."""
    reaches_a_foreign_lease = ("_fence_holding(", "LeaseConflict")
    discloses = ("lease.lease_dict(", "_lease_refusal(", '["holder"]')
    checked = []
    for node in ast.walk(APP):
        if not isinstance(node, FUNC_TYPES):
            continue
        src = ast.get_source_segment(APP_SRC, node) or ""
        if not any(k in src for k in reaches_a_foreign_lease):
            continue
        if not any(k in src for k in discloses):
            continue
        checked.append(node.name)
        assert "_lease_root_visible(" in src, (
            f"{node.name} discloses a lease it did not take without checking "
            f"that lease root's own product. Call `_lease_root_visible` and fall "
            f"back to a refusal carrying no holder and no lease body")
    assert len(checked) >= 3, (
        f"found only {len(checked)} lease-disclosing handlers ({checked}); three "
        f"are known to exist, so this test has gone blind")
