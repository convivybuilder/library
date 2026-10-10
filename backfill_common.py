"""Shared support for the Library's product-correcting backfills
(issue_01M2K33M9NG0S9HCJQJ2EPD7RP): the crew-registry resolution, the
reader-scope precondition, and the paced batched writer.
`backfill_pr_state_product.py` and `backfill_model_resolution_product.py`
both import this rather than each carrying its own copy, so the two cannot
drift on what visibility, batching or pacing means.

**THE PACED WRITER: SMALL COMMITS, AND A PAUSE BETWEEN THEM.**
The budget that matters is the fastest client's, not the server's. A caller
waiting on the write lock is held for up to `store.BUSY_TIMEOUT_MS` (5 s)
before the server answers 503, but `signal_client`'s tee path gives up after
2 s and agentctl's finalize after 3 s. A slow minute therefore loses writes
as client timeouts, and a timed-out client cannot tell whether its write
landed. So each commit here has to finish far inside 2 s.

`BATCH_SIZE` (200) meets that with room to spare. On a copy of the 09-15
backup, a commit of 200 `UPDATE signals SET product = ? WHERE id = ?`
statements took 3 to 6 ms (measured 2026-09-16, rollback-journal mode).

`BATCH_SLEEP_SECONDS` (1.0 s) is insurance, not a fix for anything observed.
SQLite's busy handler retries a waiting connection about every 100 ms once it
has waited a few hundred milliseconds. A second-long pause after each commit
guarantees every waiter several retries while the lock is free, whatever
else is holding it. Over a ~2,224-row run that adds about 11 seconds.

**No incident is claimed for it.** An earlier version of this docstring
blamed lost writes on 2026-09-16 on `backfill_pr_state_product.py`'s
back-to-back commits. The timestamps rule that out: the write finished at
17:49:11Z, and the lost records were finalized between 15:09:54Z and
15:11:36Z. The service log has no timestamps, so the cause of those losses
is unknown.
"""
from __future__ import annotations

import sys
import time

import identity
import projects
import store

# How many single-row UPDATEs share one transaction. Measured at 3-6 ms per
# commit, far inside the 2-second budget of the fastest client. See the module
# docstring.
BATCH_SIZE = 200

# How long the writer pauses between one batch's commit and the next batch's
# first write: several of a waiting connection's ~100 ms busy retries. It is
# insurance, not a response to a demonstrated failure; see the module
# docstring.
BATCH_SLEEP_SECONDS = 1.0


def bare_repo(repo: str) -> str:
    """The bare repo name out of a value that may carry an owner prefix:
    `owner/repo` becomes `repo`. A value with no `/` — a model-resolution
    run's `meta.json` already names a bare repo — is returned unchanged, so
    this is safe to apply everywhere a repo name needs resolving.

    Exactly `_review_record_product`'s and `_merge_repo_scope`'s derivation
    in `app.py` (`repo.rsplit("/", 1)[-1]`), reused here so nothing that
    resolves a repo to a product can drift from those."""
    return repo.rsplit("/", 1)[-1]


def registry_available() -> bool:
    """Whether the crew registry read anything — checked before any
    resolution. `projects.resolve` fails OPEN on its own (an unreadable
    registry makes it return its input unchanged), which would make every
    stored product look "correct" because nothing could be resolved to
    compare it against. A caller must refuse rather than resolve anything
    when this is False."""
    return projects.available()


def resolve_product(repo: str) -> str | None:
    """The canonical product a repo resolves to, or None if the registry
    doesn't know it."""
    return projects.resolve(bare_repo(repo))


def reader_scopes() -> list[tuple[str, list[str]]]:
    """`(name, sorted products)` for every identity the Library resolves,
    from the only two sources `identity.resolve` reads. Tokens are the dict
    keys and are never returned, so nothing here can print one.
    `identity._safe_products` is applied exactly as `identity.resolve`
    applies it, so a malformed `products` reads as the empty scope it
    really is."""
    out = []
    for source in (identity._tokens(), identity._client_tokens()):
        for meta in source.values():
            if not isinstance(meta, dict):
                continue
            products = sorted(identity._safe_products(meta.get("products")))
            out.append((str(meta.get("name", "unknown")), products))
    return sorted(out)


def report_reader_scopes(changes: list[dict], *, script: str) -> list[tuple[str, list[str]]]:
    """Print every identity's scope, then name each narrowly scoped identity
    whose products include a product a move takes a row out of or puts a row
    into. Returns those identities; a non-empty list is the precondition
    failing. It does not refuse to write on its own, because a narrow reader
    may be meant to gain or lose these rows — see `refuse_write_on_scope_change`
    for the refusal that keeps this a person's call without letting it pass
    unread."""
    touched = set()
    for change in changes:
        touched.add(change["old_product"])
        touched.add(change["new_product"])
    affected = []
    for name, products in reader_scopes():
        print(f"{script}: reader {name}: "
              f"{','.join(products) or '(no products)'}", file=sys.stderr)
        if "*" in products:
            continue
        if touched & set(products):
            affected.append((name, products))
    if affected:
        for name, products in affected:
            print(f"{script}: PRECONDITION NOT MET: narrowly "
                  f"scoped reader {name} ({','.join(products)}) holds a "
                  f"product these moves touch, so its visibility changes",
                  file=sys.stderr)
    else:
        print(f"{script}: precondition met: no narrowly "
              f"scoped reader holds a product these moves touch",
              file=sys.stderr)
    return affected


def refuse_write_on_scope_change(*, write: bool, affected: list, accept_scope_change: bool,
                                  script: str) -> bool:
    """True (after printing why) when `--write` must be refused because the
    precondition failed and the operator has not named the change as
    intended. Printing the precondition alone would not do: an operator who
    goes straight to `--write` without a dry run is exactly how a visibility
    change would ship unread."""
    if write and affected and not accept_scope_change:
        print(f"{script}: refusing --write: the moves above "
              f"change what a narrowly scoped reader can see. Rerun with "
              f"--accept-scope-change if that is intended.", file=sys.stderr)
        return True
    return False


def print_group_lines(changes: list[dict], *, verb: str, script: str) -> None:
    for change in changes:
        print(f"{script}: {verb} {change['repo']} "
              f"{change['old_product']}->{change['new_product']}: "
              f"{len(change['ids'])} row(s)")


def apply_changes(changes: list[dict], *, sleep=time.sleep) -> None:
    """Write every change's `ids` under its `new_product`. `BATCH_SIZE`
    single-row `UPDATE ... WHERE id = ?` statements commit together, and
    every commit but the last is followed by a `sleep(BATCH_SLEEP_SECONDS)`
    pause before the next batch's first write — see the module docstring for
    why. Batches are drawn from the ids flattened across every change rather
    than restarted at each change's boundary, so the pacing holds evenly
    across the whole run regardless of how the ids happen to group by repo.

    `sleep` is injectable so a test can assert it was called the right
    number of times, and not after the last batch, without a real wait."""
    flat = [(change["new_product"], id_)
            for change in changes for id_ in change["ids"]]
    if not flat:
        return
    conn = store.connect_rw()
    try:
        for start in range(0, len(flat), BATCH_SIZE):
            if start > 0:
                sleep(BATCH_SLEEP_SECONDS)
            batch = flat[start:start + BATCH_SIZE]
            conn.executemany(
                "UPDATE signals SET product = ? WHERE id = ?", batch)
            conn.commit()
    finally:
        conn.close()
