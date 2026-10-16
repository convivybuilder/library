"""bin/library-deploy-watch — the launchd trigger that runs
`bin/library-install` whenever `origin/main` is ahead of the deploy area
(CQ·94, issue_01M1G7G14FMD7M30ZFQK9YZVST).

**Every case here imports the real script directly and monkeypatches its
own module-level functions** (`fetch_origin_main`, `origin_main_sha`,
`run_library_install`, `post_failure_signal`) rather than running it as a
subprocess against a real git repository. `bin/library-deploy-watch` has no
shell-specific semantics the way `bin/library-install` (`/bin/sh`) does, so
unlike `tests/test_library_install.py` it does not need a subprocess
harness to exercise its own parsing — the same reason
the retired `tests/test_ci_result_fetch.py` tested `post_signal`/`_retry_after_seconds`
by calling them directly against a faked `urllib.request.urlopen`, never a
real socket. This also means `run()`'s own decision logic (whether to
install, whether to suppress a repeat failure) is tested without ever
shelling out to git for real — this file never runs a real `git fetch`,
`git clone`, or `bin/library-install` against anything.

Loaded via `importlib.machinery.SourceFileLoader` rather than a plain
`import`, because the script is deliberately named without a `.py`
extension (matching `bin/library-install`'s own convention for the
installer it triggers) — Python's default finder only recognizes `.py`/
`.pyc` suffixes, so a bare `import library_deploy_watch` would not find it.
`HOME` is set before each load, since `DEPLOY_DIR`/`STATE_DIR` are both
resolved from it at module-load time (matching the retired `bin/ci_result_fetch.py`'s
own `STATE_DIR` idiom) — never a real `~/.local/deploy/library` or a real
`~/.library`.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "library-deploy-watch"


def _load_module():
    loader = importlib.machinery.SourceFileLoader("library_deploy_watch", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def watch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LIBRARY_STATE_DIR", raising=False)
    monkeypatch.delenv("LIBRARY_DEPLOY_WATCH_TOKEN", raising=False)
    return _load_module()


# --- read_deployed_commit: missing/unparseable both read as "nothing
# deployed", never as "up to date" ------------------------------------------


def test_read_deployed_commit_is_none_when_the_file_is_missing(watch):
    assert watch.read_deployed_commit() is None


def test_read_deployed_commit_parses_the_commit_line(watch):
    watch.DEPLOYED_COMMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    watch.DEPLOYED_COMMIT_FILE.write_text(
        "ref: main\ncommit: abc123\ninstalled_at: 2026-01-01T00:00:00Z\n")
    assert watch.read_deployed_commit() == "abc123"


def test_read_deployed_commit_is_none_when_there_is_no_commit_line(watch):
    watch.DEPLOYED_COMMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    watch.DEPLOYED_COMMIT_FILE.write_text("ref: main\n")
    assert watch.read_deployed_commit() is None


# --- the failed-target marker: save/load/clear round-trip, atomic write ----


def test_load_failed_target_is_none_when_the_file_is_missing(watch):
    assert watch.load_failed_target() is None


def test_save_then_load_failed_target_round_trips(watch):
    watch.save_failed_target("deadbeef", 1)
    assert watch.load_failed_target() == "deadbeef"


def test_load_failed_target_is_none_for_malformed_json(watch):
    watch.FAILED_TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    watch.FAILED_TARGET_FILE.write_text("{not json")
    assert watch.load_failed_target() is None


def test_clear_failed_target_removes_the_file(watch):
    watch.save_failed_target("deadbeef", 1)
    watch.clear_failed_target()
    assert watch.load_failed_target() is None


def test_clear_failed_target_is_a_no_op_when_nothing_was_ever_saved(watch):
    watch.clear_failed_target()  # must not raise


def test_save_failed_target_leaves_no_tmp_file_behind(watch):
    watch.save_failed_target("deadbeef", 1)
    leftovers = list(watch.FAILED_TARGET_FILE.parent.glob("*.tmp"))
    assert leftovers == []


# --- load_token: env var takes priority over the token file ----------------


def test_load_token_prefers_the_env_var(watch, monkeypatch):
    monkeypatch.setenv("LIBRARY_DEPLOY_WATCH_TOKEN", "from-env")
    assert watch.load_token() == "from-env"


def test_load_token_falls_back_to_the_token_file(watch):
    watch.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    watch.TOKEN_FILE.write_text("from-file\n")
    assert watch.load_token() == "from-file"


def test_load_token_raises_when_neither_is_set(watch):
    with pytest.raises(watch.DeployWatchError):
        watch.load_token()


# --- post_signal: same mapping the retired bin/ci_result_fetch.py's own post_signal
# uses, against a faked urllib.request.urlopen — never a real socket. ------


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_post_signal_maps_201_to_created(watch, monkeypatch):
    monkeypatch.setattr(watch.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(201))
    assert watch.post_signal({"dedup_key": "k"}, "tok") == "created"


def test_post_signal_maps_200_to_duplicate(watch, monkeypatch):
    monkeypatch.setattr(watch.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(200))
    assert watch.post_signal({"dedup_key": "k"}, "tok") == "duplicate"


def test_post_signal_maps_a_409_httperror_to_conflict(watch, monkeypatch):
    def raise_409(req, timeout=None):
        raise watch.urllib.error.HTTPError(
            "http://x/signals", 409, "Conflict", {}, None)
    monkeypatch.setattr(watch.urllib.request, "urlopen", raise_409)
    assert watch.post_signal({"dedup_key": "k"}, "tok") == "conflict"


def test_post_signal_maps_a_500_to_error_with_the_code(watch, monkeypatch):
    def raise_500(req, timeout=None):
        raise watch.urllib.error.HTTPError(
            "http://x/signals", 500, "Internal Server Error", {}, None)
    monkeypatch.setattr(watch.urllib.request, "urlopen", raise_500)
    assert watch.post_signal({"dedup_key": "k"}, "tok") == "error:500"


def test_post_signal_retries_a_429_then_succeeds(watch, monkeypatch):
    """The livelock fix the retired `bin/ci_result_fetch.py`'s own `post_signal` carried
    — a 429 is retried in place, not reported as a failure."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            import io
            body = json.dumps({"retry_after": 3}).encode("utf-8")
            raise watch.urllib.error.HTTPError(
                "http://x/signals", 429, "Too Many Requests", {}, io.BytesIO(body))
        return _FakeResponse(201)

    monkeypatch.setattr(watch.urllib.request, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(watch.time, "sleep", lambda s: slept.append(s))
    assert watch.post_signal({"dedup_key": "k"}, "tok") == "created"
    assert slept == [3]
    assert calls["n"] == 2


def test_post_signal_gives_up_after_the_retry_bound(watch, monkeypatch):
    def always_429(req, timeout=None):
        raise watch.urllib.error.HTTPError(
            "http://x/signals", 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(watch.urllib.request, "urlopen", always_429)
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    assert watch.post_signal({"dedup_key": "k"}, "tok") == "error:429"


def test_retry_after_seconds_falls_back_for_a_non_finite_header(watch):
    e = watch.urllib.error.HTTPError(
        "http://x/signals", 429, "Too Many Requests", {"Retry-After": "inf"}, None)
    assert watch._retry_after_seconds(e) == watch.RATE_LIMIT_FALLBACK_RETRY_SECONDS


# --- post_failure_signal: the payload shape, and the fixed-body/refs-carries
# -the-detail rule the retired bin/ci_result_fetch.py's own post_signal and
# convivy-orient/deploy_watch.py's post_failure_signal both follow. ---------


def test_post_failure_signal_builds_the_documented_payload_shape(watch, monkeypatch):
    monkeypatch.setattr(watch, "load_token", lambda: "tok")
    captured = {}

    def fake_post_signal(payload, token):
        captured["payload"] = payload
        captured["token"] = token
        return "created"

    monkeypatch.setattr(watch, "post_signal", fake_post_signal)
    watch.post_failure_signal("bin/library-install exited non-zero",
                              "library-deploy-watch:fail:abc123",
                              {"target": "abc123", "exit_code": 1})
    payload = captured["payload"]
    assert captured["token"] == "tok"
    assert payload["source"] == watch.SOURCE
    assert payload["kind"] == "deploy_failure"
    assert payload["actor"] == watch.SOURCE
    assert payload["product"] == "library"
    assert payload["body"] == "bin/library-install exited non-zero"
    assert payload["dedup_key"] == "library-deploy-watch:fail:abc123"
    assert payload["refs"] == {"target": "abc123", "exit_code": 1}


def test_post_failure_signal_swallows_a_missing_token_rather_than_raising(watch):
    # No LIBRARY_DEPLOY_WATCH_TOKEN and no token file: load_token raises
    # DeployWatchError, which post_failure_signal must catch and report
    # rather than let escape — the same best-effort contract
    # convivy-orient/deploy_watch.py's own post_failure_signal documents.
    watch.post_failure_signal("x", "k", {})  # must not raise


# --- run(): the decision logic, with every git/install/signal call stubbed -


def _stub(monkeypatch, watch, **overrides):
    for name, fn in overrides.items():
        monkeypatch.setattr(watch, name, fn)


class _CP:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_is_quiet_and_does_nothing_when_deployed_matches_origin(watch, monkeypatch):
    calls = {"install": 0, "signal": 0}
    _stub(monkeypatch, watch,
         fetch_origin_main=lambda: None,
         origin_main_sha=lambda: "same",
         read_deployed_commit=lambda: "same",
         run_library_install=lambda: calls.__setitem__("install", calls["install"] + 1) or _CP(0),
         post_failure_signal=lambda *a, **k: calls.__setitem__("signal", calls["signal"] + 1))
    assert watch.run() == 0
    assert calls == {"install": 0, "signal": 0}


def test_run_installs_when_deployed_is_behind_origin(watch, monkeypatch):
    cleared = []
    _stub(monkeypatch, watch,
         fetch_origin_main=lambda: None,
         origin_main_sha=lambda: "new-target",
         read_deployed_commit=lambda: "old",
         load_failed_target=lambda: None,
         run_library_install=lambda: _CP(0, "installed ok"),
         clear_failed_target=lambda: cleared.append(True))
    assert watch.run() == 0
    assert cleared == [True]


def test_run_logs_a_successful_installs_output_warnings_included(watch, monkeypatch, capsys):
    """An install that exits 0 can still have warned, as the installer does
    when a console script will not run after the swap (CQ·111). Its output
    reaches the log before the success line rather than being dropped."""
    _stub(monkeypatch, watch,
         fetch_origin_main=lambda: None,
         origin_main_sha=lambda: "new-target",
         read_deployed_commit=lambda: "old",
         load_failed_target=lambda: None,
         run_library_install=lambda: _CP(0, "▸ Deployed: area\n", "! pip --version failed\n"),
         clear_failed_target=lambda: None)
    assert watch.run() == 0
    out = capsys.readouterr().out
    assert "▸ Deployed: area" in out
    assert "! pip --version failed" in out
    assert out.index("! pip --version failed") < out.index("succeeded for new-target")


def test_run_reports_once_and_suppresses_the_next_tick_for_the_same_target(
        watch, monkeypatch):
    # load_failed_target/save_failed_target are deliberately left real here
    # — the suppression this test proves is their own file-backed round
    # trip through run(), not a stubbed stand-in for it.
    install_calls = []
    signal_calls = []
    _stub(monkeypatch, watch,
         fetch_origin_main=lambda: None,
         origin_main_sha=lambda: "bad-target",
         read_deployed_commit=lambda: "old",
         run_library_install=lambda: install_calls.append(1) or _CP(1, "out", "err"),
         post_failure_signal=lambda *a, **k: signal_calls.append(a))
    assert watch.run() == 1
    assert len(install_calls) == 1
    assert len(signal_calls) == 1
    assert watch.load_failed_target() == "bad-target"

    # A second tick for the SAME target must neither rerun the installer
    # nor repost — the once-per-target-commit rule CQ·94 asks for.
    assert watch.run() == 1
    assert len(install_calls) == 1, "must not rerun bin/library-install for a target already reported"
    assert len(signal_calls) == 1, "must not repost a deploy_failure signal for the same target"


def test_run_tries_again_once_origin_main_moves_past_a_failed_target(
        watch, monkeypatch):
    install_calls = []
    watch.save_failed_target("old-bad-target", 1)
    _stub(monkeypatch, watch,
         fetch_origin_main=lambda: None,
         origin_main_sha=lambda: "new-target",
         read_deployed_commit=lambda: "old",
         run_library_install=lambda: install_calls.append(1) or _CP(0))
    assert watch.run() == 0
    assert len(install_calls) == 1


def test_run_posts_the_target_exit_code_and_an_output_tail_on_failure(
        watch, monkeypatch):
    captured = {}
    _stub(monkeypatch, watch,
         fetch_origin_main=lambda: None,
         origin_main_sha=lambda: "bad-target",
         read_deployed_commit=lambda: "old",
         load_failed_target=lambda: None,
         run_library_install=lambda: _CP(17, "stdout line\n", "stderr line\n"),
         post_failure_signal=lambda body, dedup_key, refs: captured.update(
             body=body, dedup_key=dedup_key, refs=refs))
    assert watch.run() == 1
    assert captured["dedup_key"] == "library-deploy-watch:fail:bad-target"
    assert captured["refs"]["target"] == "bad-target"
    assert captured["refs"]["exit_code"] == 17
    assert "stdout line" in captured["refs"]["output_tail"]
    assert "stderr line" in captured["refs"]["output_tail"]


def test_run_bounds_the_output_tail(watch, monkeypatch):
    huge = "x" * (watch.TAIL_MAX_CHARS * 3)
    captured = {}
    _stub(monkeypatch, watch,
         fetch_origin_main=lambda: None,
         origin_main_sha=lambda: "bad-target",
         read_deployed_commit=lambda: "old",
         load_failed_target=lambda: None,
         run_library_install=lambda: _CP(1, huge, ""),
         post_failure_signal=lambda body, dedup_key, refs: captured.update(refs=refs))
    watch.run()
    assert len(captured["refs"]["output_tail"]) == watch.TAIL_MAX_CHARS


def test_run_reports_a_fetch_failure_with_a_fixed_non_target_dedup_key(
        watch, monkeypatch):
    captured = {}

    def fail_fetch():
        raise watch.DeployWatchError("git fetch origin main failed: boom")

    _stub(monkeypatch, watch,
         fetch_origin_main=fail_fetch,
         post_failure_signal=lambda body, dedup_key, refs: captured.update(
             body=body, dedup_key=dedup_key))
    assert watch.run() == 1
    assert captured["dedup_key"] == "library-deploy-watch:fail:fetch"


def test_run_treats_an_install_timeout_as_a_reportable_failure(watch, monkeypatch):
    captured = {}
    _stub(monkeypatch, watch,
         fetch_origin_main=lambda: None,
         origin_main_sha=lambda: "slow-target",
         read_deployed_commit=lambda: "old",
         run_library_install=lambda: (_ for _ in ()).throw(
             watch.DeployWatchError("timed out after 600s")),
         post_failure_signal=lambda body, dedup_key, refs: captured.update(
             dedup_key=dedup_key, refs=refs))
    assert watch.run() == 1
    assert captured["dedup_key"] == "library-deploy-watch:fail:slow-target"
    assert watch.load_failed_target() == "slow-target"


# --- CLI smoke test: no network, no git — argparse only --------------------


def test_cli_refuses_an_unknown_argument(tmp_path, monkeypatch):
    import subprocess
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [str(SCRIPT), "--bogus"], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
