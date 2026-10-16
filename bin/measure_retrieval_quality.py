#!/usr/bin/env python3.11
"""measure_retrieval_quality — the Archives' semantic search measured against
a fixed question set (library/decisions/0001-what-the-archives-keep-embed-and-move-cold,
issue_01M35R58Q5QEJ3J0PT387TWM18).

The retention decision removes the vector rows of every kind it marks not
embedded, about 58% of the index today. This script is the measurement that
decision requires: run the same questions before and after that reduction,
and compare. It does not run the reduction and does not call the live
service on its own — a caller (a person, or the orchestrator) runs it once
before and once after.

**The question set is a fixture, not something this script computes.**
`tests/fixtures/retrieval_questions.json` holds about 30 queries an agent or
the operator would actually ask the Archives, each with the ids of the signals that
are right answers, picked by reading the signal's own row rather than by
running the search this script measures — a right-answer set built from the
search under test would make the measurement grade itself.

**Two independent modes, not two scripts**, because both read and write the
same JSON shape: `run` (below) hits `GET /signals/search` once per question
and writes a result file; `--compare` reads two such files and prints what
changed between them. Splitting them would mean keeping the result schema
in sync across two files instead of one.

Usage:
    bin/measure_retrieval_quality.py --url URL --token TOKEN --out RESULTS.json
    bin/measure_retrieval_quality.py --compare BEFORE.json AFTER.json

`--url` defaults to `LIBRARY_URL`, then `http://127.0.0.1:8100` — the same
fallback the retired `bin/ci_result_fetch.py` used. `--token` defaults to
`LIBRARY_TOKEN`. Neither is read from a state file: unlike the CI fetcher,
this script has no cursor and nothing scheduled ever runs it, so there is no
unattended caller a file-based fallback would serve.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8100"
# Every route but /health refuses a request without this header (app.py's
# negotiate_version middleware), so a call without it is a 400, not a search.
PROTOCOL_VERSION = "1"
DEFAULT_QUESTIONS = (Path(__file__).resolve().parent.parent
                     / "tests" / "fixtures" / "retrieval_questions.json")
K_VALUES = (5, 10)
# The search window requested per question. Equal to the largest k so every
# k's recall/precision is computed from one call; the rank of the first
# right answer is reported only up to this window (see `score_question`).
SEARCH_LIMIT = max(K_VALUES)


# --- pure scoring (unit-tested, no network) ----------------------------------

def recall_at_k(right_answers: set[str], ranked_ids: list[str], k: int) -> float:
    """Share of `right_answers` present in the top `k` of `ranked_ids`.

    0.0 when `right_answers` is empty — a question with no known right
    answer cannot be scored, and the fixture is expected to give every
    question at least one, so this is a defined floor rather than a
    division this module needs to guard against elsewhere."""
    if not right_answers:
        return 0.0
    hits = sum(1 for rid in ranked_ids[:k] if rid in right_answers)
    return hits / len(right_answers)


def precision_at_k(right_answers: set[str], ranked_ids: list[str], k: int) -> float:
    """Share of the top `k` of `ranked_ids` that are in `right_answers`.

    Divides by `k` itself, not by `len(ranked_ids[:k])` — a page short of
    `k` (the Archives held fewer than `k` candidates at all) still means the
    caller received fewer right answers than it asked for, and precision
    should read that as a cost rather than rounding the denominator down to
    hide it."""
    if k <= 0:
        return 0.0
    hits = sum(1 for rid in ranked_ids[:k] if rid in right_answers)
    return hits / k


def first_rank(right_answers: set[str], ranked_ids: list[str]) -> int | None:
    """1-indexed rank of the first `ranked_ids` entry in `right_answers`, or
    `None` when none of them appear anywhere in `ranked_ids` — which, since
    `ranked_ids` is the whole window a caller fetched, is reported as
    "not found in the window" rather than guessed at."""
    for i, rid in enumerate(ranked_ids):
        if rid in right_answers:
            return i + 1
    return None


def score_question(question: dict, ranked_ids: list[str]) -> dict:
    """One question's scores against the ids `/signals/search` returned, in
    the order it returned them — nearest first, per the route's own
    contract, so `ranked_ids` needs no re-sorting here."""
    right_answers = set(question["right_answers"])
    scores = {
        "id": question["id"],
        "kind": question["kind"],
        "query": question["query"],
        "right_answers": sorted(right_answers),
        "returned_ids": ranked_ids,
        "first_rank": first_rank(right_answers, ranked_ids),
    }
    for k in K_VALUES:
        scores[f"recall_at_{k}"] = recall_at_k(right_answers, ranked_ids, k)
        scores[f"precision_at_{k}"] = precision_at_k(right_answers, ranked_ids, k)
    return scores


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(scored_questions: list[dict]) -> dict:
    """The means `main()` prints and stores alongside the per-question
    table. `mean_first_rank` is taken only over questions whose right answer
    was found somewhere in the window — folding a "not found" question into
    that mean would need an arbitrary penalty value, and `not_found_count`
    already reports exactly what that mean cannot: how many questions this
    silently excludes."""
    summary: dict = {"question_count": len(scored_questions)}
    for k in K_VALUES:
        summary[f"mean_recall_at_{k}"] = _mean(
            [q[f"recall_at_{k}"] for q in scored_questions])
        summary[f"mean_precision_at_{k}"] = _mean(
            [q[f"precision_at_{k}"] for q in scored_questions])
    found_ranks = [q["first_rank"] for q in scored_questions
                   if q["first_rank"] is not None]
    summary["mean_first_rank"] = _mean([float(r) for r in found_ranks])
    summary["not_found_count"] = len(scored_questions) - len(found_ranks)
    return summary


# --- I/O: the search call and the question fixture ---------------------------

def load_questions(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data["questions"]


def search(base_url: str, token: str, query: str, limit: int) -> dict:
    """One `GET /signals/search` call. Raises on a non-2xx response or a
    transport failure — a measurement run that silently scored a failed
    call as "zero results" would report an index regression that never
    happened."""
    qs = urllib.parse.urlencode({"q": query, "limit": limit})
    req = urllib.request.Request(
        f"{base_url}/signals/search?{qs}",
        headers={"Authorization": f"Bearer {token}",
                 "Library-Protocol": PROTOCOL_VERSION},
        method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET /signals/search failed: {e.code} {body[:300]}") from e


def run_questions(base_url: str, token: str, questions: list[dict],
                  *, searcher=None) -> list[dict]:
    """Every question in `questions`, scored. `searcher` defaults to the
    module's own `search`, resolved at call time — the seam a test replaces
    with a stub that returns a canned response, never a real socket."""
    searcher = searcher if searcher is not None else search
    scored = []
    for question in questions:
        response = searcher(base_url, token, question["query"], SEARCH_LIMIT)
        ranked_ids = [s["id"] for s in response.get("signals", [])]
        scored.append(score_question(question, ranked_ids))
    return scored


# --- reporting ----------------------------------------------------------------

def print_table(scored_questions: list[dict]) -> None:
    header = (f"{'id':4} {'kind':16} {'rank':>5} "
              + " ".join(f"r@{k}/p@{k}".rjust(11) for k in K_VALUES)
              + "  query")
    print(header)
    for q in scored_questions:
        rank = "-" if q["first_rank"] is None else str(q["first_rank"])
        cells = " ".join(
            f"{q[f'recall_at_{k}']:.2f}/{q[f'precision_at_{k}']:.2f}".rjust(11)
            for k in K_VALUES)
        print(f"{q['id']:4} {q['kind']:16} {rank:>5} {cells}  {q['query']}")


def print_summary(summary: dict) -> None:
    print(f"\n{summary['question_count']} questions")
    for k in K_VALUES:
        print(f"  mean recall@{k}:    {summary[f'mean_recall_at_{k}']:.4f}")
        print(f"  mean precision@{k}: {summary[f'mean_precision_at_{k}']:.4f}")
    print(f"  mean first rank (of {summary['question_count'] - summary['not_found_count']} found): "
          f"{summary['mean_first_rank']:.2f}")
    print(f"  not found in window: {summary['not_found_count']}")


def print_compare(before: dict, after: dict) -> None:
    """Per-question change between two result files this script wrote, plus
    the change in each mean. Questions are matched by `id`; a question
    present in only one file is reported and skipped rather than compared
    against nothing."""
    before_by_id = {q["id"]: q for q in before["questions"]}
    after_by_id = {q["id"]: q for q in after["questions"]}
    only_before = before_by_id.keys() - after_by_id.keys()
    only_after = after_by_id.keys() - before_by_id.keys()
    for qid in sorted(only_before):
        print(f"{qid}: only in the before file, skipped")
    for qid in sorted(only_after):
        print(f"{qid}: only in the after file, skipped")

    common_ids = sorted(before_by_id.keys() & after_by_id.keys())
    header = (f"{'id':4} {'rank':>13} "
              + " ".join(f"d(r@{k}/p@{k})".rjust(13) for k in K_VALUES))
    print(header)
    for qid in common_ids:
        b, a = before_by_id[qid], after_by_id[qid]
        rank_change = f"{b['first_rank']}->{a['first_rank']}"
        cells = " ".join(
            f"{a[f'recall_at_{k}'] - b[f'recall_at_{k}']:+.2f}/"
            f"{a[f'precision_at_{k}'] - b[f'precision_at_{k}']:+.2f}".rjust(13)
            for k in K_VALUES)
        print(f"{qid:4} {rank_change:>13} {cells}")

    print("\nmean change (after - before):")
    for k in K_VALUES:
        d_recall = after["summary"][f"mean_recall_at_{k}"] - before["summary"][f"mean_recall_at_{k}"]
        d_precision = (after["summary"][f"mean_precision_at_{k}"]
                       - before["summary"][f"mean_precision_at_{k}"])
        print(f"  recall@{k}:    {d_recall:+.4f}")
        print(f"  precision@{k}: {d_precision:+.4f}")
    d_not_found = after["summary"]["not_found_count"] - before["summary"]["not_found_count"]
    print(f"  not found in window: {d_not_found:+d}")


# --- main ----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Measure the Archives' semantic search against a fixed "
                     "question set, or compare two prior measurements.")
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS,
                    help=f"the question fixture (default {DEFAULT_QUESTIONS})")
    ap.add_argument("--url", default=os.environ.get("LIBRARY_URL", DEFAULT_URL),
                    help="Library base URL (default $LIBRARY_URL or "
                         f"{DEFAULT_URL})")
    ap.add_argument("--token", default=os.environ.get("LIBRARY_TOKEN"),
                    help="bearer token for signal.read (default $LIBRARY_TOKEN)")
    ap.add_argument("--out", type=Path,
                    help="write the scored results as JSON to this path")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE_JSON", "AFTER_JSON"),
                    help="print the per-question change between two result "
                         "files this script wrote, instead of running "
                         "questions against a live service")
    args = ap.parse_args()

    if args.compare:
        before = json.loads(Path(args.compare[0]).read_text())
        after = json.loads(Path(args.compare[1]).read_text())
        print_compare(before, after)
        return

    if not args.token:
        sys.exit("no token: pass --token or set LIBRARY_TOKEN")
    if not args.out:
        sys.exit("--out is required outside --compare mode")

    questions = load_questions(args.questions)
    scored_questions = run_questions(args.url, args.token, questions)
    if scored_questions and all(not q["returned_ids"] for q in scored_questions):
        sys.exit(
            "every question returned zero results: --url, --token, or the "
            "token's scope probably doesn't cover these signals (refusing "
            "to write a measurement that would read as a total loss)")
    summary = summarize(scored_questions)
    print_table(scored_questions)
    print_summary(summary)

    result = {
        "base_url": args.url,
        "run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "k_values": list(K_VALUES),
        "questions": scored_questions,
        "summary": summary,
    }
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
