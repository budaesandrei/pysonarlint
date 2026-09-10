"""Live progress on stderr.

Everything goes to stderr so `-f json` and `-f sarif` stay pipeable, and the whole
thing silences itself when stderr is not a terminal, so CI logs and captured output do
not fill with spinner frames.

A JVM start costs several seconds and each file costs a second or two, which looks
identical to a hang without this.
"""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time

_FRAMES = "|/-\\"
# U+28xx braille renders as a smooth spinner in modern terminals, but Windows consoles
# in a legacy code page cannot encode it, so it has to be earned rather than assumed.
_FRAMES_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _supports_unicode(stream) -> bool:  # noqa: ANN001
    encoding = getattr(stream, "encoding", None) or ""
    try:
        "".join(_FRAMES_UNICODE).encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


class Progress:
    """A one-line status display. Inert unless stderr is an interactive terminal."""

    def __init__(self, *, enabled: bool | None = None, stream=None) -> None:  # noqa: ANN001
        self._stream = stream or sys.stderr
        if enabled is None:
            enabled = bool(
                getattr(self._stream, "isatty", lambda: False)()
                and not os.environ.get("NO_COLOR")
                and os.environ.get("TERM") != "dumb"
                and not os.environ.get("CI")
            )
        self.enabled = enabled
        self._frames = _FRAMES_UNICODE if _supports_unicode(self._stream) else _FRAMES
        self._cycle = itertools.cycle(self._frames)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._message = ""
        self._width = 0
        self._done = 0
        self._total = 0
        self._issues = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self, message: str) -> None:
        """Begin animating with an indeterminate message."""
        if not self.enabled or self._thread is not None:
            return
        self._message = message
        self._stop.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self, final: str | None = None) -> None:
        """Stop animating and clear the line, optionally printing a closing message."""
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1.0)
            self._thread = None
        if not self.enabled:
            return
        self._clear()
        if final:
            print(final, file=self._stream, flush=True)

    def __enter__(self) -> Progress:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- updates -----------------------------------------------------------

    def note(self, message: str) -> None:
        """Print a line above the status area."""
        if not self.enabled:
            return
        with self._lock:
            self._clear()
            print(message, file=self._stream, flush=True)

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = total
            self._done = 0

    def advance(self, *, issues: int = 0, name: str = "") -> None:
        """Record one completed file."""
        with self._lock:
            self._done += 1
            self._issues = issues
            if name:
                self._message = name

    def set_message(self, message: str) -> None:
        with self._lock:
            self._message = message

    # -- rendering ---------------------------------------------------------

    def _animate(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                self._render(next(self._cycle))
            self._stop.wait(0.12)

    def _render(self, frame: str) -> None:
        line = f"{frame} {self._compose()}"
        columns = _terminal_width()
        if len(line) > columns - 1:
            line = line[: columns - 2] + "…"
        pad = max(0, self._width - len(line))
        self._stream.write("\r" + line + " " * pad)
        self._stream.flush()
        self._width = len(line)

    def _compose(self) -> str:
        if not self._total:
            return self._message
        percent = int(self._done / self._total * 100)
        bar = _bar(self._done, self._total)
        found = ""
        if self._issues:
            plural = "s" if self._issues != 1 else ""
            found = f", {self._issues} issue{plural}"
        return f"{bar} {percent:3d}%  {self._done}/{self._total}{found}  {self._message}"

    def _clear(self) -> None:
        if self._width:
            self._stream.write("\r" + " " * self._width + "\r")
            self._stream.flush()
            self._width = 0


def _bar(done: int, total: int, width: int = 20) -> str:
    filled = round(done / total * width) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _terminal_width(default: int = 80) -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return default
