"""bin/measure_retrieval_quality.py — the Archives retrieval-quality
measurement (library/decisions/0001-what-the-archives-keep-embed-and-move-cold,
issue_01M35R58Q5QEJ3J0PT387TWM18).

No network anywhere in this file. `search` is the one I/O seam and is
either monkeypatched at the `urllib.request.urlopen` level (to pin its real
status/error handling, matching the retired `test_ci_result_fetch.py`'s own
`test_post_signal_*` tests) or replaced wholesale by a stub `searcher`
passed into `run_questions`/`main`. Every scoring function is pure and
tested directly against hand-built id lists, never against the live
service or the shipped question fixture.
"""
from __future__ import annotations

import json

import measure_retrieval_quality as mrq
import pytest

# --- recall_at_k / precision_at_k / first_rank -------------------------------

def test_recall_at_k_counts_every_right_answer_found_in_the_window():
    right = {"a", "b", "c"}
    ranked = ["x", "a", "y", "b", "z"]
    assert mrq.recall_at_k(right, ranked, 5) == pytest.approx(2 / 3)


def test_recall_at_k_only_counts_within_the_first_k():
    right = {"a", "b"}
    ranked = ["x", "y", "a", "b"]
    assert mrq.recall_at_k(right, ranked, 2) == 0.0
    assert mrq.recall_at_k(right, ranked, 4) == 1.0


def test_recall_at_k_is_zero_when_no_right_answers_are_given():
    assert mrq.recall_at_k(set(), ["a", "b"], 5) == 0.0


def test_precision_at_k_divides_by_k_not_by_the_page_returned():
    """A page shorter than k (the window held fewer than k candidates at
    all) must not round the denominator down to the page length — that
    would hide the shortfall rather than score it."""
    right = {"a"}
    ranked = ["a"]
    assert mrq.precision_at_k(right, ranked, 5) == pytest.approx(1 / 5)


def test_precision_at_k_counts_only_right_answers_in_the_first_k():
    right = {"a", "b"}
    ranked = ["a", "x", "b", "y"]
    assert mrq.precision_at_k(right, ranked, 2) == pytest.approx(1 / 2)
    assert mrq.precision_at_k(right, ranked, 4) == pytest.approx(2 / 4)


def test_first_rank_is_one_indexed():
    assert mrq.first_rank({"b"}, ["a", "b", "c"]) == 2


def test_first_rank_is_the_first_match_when_several_right_answers_appear():
    assert mrq.first_rank({"a", "c"}, ["x", "c", "a"]) == 2


def test_first_rank_is_none_when_nothing_in_the_window_matches():
    assert mrq.first_rank({"z"}, ["a", "b", "c"]) is None


# --- score_question / summarize ----------------------------------------------

def _question(qid="q01", kind="finding", right=("sig_a",)):
    return {"id": qid, "kind": kind, "query": "why did X happen",
            "right_answers": list(right)}


def test_score_question_reports_every_k_and_the_first_rank():
    q = _question(right=("sig_b",))
    scored = mrq.score_question(q, ["sig_a", "sig_b", "sig_c"])
    assert scored["id"] == "q01"
    assert scored["first_rank"] == 2
    assert scored["recall_at_5"] == 1.0
    assert scored["precision_at_5"] == pytest.approx(1 / 5)
    assert scored["recall_at_10"] == 1.0
    assert scored["returned_ids"] == ["sig_a", "sig_b", "sig_c"]


def test_score_question_reports_a_miss_as_a_none_rank_and_zero_recall():
    q = _question(right=("sig_missing",))
    scored = mrq.score_question(q, ["sig_a", "sig_b"])
    assert scored["first_rank"] is None
    assert scored["recall_at_5"] == 0.0
    assert scored["precision_at_5"] == 0.0


def test_summarize_means_every_question_recall_and_precision():
    scored = [
        mrq.score_question(_question("q01", right=("a",)), ["a"]),
        mrq.score_question(_question("q02", right=("b",)), ["x", "b"]),
    ]
    summary = mrq.summarize(scored)
    assert summary["question_count"] == 2
    assert summary["mean_recall_at_5"] == pytest.approx(1.0)
    assert summary["not_found_count"] == 0
    # ranks 1 and 2 -> mean 1.5
    assert summary["mean_first_rank"] == pytest.approx(1.5)


