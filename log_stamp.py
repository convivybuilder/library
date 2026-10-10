"""Prefix every line the Library writes to `serve.log` with a UTC timestamp.

**WHY THIS EXISTS: THE LOG COULD NOT SAY WHEN.** launchd sends the service's
stdout and stderr to `~/.library/serve.log`, and before this every line
reached it unstamped. On 2026-09-16 that left two questions unanswerable in
one day. Twelve `database busy` lines sat together at the end of the file,
and nothing could show whether they came from a backfill write at 17:49Z,
from a restart at 14:28Z, or from anything in between. The Bosun's lost
records at 15:09:54Z to 15:11:36Z therefore could not be matched to a single
line. A wrong cause was first read from how the lines were ordered, then
withdrawn once other timestamps disproved it. A log that records what
happened but not when cannot tell those apart.

**ONE WRAPPER AT THE ENTRY POINT, NOT A CHANGE AT EACH PRINT.** The service
writes through about a hundred `print(..., file=sys.stderr)` calls, through
`traceback.print_exc`, and through uvicorn's logging handlers. All of them
look up `sys.stdout` or `sys.stderr` when they write, or, for uvicorn, when
`uvicorn.Config` is built inside `serve_dual.main()`. Replacing both streams
once, before anything writes, stamps every one of them. A second logging
framework at each call site would stamp only the lines someone remembered
to convert.

**INSTALLED ONLY WHEN `serve_dual` RUNS AS THE SERVICE.** Two test files
import `serve_dual`. Installing on import would wrap pytest's own output, so
`install()` is called from `serve_dual`'s `__main__` block and nowhere else.

**A LINE IS STAMPED WHEN IT STARTS, NOT PER `write` CALL.** `print` can
arrive as several `write` calls ("text", then "\\n"), and a traceback can
arrive as one call holding many lines. The wrapper tracks whether the
underlying stream is at the start of a line and stamps each line exactly
once. **Only `\\n` ends a line**, because that is where the lines `grep` sees
end. A bare `\\r` does not: a progress bar such as tqdm, which fastembed's
first model download prints to stderr, rewrites one line with it, and
stamping after each `\\r` would bury several stamps inside that line
(reviewer, PR #126). The flag and the write happen under one lock, because request handlers
print from `run_in_threadpool` threads concurrently. Without the lock the
flag can fall out of step with what was actually written, and a stamp lands
in the middle of a line or is missing from one; the concurrency test fails
without it. **The lock does not keep two threads' text on separate lines.**
`print` writes the text and the newline as two calls, so another thread's
text can land between them. That was true before this wrapper existed, and
the merged line still starts with exactly one stamp.

UTC with milliseconds (`2026-09-16T18:30:01.123Z`), since stored and logged
timestamps are UTC by convention (ADR-0004).
"""
from __future__ import annotations

import datetime as _dt
import io
import sys
import threading


def _stamp() -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z "


class TimestampedStream(io.TextIOBase):
    """A text stream that prefixes each line written through it with
    `_stamp()`, then passes it to `raw`. Everything else is delegated to
    `raw`, so callers that check `isatty()` or `fileno()` see the real
    stream."""

    def __init__(self, raw, *, clock=_stamp):
        self._raw = raw
        self._clock = clock
        self._at_line_start = True
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            out = []
            for piece in s.splitlines(keepends=True):
                if self._at_line_start:
                    out.append(self._clock())
                out.append(piece)
                self._at_line_start = piece.endswith("\n")
            self._raw.write("".join(out))
        return len(s)

    def flush(self) -> None:
        self._raw.flush()

    def isatty(self) -> bool:
        return self._raw.isatty()

    def fileno(self) -> int:
        return self._raw.fileno()

    def writable(self) -> bool:
        return True

    @property
    def encoding(self):
        return getattr(self._raw, "encoding", None)


def install() -> None:
    """Replace `sys.stdout` and `sys.stderr` with timestamping wrappers. It
    is idempotent: calling it twice does not stamp a line twice."""
    for name in ("stdout", "stderr"):
        current = getattr(sys, name)
        if not isinstance(current, TimestampedStream):
            setattr(sys, name, TimestampedStream(current))
