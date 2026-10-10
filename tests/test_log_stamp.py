"""`log_stamp`: every line reaching `serve.log` starts with exactly one UTC
timestamp, however the line was written."""
from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import log_stamp

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ")


def _stream():
    raw = io.StringIO()
    return raw, log_stamp.TimestampedStream(raw, clock=lambda: "T ")


def test_a_line_is_stamped_once():
    raw, s = _stream()
    s.write("library: database busy\n")
    assert raw.getvalue() == "T library: database busy\n"


def test_print_style_split_writes_stamp_the_line_once():
    """`print` writes the text and the newline as separate calls. Stamping
    per call would put a second stamp in front of the newline."""
    raw, s = _stream()
    print("library: code abc", file=s)
    assert raw.getvalue() == "T library: code abc\n"


def test_a_multi_line_write_stamps_every_line():
    """A traceback arrives as one write holding many lines."""
    raw, s = _stream()
    s.write("Traceback (most recent call last):\n  File x\nValueError: y\n")
    assert raw.getvalue() == "T Traceback (most recent call last):\nT   File x\nT ValueError: y\n"


def test_a_continued_line_is_not_restamped():
    raw, s = _stream()
    s.write("library: staged ")
    s.write("commit abc\n")
    s.write("next\n")
    assert raw.getvalue() == "T library: staged commit abc\nT next\n"


def test_a_bare_carriage_return_does_not_start_a_new_line():
    """`serve.log`'s lines, the ones `grep` sees, end only at `\\n`. A
    progress bar such as tqdm, which fastembed's first model download shows on
    stderr, rewrites one line with bare `\\r`. Treating `\\r` as a line end
    put a stamp inside that line on every update (reviewer, PR #126)."""
    raw, s = _stream()
    s.write("50%\r75%\r100%\n")
    s.write("next\n")
    assert raw.getvalue() == "T 50%\r75%\r100%\nT next\n"


def test_crlf_still_ends_a_line_once():
    raw, s = _stream()
    s.write("a\r\nb\r\n")
    assert raw.getvalue() == "T a\r\nT b\r\n"


class _SwitchyRaw(io.StringIO):
    """A raw stream that yields the GIL inside every write, so a race in the
    wrapper's line-start bookkeeping actually happens during the test instead
    of only in theory."""

    def write(self, s):
        time.sleep(0)
        return super().write(s)


def test_concurrent_writers_never_put_a_stamp_mid_line():
    """What the lock guarantees, and all it guarantees. A stamp sits only at
    the start of a line in the file, and at most once per line, because the
    line-start flag always matches what was last written to the raw stream.

    It does NOT keep two threads' text from sharing one line. `print` writes
    the text and the newline as two calls, so another thread's text can land
    between them, and that was already possible before this wrapper existed.
    The assertion therefore checks where stamps are, not how many lines there
    are."""
    raw = _SwitchyRaw()

    def clock():
        time.sleep(0)
        return "@T "

    s = log_stamp.TimestampedStream(raw, clock=clock)

    def worker(n):
        for i in range(300):
            s.write(f"w{n} line {i}")
            s.write("\n")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for line in raw.getvalue().split("\n"):
        if not line:
            continue
        assert line.startswith("@T "), line
        assert line.count("@T ") == 1, line


def test_install_is_idempotent_and_wraps_both_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    log_stamp.install()
    log_stamp.install()
    assert isinstance(sys.stdout, log_stamp.TimestampedStream)
    assert isinstance(sys.stderr, log_stamp.TimestampedStream)
    assert not isinstance(sys.stderr._raw, log_stamp.TimestampedStream)


def test_the_real_clock_writes_a_utc_millisecond_stamp():
    raw = io.StringIO()
    s = log_stamp.TimestampedStream(raw)
    s.write("x\n")
    assert STAMP.match(raw.getvalue())


def test_installed_in_a_real_process_stamps_prints_and_tracebacks():
    """End to end, in a fresh interpreter: after `install()`, a plain print
    to stderr, a traceback, and a logging handler created afterwards (which is
    how uvicorn's handlers bind) all reach the real stderr stamped."""
    code = (
        "import log_stamp, sys, traceback, logging\n"
        "log_stamp.install()\n"
        "print('library: database busy', file=sys.stderr)\n"
        "try:\n    raise ValueError('boom')\nexcept ValueError:\n"
        "    traceback.print_exc(file=sys.stderr)\n"
        "h = logging.StreamHandler(sys.stderr)\n"
        "lg = logging.getLogger('t'); lg.addHandler(h); lg.warning('uvicorn-ish')\n"
    )
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", code], cwd=root,
                         capture_output=True, text=True, timeout=60, check=True)
    lines = [ln for ln in out.stderr.splitlines() if ln]
    assert lines, out.stderr
    assert all(STAMP.match(ln) for ln in lines), out.stderr
    assert any("database busy" in ln for ln in lines)
    assert any("ValueError: boom" in ln for ln in lines)
    assert any("uvicorn-ish" in ln for ln in lines)


def test_serve_dual_installs_only_when_run_as_the_service():
    """Importing `serve_dual`, as two test files do, must not wrap the
    process's streams."""
    before = sys.stderr
    import serve_dual  # noqa: F401
    assert sys.stderr is before
    src = (Path(__file__).resolve().parent.parent / "serve_dual.py").read_text()
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert "log_stamp.install()" in main_block
    assert main_block.index("log_stamp.install()") < main_block.index("asyncio.run(main())")
