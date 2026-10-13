"""The Reference Room — read-only access to the knowledge index.

Contract §3. **The Reference Room's content stays files in the `knowledge`
repository** (ADR-0022), because review of a document means a diff. The Library
serves an INDEX over those files and returns ids that resolve to repository
paths; it does not become their store. That is why this module reads a different
database from `store.py` and returns `source_ref` on every hit.

**Search here is full-text, not semantic, and that is the index rather than a
simplification.** `~/.knowledge-index/index.db` carries an FTS5 table over id,
title, body and topics; its `embeddings` table holds zero rows. So retrieval is
BM25 over terms, which satisfies ADR-0019's determinism requirement outright:
the same query against the same corpus returns the same ranking, with no model
anywhere on the path.

**THE REFERENCE ROOM IS NOT PRODUCT-SCOPED, AND THAT IS DELIBERATE RATHER THAN AN
OMISSION.** The Chart House and the Archives are scoped hard — a caller must not
learn that another product's work item or signal exists. Reviewed documents are
the opposite: the KB is company knowledge, `kb_search` has never scoped by
project, and the dispatch machinery deliberately points several repos at the
`platform` corpus precisely so an agent working one product can read the
platform's design rationale. Scoping this the way items are scoped would take
ADR-0019 through ADR-0022 away from every agent that needs them.

`project` stays available as a FILTER, which is a caller narrowing its own
results, and is a different thing from a boundary it cannot cross.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from pathlib import Path

import store

INDEX_DB = Path(os.environ.get(
    "LIBRARY_KB_INDEX", str(Path.home() / ".knowledge-index" / "index.db")))

# ---------------------------------------------------------------------------
# THESE TWO CONSTANTS ARE DUPLICATED, and the duplication is the risk rather
# than the cost (reviewer, PR #2). The source of truth is
# `agent-platform/bin/mcp-kb-server` — `_BM25_WEIGHTS` and `_STOPWORDS`, around
# lines 95–113. The two repositories are separate deployables so sharing a
# package is not free, but copy-paste is the most fragile way to hold the
# invariant this design depends on: two search surfaces over one index that rank
# differently make "the KB says" depend on which door you came through.
#
# `test_the_ranking_constants_have_not_drifted` pins BOTH halves. It asserts the
# literal values here, which fails in CI the moment this copy changes, AND
# compares against the real `mcp-kb-server` when that checkout exists — which it
# does not in CI. **The cross-repo half is therefore invisible to CI by design**,
# the same shape that let `KNOWN_KB_PROJECTS` and orient's registry test rot
# unnoticed for two days each; it fails locally and on the mini, which is where
# anyone editing either file is working.
# ---------------------------------------------------------------------------
# Column-weighted BM25, in docs_fts column order (id, title, body, topics).
# id/title/topics outweigh body so a document surfaces when its NAME is queried,
# not only when the words appear deep in its prose. Taken from `mcp-kb-server`
# rather than re-derived: two search surfaces over one index that rank
# differently would make "the KB says" depend on which door you came through.
BM25_WEIGHTS = "10.0, 8.0, 1.0, 4.0"

# The FTS5 `snippet()` call `GET /reference/search` and `GET /search` both make
# against `docs_fts`: column 2 (`body`), bracketed with `<<`/`>>` rather than
# HTML so a caller does not have to know it is safe to render unescaped, an
# ellipsis for whatever the 24-token window cuts off. One constant so the two
# routes' excerpts cannot drift the way `BM25_WEIGHTS` above already guards
# against for ranking.
SNIPPET_SQL = "snippet(docs_fts, 2, '<<', '>>', '…', 24)"

# Function words dropped from a query, so a natural-language question does not
# OR in words absent from every document. Deliberately small — only true
# stopwords, so a domain term like "deploy" or "setup" always survives.
STOPWORDS = frozenset("""
a an the this that these those is are was were be been being am
i me my we our you your he she it they them their its his her
do does did how what when where which who whom why whose
to of in on at by for with from into onto out up down over under off
and or but nor if then else so as about than too very
can could should would may might must shall will
not no yes please
""".split())

# THE AUDIENCE FIELD IS RETURNED AND NOT ENFORCED, deliberately (the operator, 2026-08-08,
# `dec_01KZH8XTJWXK5SV2NJBCD0J3C1`). A wall was built here and denied; the argument
# on both sides lives in `app.py` beside the routes, and the condition that
# reverses it — go-to-market, or any access that is not the operator's own — is parked as
# `issue_01KZHD2AHS6MB4ZGYQFGPZPD2R`.
#
# The predicate that enforced it is gone rather than left unused: dead code that
# looks like a control is the shape this file already deleted twice, once for an
# unreachable `human` role and once for an unreachable carve-out. A caller that
# wants to respect the field can — it travels on every document.

# Archived and deprecated documents are hidden unless asked for by name. A
# superseded document answering a search as though it were current is worse than
# no answer, which is the same reason a Course defaults to `live`.
DEFAULT_STATUSES = ("current", "draft")

# **THE CLOSED VOCABULARY A `status` FILTER MAY NAME**, plus the sentinel that
# means "every one of them" (issue_01M1MWRXAZ5NRH10NP6Z9MBH0C).
#
# **`all` EXISTS BECAUSE THE CORPUS COUNT IS NOT DERIVABLE ANY OTHER WAY.**
# `total` on a listing is scoped to the filter, so a caller wanting every
# document had to issue one call per status and union them — which hardcodes
# this vocabulary in the caller. Add a fifth status and that caller silently
# drops its documents, and cannot detect it: summing the per-status totals IS
# the union again, not an independent number to check the union against, and
# nothing in the response envelope carries a corpus-wide count. Measured
# against the live index on 2026-09-10: the default answers 761, the four
# statuses answer 703/58/13/4, and the corpus is 778.
#
# **THIS SET MUST AGREE WITH THE WRITE SIDE**, `reference_admission
# .ALLOWED_STATUSES`, or a document admitted under a status this refuses
# becomes unreachable by the filter that would find it.
# `test_the_read_and_write_status_vocabularies_agree` pins both halves rather
# than importing across the two modules, matching how the BM25 constants are
# held.
STATUS_ALL = "all"
ALLOWED_STATUSES = frozenset({"draft", "current", "deprecated", "archived"})
assert STATUS_ALL not in ALLOWED_STATUSES


class ReferenceIndexUnavailable(RuntimeError):
    """The knowledge index cannot be opened for reading.

    **Mirrors `store.MisconfiguredDatabase`**: a distinct subclass rather than
    the generic `FileNotFoundError` or `OSError` other modules raise for
    unrelated files, so `app.py`'s handler can catch this and only this,
    without swallowing a same-shaped failure that has nothing to do with the
    Reference Room. `connect()` raises it for every way it can fail to hand
    back a usable connection — a missing index, a permission failure, or
    `sqlite3.connect()` itself failing after `stat()` already succeeded — so
    every caller, present and future, is covered by one `@app.exception_handler`
    rather than a check each call site must remember to add
    (`issue_01M1N7J265ST2V6VZ2C9TA4BN7`).
    """


def connect() -> sqlite3.Connection:
    """A read-only connection to the knowledge index, or `ReferenceIndexUnavailable`.

    **The URI comes from `store.db_uri` because interpolating the path raw defeats
    `mode=ro`.** SQLite splits the URI on its first `?`, so an `INDEX_DB` holding
    one — `LIBRARY_KB_INDEX` is caller-supplied and `?` is a legal POSIX filename
    character — opens a different file, creates it, and accepts writes through the
    handle this function promises is read-only. Found in `migrate.py` by a security
    reviewer (convivy/library#22) and in `store`/`vectors` on #32; this was the
    last site in the repo.

    **Stats the path rather than calling `is_file()`, because `is_file()` returns
    `False` on any `OSError`.** A missing file, a permission failure, a dead
    symlink and a path that vanished mid-check all collapse to the same `False`
    and, before this, the same "no knowledge index" message — twelve occurrences
    of that message in `serve.log` on 2026-09-03 left no evidence of which of
    those it actually was. The genuine-absence case (ENOENT) still gets its own
    "no knowledge index" message; anything else `stat()` raises carries its own
    errno and strerror instead, so the next occurrence's log line says which
    failure it hit rather than asserting absence by default.

    **`sqlite3.connect()` failing is caught too**, because a `stat()` that
    succeeds does not guarantee the open that follows it does — the file can be
    removed or replaced in the window between the two, and that failure is a
    `sqlite3.Error`, not an `OSError`, so it would otherwise reach a caller
    unhandled.
    """
    try:
        INDEX_DB.stat()
    except FileNotFoundError:
        raise ReferenceIndexUnavailable(f"no knowledge index at {INDEX_DB}") from None
    except OSError as e:
        raise ReferenceIndexUnavailable(f"{INDEX_DB} could not be statted: {e}") from e
    try:
        conn = sqlite3.connect(store.db_uri(INDEX_DB, readonly=True), uri=True)
    except sqlite3.Error as e:
        raise ReferenceIndexUnavailable(f"{INDEX_DB} could not be opened: {e}") from e
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def reading():
    """`connect()`, closed on exit, with a failed READ raised as
    `ReferenceIndexUnavailable` too (issue_01M4RD3919HN309R1S2KHR0JDQ).

    **The watcher rebuilds the index in place, so a connection that opened
    cleanly can fail on its first statement** with `database disk image is
    malformed` or `file is not a database`. That is a `sqlite3.Error` raised
    by the read rather than by `connect()`, and before this it reached the
    caller as a 500 at nine routes. Raised as the open's failure instead, the
    registered handler logs the detail and answers its sanitized 503, the
    answer library#259 gave `resolve_profile`.

    **`SQLITE_BUSY` is re-raised unchanged**, so `app.py`'s `database_busy`
    handler still answers it with `Retry-After`. Contention is not an
    unavailable index, and relabelling it would drop the retry hint.

    Use it only around reads of the index. A `sqlite3.Error` from any other
    database inside the block would be reported as the index's.
    """
    conn = connect()
    try:
        yield conn
    except sqlite3.OperationalError as e:
        if (getattr(e, "sqlite_errorcode", 0) or 0) & 0xFF == sqlite3.SQLITE_BUSY:
            raise
        raise ReferenceIndexUnavailable(f"{INDEX_DB} could not be read: {e}") from e
    except sqlite3.Error as e:
        raise ReferenceIndexUnavailable(f"{INDEX_DB} could not be read: {e}") from e
    finally:
        conn.close()


def build_match(query: str) -> str | None:
    """A free-text query as an FTS5 MATCH expression, or None if nothing survives.

    OR rather than FTS5's default AND, so a question-form query whose stopwords
    are absent from every document still retrieves on its content terms; the BM25
    weighting then floats the documents matching more, and rarer, terms.

    **THE TOKENIZER IS WHAT GUARANTEES SAFETY; THE QUOTING IS THE SECOND LINE**
    (reviewer, PR #2). `[a-z0-9]+` over a lowercased string means a term can never
    contain a double quote, whitespace, or any FTS5 operator character — `foo"bar`
    tokenizes to two terms and `café` to `caf`, both before quoting happens. So no
    caller input can break out of the expression. The quoting then stops a term
    that is ITSELF a bare keyword (`and`, `or`, `not`, `near`) parsing as an
    operator, which the tokenizer cannot prevent because such a term is
    alphanumeric and legitimate. A malformed MATCH is a 500 on a read path, and a
    cleverly-formed one is a query the caller did not write.
    """
    seen: set[str] = set()
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower())
             if len(t) > 1 and t not in STOPWORDS and not (t in seen or seen.add(t))]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def _json_or(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def score_of(bm25_value: float) -> float:
    """FTS5's raw BM25 output as a score where HIGHER MEANS MORE RELEVANT.

    SQLite's `bm25()` returns a NEGATIVE number and a more-negative one is a
    better match, which is the opposite of what every other score in the platform
    means — the adjacency briefing's `similarity` is a cosine where higher is
    closer. Handing a caller a raw negative under the name `score` would invite a
    threshold comparison that silently selects the WORST matches.

    So it is negated here, and trimmed to six SIGNIFICANT figures rather than to a
    fixed number of decimal places. **The distinction is not cosmetic: BM25's
    magnitude scales with the corpus**, and a fixed `round(x, 4)` silently returned
    0.0 for every document on a small index — caught by this repo's own fixture,
    where real scores are around 1e-06. Adjacency can round at four places because
    a cosine is bounded in [0, 1]; BM25 is unbounded in both directions and cannot.

    **The magnitude is not comparable across queries or corpus states** — BM25
    weights term rarity against the whole index, so a score on one query and a
    score on another say nothing about each other. It is an ordering signal with a
    number attached, which is what the contract asks for and all it is."""
    return float(f"{-bm25_value:.6g}")


def audience_clause(count: int, *, expr: str = "audience") -> str:
    """A JSON-array membership test, in SQL, guarded against a value this
    service does not write.

    **Generalised out of `app.py`'s `_audience_clause` (PR #47) to serve a
    second caller with a different shape.** `GET /reference/search` and `GET
    /reference/documents` test membership of ONE value; `crew_profiles.resolve`
    (issue_01M19QPV0J72EK1NDB0H8KT5EM) evaluates a profile's `audience_contains`
    rule, which names a LIST and matches any-of. `count` parameterises the
    number of bound placeholders rather than fixing it at one, so the same
    guarded shape serves both without a third copy of it appearing in
    `crew_profiles.py`. `app.py`'s `_audience_clause` is now a one-line call
    into this function; nothing about its behaviour or its callers changed.

    **`expr` NAMES WHAT TO TEST, NOT NECESSARILY A COLUMN.** It defaults to
    the bare `audience` column for a caller with no table alias
    (`crew_profiles.resolve` builds its query directly against `docs` with
    none); a caller that joins under an alias, as both Reference Room routes
    do, passes `"d.audience"`. **A caller may also pass any SQL expression
    that evaluates to a JSON value**, such as a guarded
    `json_extract(d.frontmatter_extra, '$.binds_agents')` — this is how the
    instruction-block selector fields are tested, since they live inside that
    column rather than one of their own. Generalised here rather than copied
    into a third place, so every membership test in this service shares one
    guard.

    **GUARDED BY `json_valid` BECAUSE THIS SERVICE DOES NOT WRITE THE VALUE
    UNDER TEST** (reviewer, PR #47). `json_each` raises `malformed JSON` on a
    non-NULL, non-JSON value — an empty string or any unparseable text — and
    one such row anywhere in the corpus would 500 this route for every
    caller, not just for that document. The index is built by the
    `knowledge` repository, so what lands here is not ours to guarantee.
    **This guards the value `expr` evaluates to, not `expr`'s own evaluation**
    — a caller building `expr` from a column that can itself fail to parse
    (as `frontmatter_extra` can) needs its own guard around that extraction
    first; see `app.py`'s `_selector_clause`.

    **BOTH CHECKS, NESTED, AND THE ORDER IS FORCED.** `json_valid` alone is
    too loose and `json_type` alone raises: `json_type` throws `malformed
    JSON` on exactly the values `json_valid` exists to catch, so the shape
    check can only run once parseability is established. Nested `CASE`
    forces that; `json_valid(x) AND json_type(x) = ...` works today but
    leans on `AND` short-circuiting, which is the assumption this comment
    already declines to make below.

    **WHY THE SHAPE CHECK AT ALL** (security-reviewer, PR #47 round 2).
    `json_valid` also passes a JSON scalar or object, and `json_each` on an
    object iterates its VALUES — so `{"a": "architect"}` would make
    `audience=architect` match a document whose audience was never
    `["architect"]`. A wrong answer rather than a crash, which is the worse
    failure because nothing reports it.

    **`CASE WHEN` RATHER THAN `json_valid(...) AND EXISTS(...)`.** `AND`
    would rely on SQLite evaluating the guard first, which the query planner
    is free to reorder; `CASE` is defined to evaluate its branches lazily, so
    the guard cannot be optimised away. A NULL audience takes the ELSE
    branch and matches nothing, which is the same answer `json_each` gives
    it, and an unparseable one now matches nothing instead of raising.
    """
    placeholders = ",".join("?" * count)
    return (f"CASE WHEN json_valid({expr}) THEN "
            f"(CASE WHEN json_type({expr}) = 'array' "
            f"THEN EXISTS (SELECT 1 FROM json_each({expr}) je "
            f"WHERE je.value IN ({placeholders})) ELSE 0 END) ELSE 0 END")


# ---------------------------------------------------------------------------
# `blurb` IS ORIENT'S RULE, COPIED, so the one-liner a listing row carries is
# the one `/memory` already renders (issue_01M0R80SMQC575NPYPGWPS3WME). The
# source is `_blurb` in convivy-orient `engineering_build.py`. Change the two
# together or not at all.
#
# **IT RUNS ON THE STORED BODY, WITH NO CLEANING STEP, AND THAT WAS MEASURED.**
# Orient passes `_blurb` a body run through `build._clean_body`, which strips
# leading frontmatter and every `## Provenance` section. On the live index on
# 2026-09-11, `_blurb(body) == _blurb(_clean_body(body))` for 763 of 763
# current and draft documents: the indexer stores no frontmatter, and provenance
# is a trailing section the first prose line never reaches. Copying the cleaning
# would put Orient's site policy in the Library to change nothing.
# ---------------------------------------------------------------------------
#
# **TWO STEPS DIFFER IN FORM FROM ORIENT'S, BECAUSE ORIENT'S ARE QUADRATIC ON
# UNCLOSED INPUT** (security-reviewer, PR #107). `re.sub(r"<!--.*?-->", ...)`
# rescans to the end of the body from every unclosed `<!--`, and `_MD_LINK`
# does the same from every `[` on a line with no `]`. A body of repeated
# `<!--` took 2.8s at 64,000 characters and quadrupled per doubling, and the
# listing runs this synchronously on the event loop, so one such document would
# stall every request the service is serving. `_strip_html_comments` gives the
# regex's exact result in one pass; `_MD_LINK` scans at most `BLURB_SCAN`
# characters of the line. The longest first prose line in the live index on
# 2026-09-11 was 1,330 characters, so the cut changes no blurb served today.
# ---------------------------------------------------------------------------
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_MARK = re.compile(r"[*_`>#]")
_NUMBERED_ITEM = re.compile(r"\d+\.\s")
BLURB_MAX = 220
BLURB_SCAN = 2000


def _strip_html_comments(body: str) -> str:
    """`re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)`, in linear time.

    Each `<!--` closes at the first `-->` that starts after it, which is what
    the non-greedy `.*?` matches, and the scan resumes after that close. An
    unclosed `<!--` has no `-->` after it, so no later `<!--` can close either,
    and the rest of the body is kept as the regex would keep it.
    """
    out, i = [], 0
    while (start := body.find("<!--", i)) != -1:
        end = body.find("-->", start + 4)
        if end == -1:
            break
        out.append(body[i:start])
        i = end + 3
    out.append(body[i:])
    return "".join(out)


def blurb(body: str) -> str:
    """The first prose line of a body, with simple markdown removed, cut to
    `BLURB_MAX` characters. A body with no prose line gives `""`.

    HTML comments go first, because they can span lines and
    `<!-- unsupported notion block: file -->` is not a summary. Skipped lines
    are blanks, headings, `---` rules, table rows, code fences, quotes and list
    items. An emphasised prose line (`**Bold** lead …`) is NOT skipped, which is
    why a bullet needs its trailing space: many documents open on one.
    """
    body = _strip_html_comments(body)
    for line in body.split("\n"):
        s = line.strip()
        if (not s or s.startswith(("#", "---", "|", "```", ">"))
                or s[:2] in ("- ", "* ", "+ ") or _NUMBERED_ITEM.match(s)):
            continue
        s = _MD_LINK.sub(r"\1", s[:BLURB_SCAN])
        s = _MD_MARK.sub("", s)
        return s[:BLURB_MAX]
    return ""


def has_frontmatter_extra(conn: sqlite3.Connection) -> bool:
    """Whether this index's `docs` table has a `frontmatter_extra` column.

    **A READ PATH CAN ASK THE ROW; A `WHERE` CLAUSE CANNOT.**
    `_frontmatter_extra_dict` below handles the column's absence per row, with
    `"frontmatter_extra" not in row.keys()` — an index predating
    agent-platform#517 has no such column, and this service does not build the
    index, so that state is real rather than theoretical. A filter has no row
    to ask: naming a missing column in SQL is a parse error, so
    `sqlite3.OperationalError: no such column` would 500 the whole request
    rather than returning nothing. The column has to be checked before the
    clause is built, which is what this is for.

    Asked of the connection rather than cached on the module, because the
    index is swapped underneath this service rather than migrated in place,
    and a cached answer would survive the swap that invalidates it.
    """
    return any(r[1] == "frontmatter_extra"
               for r in conn.execute("PRAGMA table_info(docs)"))


def _frontmatter_extra_dict(row: sqlite3.Row) -> dict | None:
    """The row's `frontmatter_extra` column as a dict, or `None` when there is
    nothing usable in it.

    **`None` on four distinct causes, all served the same way by `doc_dict`:**
    the column is absent (an index predating agent-platform#517), the value is
    NULL or empty, the text is not valid JSON, or it parses to something other
    than an object (`json.loads` accepts a bare list or scalar too). The index
    is written by `agent-platform`'s indexer, not this service, so a row this
    service did not write can hold any of these, and none of them is a reason
    to fail a request that asked for the document, not for this column.
    """
    if "frontmatter_extra" not in row.keys():
        return None
    raw = row["frontmatter_extra"]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def doc_dict(row: sqlite3.Row, *, body: bool, snippet: str | None = None,
             score: float | None = None, summary: bool = False) -> dict:
    """One document in protocol shape.

    `source_ref` is the point: it resolves to a path in the `knowledge`
    repository, which is where the document actually lives. A caller that wants
    to CHANGE one goes there and opens a diff, rather than asking the Library to
    write it.

    **`body_sha256` IS WHY EVERY REFERENCE GOES THROUGH THIS FUNCTION**
    (issue_01M19QQCKXW818SKY5A4G3XPHT). The Bosun asks what an agent needs and
    the Individuator fetches it afterwards, so a document can change between
    those two calls; the hash makes that visible instead of silent. Adding it
    here rather than at each route is what makes "every reference the Library
    returns carries one" true by construction — search hits, the listing, a
    single document and a resolved profile are four call sites and one field.

    **NAMED FOR THE BYTES IT ACTUALLY COVERS, WHICH ARE NOT THE FILE'S.** The
    index stores a document's frontmatter in its own columns and hashes only
    what is left, so two documents differing solely in frontmatter hash
    identically. `content_sha` would promise the file; this covers the body,
    and the body is exactly what `GET /reference/documents/{id}` returns — so a
    caller comparing what it fetched against the hash it was handed is
    comparing like with like. A hash of the source file would be the wrong
    hash here: it would change on edits this service never serves, and a
    caller could not verify it against anything the Library gives it.

    **IT IS A HASH OF WHAT THE INDEX HOLDS, not of what is on disk right now.**
    A change the watcher has not indexed yet is invisible to it, which is a
    property of reading an index rather than a gap in the hash — the Library
    serves the index, so the hash covers what the Library can serve.

    Read straight from the `body_hash` column, which `lib/knowledge/indexer.py`
    writes as `sha256(body)` at index time. Verified against the live index on
    2026-09-06 by recomputing it for all 750 documents: zero mismatches. This
    service does not rehash, because a second implementation of the same digest
    is a second thing to drift.

    **`summary` ADDS `blurb` AND `body_len`, for a row that omits the body.**
    The listing uses it so a client can render a one-liner and size a document
    without fetching ~760 bodies per page render. `body_len` is the STORED
    body's length in characters (code points, not bytes), the same text
    `body_sha256` covers. Orient's `/memory` strips provenance sections before
    measuring, so its own lengths differ on every document that has one; the
    operator accepted the stored length for `/memory`'s drawer threshold on 2026-09-11.

    **AN AGENT DOCUMENT CARRIES `model`, `tools`, `trigger`, `role`,
    `model_floor` AND `builtins` TOO** (issue_01M1RYSP06C98MN55ZAC47NVNA;
    `role` is issue_01M38TY0ES13YXCATFDG87C7B4, required on every agent
    document since knowledge#606; `model_floor`, the model fail-over may fall
    back to, is read by agentctl's `model_floor_for` once its `resolve_agent`
    reads this route, issue_01M35KPG11KD8BZTEVKWAMQX26; `builtins`, the
    built-in tool allowlist `agentctl spawn` narrows Claude Code's `--tools`
    to for a dispatched run, is read the same way by `resolve_agent` since
    agent-platform#818), read out of `frontmatter_extra` rather than a column
    of their own — the indexer stores every frontmatter key with
    no column under that one JSON blob (agent-platform#517), and these six
    are it for a `kind: agent` document. Only six of that blob's keys travel:
    `frontmatter_extra` also carries fields like `source_author` and `sourcing`
    that no caller has asked for, and serving the whole blob would be a wire
    contract change nobody agreed to. A non-agent document gets none of the
    six — an agent's tool policy and model are not a fact about a decision
    record. **A key `frontmatter_extra` does not carry is `None`, never
    invented; a `frontmatter_extra` this service cannot parse at all drops all
    six instead of guessing** — see `_frontmatter_extra_dict`. **`builtins`
    needs that distinction more than the other five do**: `resolve_agent`
    reads a missing key as no override on Claude Code's default built-ins and
    an empty list as "load none of them," so serving `[]` for an agent whose
    blob never set `builtins` would silently strip every built-in tool from a
    dispatch that never asked for that.

    **A `skill` DOCUMENT CARRIES `trigger`, AND ONLY THAT** (issue_01M34SV3BDRHBTXZPZ8C4AH9HD):
    `knowledge-ctx` writes each rendered SKILL.md's description from it, on
    the same `_frontmatter_extra_dict` contract as the agent fields: a skill
    with no `trigger` reads `None`, and an unparseable blob drops it. The
    agent-only fields stay off a skill, whatever its blob holds.

    **AN `instruction-block` DOCUMENT CARRIES `binds_crews`, `binds_agents`,
    `binds_topics`, `scope` AND `binds_roles` THE SAME WAY**
    (issue_01M2MC21YAWT2V61QWSM247VRX, `scope`/`binds_roles` added by
    issue_01M4AMQ15DGKET6NKXFZM0QS7X for the Bosun's `knowledge-ctx use`):
    the selector fields that say which crews, agents, topics and roles must
    obey the block, plus the block's `scope`, out of the same
    `frontmatter_extra` blob and on the same `_frontmatter_extra_dict`
    contract — a key that is not there is `None`, never invented, and an
    unparseable `frontmatter_extra` drops all five rather than guessing.
    **What is NOT re-validated here: a selector field that is present but
    is not a list of strings is served as whatever `frontmatter_extra`
    actually holds for that key**, matching how `model`, `tools` and
    `trigger` above are passed through with no shape check of their own.
    The sibling document validator in the `knowledge` repository is what
    refuses such a document at review time; this route reads an index built
    elsewhere and must not 500, or silently coerce, a row it did not write.
    """
    out = {
        "id": row["id"],
        "source_ref": row["source_ref"],
        "kind": row["kind"],
        "project": row["project"],
        "title": row["title"],
        "status": row["status"],
        "topics": _json_or(row["topics"], []),
        "audience": _json_or(row["audience"], []),
        "last_reviewed": row["last_reviewed"],
        "updated_at": row["updated_at"],
        "body_sha256": row["body_hash"],
    }
    if row["kind"] == "agent":
        extra = _frontmatter_extra_dict(row)
        if extra is not None:
            out["model"] = extra.get("model")
            out["tools"] = extra.get("tools")
            out["trigger"] = extra.get("trigger")
            out["role"] = extra.get("role")
            out["model_floor"] = extra.get("model_floor")
            out["builtins"] = extra.get("builtins")
    if row["kind"] == "skill":
        extra = _frontmatter_extra_dict(row)
        if extra is not None:
            out["trigger"] = extra.get("trigger")
    if row["kind"] == "instruction-block":
        extra = _frontmatter_extra_dict(row)
        if extra is not None:
            out["binds_crews"] = extra.get("binds_crews")
            out["binds_agents"] = extra.get("binds_agents")
            out["binds_topics"] = extra.get("binds_topics")
            out["scope"] = extra.get("scope")
            out["binds_roles"] = extra.get("binds_roles")
    if row["kind"] == "build-plan":
        # A build plan's one selector (issue_01M2TWRWHJMA6MF9NKZ9Z05KXN): a
        # coder dispatched on matching topics receives it. Without this
        # branch the filter matched on it and every response dropped it.
        extra = _frontmatter_extra_dict(row)
        if extra is not None:
            out["binds_topics"] = extra.get("binds_topics")
    if snippet is not None:
        out["snippet"] = snippet
    if score is not None:
        out["score"] = score
    if summary:
        out["blurb"] = blurb(row["body"])
        out["body_len"] = len(row["body"])
    if body:
        out["body"] = row["body"]
    return out