def test_summarize_excludes_not_found_questions_from_the_mean_rank_but_counts_them():
    scored = [
        mrq.score_question(_question("q01", right=("a",)), ["a"]),
        mrq.score_question(_question("q02", right=("missing",)), ["x", "y"]),
    ]
    summary = mrq.summarize(scored)
    assert summary["not_found_count"] == 1
    # only q01's rank (1) enters the mean
    assert summary["mean_first_rank"] == pytest.approx(1.0)


def test_summarize_of_no_questions_reports_zero_means_not_a_crash():
    summary = mrq.summarize([])
    assert summary["question_count"] == 0
    assert summary["mean_recall_at_5"] == 0.0
    assert summary["mean_first_rank"] == 0.0
    assert summary["not_found_count"] == 0


# --- run_questions, via a stub searcher (no network) -------------------------

def test_run_questions_calls_the_searcher_once_per_question_and_scores_it():
    questions = [_question("q01", right=("a",)), _question("q02", right=("z",))]
    calls = []

    def stub_searcher(base_url, token, query, limit):
        calls.append((base_url, token, query, limit))
        return {"signals": [{"id": "a"}, {"id": "b"}]}

    scored = mrq.run_questions("http://x", "tok", questions, searcher=stub_searcher)
    assert len(calls) == 2
    assert calls[0] == ("http://x", "tok", "why did X happen", mrq.SEARCH_LIMIT)
    assert scored[0]["first_rank"] == 1
    assert scored[1]["first_rank"] is None


def test_run_questions_handles_a_response_with_no_signals_key():
    questions = [_question("q01", right=("a",))]

    def stub_searcher(base_url, token, query, limit):
        return {"signals": []}

    scored = mrq.run_questions("http://x", "tok", questions, searcher=stub_searcher)
    assert scored[0]["returned_ids"] == []
    assert scored[0]["first_rank"] is None


# --- search(): real urllib/HTTPError semantics, no FakeServer ---------------
#
# Matches the retired test_ci_result_fetch.py's own post_signal tests: urlopen is
# monkeypatched, but search()'s own status/error handling runs for real.

class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def test_search_returns_the_parsed_json_body(monkeypatch):
    payload = {"signals": [{"id": "sig_a"}], "query": "q"}

    def fake_urlopen(req, timeout=None):
        assert req.get_header("Authorization") == "Bearer tok"
        assert req.get_header("Library-protocol") == mrq.PROTOCOL_VERSION
        assert "/signals/search?" in req.full_url
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(mrq.urllib.request, "urlopen", fake_urlopen)
    result = mrq.search("http://x", "tok", "a query", 10)
    assert result == payload


def test_search_raises_on_an_http_error_rather_than_returning_a_falsy_result():
    def raise_503(req, timeout=None):
        raise mrq.urllib.error.HTTPError(
            "http://x/signals/search", 503, "Service Unavailable", {}, None)
    import measure_retrieval_quality as mod
    orig = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = raise_503
    try:
        with pytest.raises(RuntimeError, match="503"):
            mrq.search("http://x", "tok", "a query", 10)
    finally:
        mod.urllib.request.urlopen = orig


# --- print_compare ------------------------------------------------------------

def _result(questions, summary):
    return {"base_url": "http://x", "questions": questions, "summary": summary}


def test_print_compare_reports_the_per_question_and_mean_change(capsys):
    before_q = [mrq.score_question(_question("q01", right=("a",)), ["x", "a"])]
    after_q = [mrq.score_question(_question("q01", right=("a",)), ["a"])]
    before = _result(before_q, mrq.summarize(before_q))
    after = _result(after_q, mrq.summarize(after_q))

    mrq.print_compare(before, after)
    out = capsys.readouterr().out
    assert "q01" in out
    assert "1->1" in out or "2->1" in out


def test_print_compare_skips_a_question_present_in_only_one_file(capsys):
    before_q = [mrq.score_question(_question("q01", right=("a",)), ["a"])]
    after_q = [mrq.score_question(_question("q02", right=("b",)), ["b"])]
    before = _result(before_q, mrq.summarize(before_q))
    after = _result(after_q, mrq.summarize(after_q))

    mrq.print_compare(before, after)
    out = capsys.readouterr().out
    assert "q01: only in the before file, skipped" in out
    assert "q02: only in the after file, skipped" in out


