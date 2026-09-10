"""The stderr progress indicator.

Two properties matter more than the cosmetics: it must be completely inert when stderr
is not an interactive terminal (otherwise CI logs and piped json fill with spinner
frames), and it must never leave an animation thread behind.
"""

from __future__ import annotations

import threading

import pytest

from pysonarlint.progress import (
    _FRAMES,
    _FRAMES_UNICODE,
    Progress,
    _bar,
    _supports_unicode,
    _terminal_width,
)


class FakeStream:
    """Just enough of a stream: isatty, encoding, write, flush."""

    def __init__(self, *, tty: bool = True, encoding: str = "utf-8") -> None:
        self._tty = tty
        self.encoding = encoding
        self.chunks: list[str] = []
        self.flushes = 0

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1

    @property
    def text(self) -> str:
        return "".join(self.chunks)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's NO_COLOR or CI must not decide what the tests observe."""
    for var in ("NO_COLOR", "CI", "TERM"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_threads() -> None:
    """Fail loudly if a test leaves an animation thread running."""
    before = threading.active_count()
    yield
    assert threading.active_count() == before


# -- enabled detection ----------------------------------------------------


def test_enabled_on_an_interactive_terminal() -> None:
    assert Progress(stream=FakeStream(tty=True)).enabled is True


def test_disabled_when_not_a_terminal() -> None:
    """This is what keeps piped json and CI logs clean."""
    assert Progress(stream=FakeStream(tty=False)).enabled is False


def test_disabled_on_a_stream_without_isatty() -> None:
    class Bare:
        encoding = "utf-8"

    assert Progress(stream=Bare()).enabled is False


@pytest.mark.parametrize(
    ("var", "value"),
    [("NO_COLOR", "1"), ("CI", "true"), ("TERM", "dumb")],
)
def test_environment_can_disable_it(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    monkeypatch.setenv(var, value)
    assert Progress(stream=FakeStream(tty=True)).enabled is False


def test_term_other_than_dumb_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    assert Progress(stream=FakeStream(tty=True)).enabled is True


def test_explicit_enabled_overrides_detection() -> None:
    assert Progress(enabled=True, stream=FakeStream(tty=False)).enabled is True
    assert Progress(enabled=False, stream=FakeStream(tty=True)).enabled is False


def test_defaults_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeStream(tty=False)
    monkeypatch.setattr("sys.stderr", stream)
    assert Progress()._stream is stream


# -- spinner frame selection ----------------------------------------------


def test_unicode_frames_on_a_utf8_stream() -> None:
    assert Progress(stream=FakeStream(encoding="utf-8"))._frames == _FRAMES_UNICODE


def test_ascii_frames_on_a_legacy_code_page() -> None:
    """A Windows console in cp1252 cannot encode U+28xx, so it must not be assumed."""
    assert Progress(stream=FakeStream(encoding="cp1252"))._frames == _FRAMES


def test_ascii_frames_on_an_unknown_encoding() -> None:
    assert Progress(stream=FakeStream(encoding="not-a-real-codec"))._frames == _FRAMES


@pytest.mark.parametrize(
    ("encoding", "supported"),
    [("utf-8", True), ("utf-16", True), ("cp437", False), ("ascii", False), ("", False)],
)
def test_supports_unicode(encoding: str, supported: bool) -> None:
    assert _supports_unicode(FakeStream(encoding=encoding)) is supported


def test_supports_unicode_on_a_stream_with_no_encoding_attribute() -> None:
    assert _supports_unicode(object()) is False


# -- updates --------------------------------------------------------------


def test_set_total_resets_the_done_counter() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.advance()
    p.set_total(5)
    assert (p._total, p._done) == (5, 0)


def test_advance_counts_files_and_records_the_name() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.set_total(3)
    p.advance(issues=2, name="a.py")
    p.advance(issues=7, name="b.py")
    assert p._done == 2
    assert p._issues == 7
    assert p._message == "b.py"


def test_advance_without_a_name_keeps_the_previous_one() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.set_message("keep me")
    p.advance()
    assert p._message == "keep me"


def test_set_message_replaces_the_message() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.set_message("syncing with server...")
    assert p._message == "syncing with server..."


def test_note_prints_above_the_status_line() -> None:
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p._render("|")  # draw a status line so there is something to clear
    stream.chunks.clear()
    p.note("found 3 file(s)")
    assert stream.text.startswith("\r")  # the status line is cleared first
    assert "found 3 file(s)" in stream.text


def test_note_is_silent_when_disabled() -> None:
    stream = FakeStream(tty=False)
    p = Progress(stream=stream)
    p.note("invisible")
    assert stream.text == ""


# -- composition ----------------------------------------------------------


def test_compose_is_just_the_message_before_a_total_is_known() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.set_message("starting analysis engine (JVM)...")
    assert p._compose() == "starting analysis engine (JVM)..."


def test_compose_includes_a_bar_percentage_and_tally() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.set_total(4)
    p.advance(name="a.py")
    assert p._compose() == "[#####---------------]  25%  1/4  a.py"


def test_compose_reports_issues_when_there_are_any() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.set_total(2)
    p.advance(issues=1, name="a.py")
    assert ", 1 issue " in p._compose()
    p.advance(issues=3, name="b.py")
    assert ", 3 issues " in p._compose()


def test_compose_omits_the_issue_tally_at_zero() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.set_total(1)
    p.advance(issues=0, name="a.py")
    assert "issue" not in p._compose()


@pytest.mark.parametrize(
    ("done", "total", "expected"),
    [
        (0, 10, "[--------------------]"),
        (5, 10, "[##########----------]"),
        (10, 10, "[####################]"),
        (1, 3, "[#######-------------]"),
        (0, 0, "[--------------------]"),  # no division by zero
    ],
)
def test_bar(done: int, total: int, expected: str) -> None:
    assert _bar(done, total) == expected


def test_bar_honours_a_custom_width() -> None:
    assert _bar(1, 2, width=4) == "[##--]"


# -- rendering ------------------------------------------------------------


def test_render_writes_a_carriage_returned_line() -> None:
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p.set_message("working")
    p._render("|")
    assert stream.text == "\r| working"
    assert stream.flushes == 1


def test_render_pads_over_a_previously_longer_line() -> None:
    """Without the padding, the tail of a longer previous line stays on screen."""
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p.set_message("a long message")
    p._render("|")
    stream.chunks.clear()
    p.set_message("short")
    p._render("|")
    assert stream.text == "\r| short" + " " * (len("| a long message") - len("| short"))


def test_render_truncates_to_the_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pysonarlint.progress._terminal_width", lambda default=80: 20)
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p.set_message("x" * 200)
    p._render("|")
    line = stream.text.lstrip("\r")
    assert len(line) == 19
    assert line.endswith("…")


def test_render_leaves_a_short_line_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pysonarlint.progress._terminal_width", lambda default=80: 80)
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p.set_message("short")
    p._render("|")
    assert "…" not in stream.text


def test_clear_erases_the_line_and_forgets_its_width() -> None:
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p.set_message("abc")
    p._render("|")
    width = p._width
    stream.chunks.clear()
    p._clear()
    assert stream.text == "\r" + " " * width + "\r"
    assert p._width == 0


def test_clear_is_a_no_op_when_nothing_was_drawn() -> None:
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p._clear()
    assert stream.text == ""


def test_terminal_width_falls_back_when_there_is_no_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise OSError("not a terminal")

    monkeypatch.setattr("os.get_terminal_size", boom)
    assert _terminal_width() == 80
    assert _terminal_width(default=120) == 120


def test_terminal_width_uses_the_real_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.get_terminal_size", lambda: type("S", (), {"columns": 133})())
    assert _terminal_width() == 133


# -- lifecycle ------------------------------------------------------------


def test_start_animates_then_stop_joins_the_thread() -> None:
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p.start("booting")
    assert p._thread is not None
    p.stop()
    assert p._thread is None
    # At least one frame was drawn, and the line was cleared on the way out.
    assert stream.text.startswith("\r")
    assert stream.text.endswith("\r")


def test_start_is_idempotent() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.start("first")
    thread = p._thread
    p.start("second")
    try:
        assert p._thread is thread
        assert p._message == "first"  # the second call did nothing at all
    finally:
        p.stop()


def test_start_does_nothing_when_disabled() -> None:
    stream = FakeStream(tty=False)
    p = Progress(stream=stream)
    p.start("booting")
    assert p._thread is None
    assert stream.text == ""


def test_stop_is_idempotent() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    p.start("booting")
    p.stop()
    p.stop()  # must not raise or try to join a dead thread
    assert p._thread is None


def test_stop_without_start_is_harmless() -> None:
    Progress(enabled=True, stream=FakeStream()).stop()


def test_stop_prints_a_closing_message() -> None:
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p.stop("done in 4.2s")
    assert "done in 4.2s" in stream.text


def test_stop_when_disabled_writes_nothing() -> None:
    stream = FakeStream(tty=False)
    p = Progress(stream=stream)
    p.stop("done")
    assert stream.text == ""


def test_context_manager_stops_on_exit() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    with p as entered:
        assert entered is p
        p.start("booting")
    assert p._thread is None


def test_context_manager_stops_on_an_exception() -> None:
    p = Progress(enabled=True, stream=FakeStream())
    with pytest.raises(RuntimeError), p:
        p.start("booting")
        raise RuntimeError("boom")
    assert p._thread is None


def test_animation_advances_through_frames() -> None:
    """The spinner must actually cycle, not redraw one frame forever."""
    stream = FakeStream()
    p = Progress(enabled=True, stream=stream)
    p.set_message("m")
    # Drive the render loop directly rather than waiting on wall-clock frames.
    for _ in range(3):
        p._render(next(p._cycle))
    drawn = [c.lstrip("\r")[0] for c in stream.chunks]
    assert drawn == list(p._frames[:3])