# --- main(): both modes, end to end, no network -------------------------------

def test_main_run_mode_writes_a_result_file_and_prints_the_table(tmp_path, monkeypatch, capsys):
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(json.dumps({"questions": [
        {"id": "q01", "kind": "finding", "query": "why did X happen",
         "right_answers": ["sig_a"]},
    ]}))
    out_path = tmp_path / "results.json"

    def fake_search(base_url, token, query, limit):
        return {"signals": [{"id": "sig_a"}]}

    monkeypatch.setattr(mrq, "search", fake_search)
    monkeypatch.setattr("sys.argv", [
        "measure_retrieval_quality.py",
        "--questions", str(questions_path),
        "--url", "http://x", "--token", "tok",
        "--out", str(out_path)])

    mrq.main()

    written = json.loads(out_path.read_text())
    assert written["questions"][0]["first_rank"] == 1
    assert written["summary"]["question_count"] == 1
    out = capsys.readouterr().out
    assert "q01" in out
    assert f"wrote {out_path}" in out


def test_main_refuses_to_write_when_every_question_returns_zero_results(tmp_path, monkeypatch, capsys):
    """A token whose scope doesn't cover these signals, or a wrong --url,
    returns 200 with no signals for every question — no HTTPError to catch.
    That must not write a result file that reads as a total loss."""
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(json.dumps({"questions": [
        {"id": "q01", "kind": "finding", "query": "why did X happen",
         "right_answers": ["sig_a"]},
        {"id": "q02", "kind": "finding", "query": "why did Y happen",
         "right_answers": ["sig_b"]},
    ]}))
    out_path = tmp_path / "results.json"

    def fake_search(base_url, token, query, limit):
        return {"signals": []}

    monkeypatch.setattr(mrq, "search", fake_search)
    monkeypatch.setattr("sys.argv", [
        "measure_retrieval_quality.py",
        "--questions", str(questions_path),
        "--url", "http://x", "--token", "tok",
        "--out", str(out_path)])

    with pytest.raises(SystemExit) as exc_info:
        mrq.main()

    message = str(exc_info.value)
    assert "url" in message.lower()
    assert "token" in message.lower()
    assert "scope" in message.lower()
    assert not out_path.exists()


def test_main_requires_a_token_outside_compare_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("LIBRARY_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", [
        "measure_retrieval_quality.py", "--out", str(tmp_path / "out.json")])
    with pytest.raises(SystemExit):
        mrq.main()


def test_main_requires_out_outside_compare_mode(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "measure_retrieval_quality.py", "--token", "tok"])
    with pytest.raises(SystemExit):
        mrq.main()


def test_main_compare_mode_reads_two_files_and_never_calls_search(tmp_path, monkeypatch, capsys):
    before_q = [mrq.score_question(
        {"id": "q01", "kind": "finding", "query": "q", "right_answers": ["a"]},
        ["x", "a"])]
    after_q = [mrq.score_question(
        {"id": "q01", "kind": "finding", "query": "q", "right_answers": ["a"]},
        ["a"])]
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(_result(before_q, mrq.summarize(before_q))))
    after_path.write_text(json.dumps(_result(after_q, mrq.summarize(after_q))))

    def boom(*args, **kwargs):
        raise AssertionError("search must not be called in --compare mode")

    monkeypatch.setattr(mrq, "search", boom)
    monkeypatch.setattr("sys.argv", [
        "measure_retrieval_quality.py",
        "--compare", str(before_path), str(after_path)])

    mrq.main()
    out = capsys.readouterr().out
    assert "q01" in out
    assert "mean change" in out


# --- the shipped fixture itself -----------------------------------------------

def test_the_shipped_question_fixture_loads_and_every_question_has_a_right_answer():
    questions = mrq.load_questions(mrq.DEFAULT_QUESTIONS)
    assert len(questions) >= 30
    seen_ids = set()
    for q in questions:
        assert q["id"] not in seen_ids
        seen_ids.add(q["id"])
        assert q["query"].strip()
        assert q["right_answers"]
