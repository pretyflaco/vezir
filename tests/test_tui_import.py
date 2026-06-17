"""Import picker: flat list of all recordings + browse fallback.

Regression for the bug where the TUI Import picker rooted a DirectoryTree at
the last imported folder (a leaf session dir with one .ogg and no way to
navigate out), so the user saw a single recording.  The picker now lists
every recording under ~/vezir-meetings/ in a flat, scrollable, newest-first
OptionList, with a 'b' browse fallback.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.widgets import OptionList

from vezir.client.tui.record_screen import (
    ImportScreen,
    _recording_label,
    _scan_recordings,
)


def _make_recordings(base: Path) -> list[Path]:
    """Create a realistic ~/vezir-meetings/<team>/meeting-*/audio.ogg tree."""
    specs = [
        ("blink", "meeting-20260617-123708_DEVSTANDUP", "meeting-20260617-123708.ogg"),
        ("blink", "meeting-20260616-110055", "meeting-20260616-110055.ogg"),
        ("twentyone", "meeting-20260615-090000_SYNC", "meeting-20260615-090000.wav"),
    ]
    paths = []
    for team, sess, fname in specs:
        d = base / team / sess
        d.mkdir(parents=True, exist_ok=True)
        p = d / fname
        p.write_bytes(b"OggS" + b"\x00" * 32)
        paths.append(p)
    return paths


# ── pure helpers ──

def test_scan_finds_all_recordings_newest_first(tmp_path):
    _make_recordings(tmp_path)
    recs = _scan_recordings(tmp_path)
    assert len(recs) == 3
    # newest-first by the meeting-YYYYMMDD-HHMMSS timestamp
    assert recs[0].parent.name.startswith("meeting-20260617")
    assert recs[-1].parent.name.startswith("meeting-20260615")


def test_scan_empty_base_returns_empty(tmp_path):
    assert _scan_recordings(tmp_path / "does-not-exist") == []
    assert _scan_recordings(tmp_path) == []


def test_scan_skips_dotdirs(tmp_path):
    (tmp_path / ".trash" / "m").mkdir(parents=True)
    (tmp_path / ".trash" / "m" / "x.ogg").write_bytes(b"OggS")
    assert _scan_recordings(tmp_path) == []


def test_recording_label_format(tmp_path):
    p = (tmp_path / "blink" / "meeting-20260617-123708_DEVSTANDUP"
         / "meeting-20260617-123708.ogg")
    p.parent.mkdir(parents=True)
    p.write_bytes(b"OggS" + b"\x00" * 100)
    label = _recording_label(p, tmp_path)
    assert "blink/meeting-20260617-123708_DEVSTANDUP" in label
    assert "2026-06-17 12:37" in label


# ── modal behavior (Textual pilot) ──

class _Harness(App):
    """Minimal app to push ImportScreen and capture its dismissal result."""

    def __init__(self, browse_start: Path):
        super().__init__()
        self._browse_start = browse_start
        self.picked: object = "UNSET"

    def on_mount(self) -> None:
        def _done(result):
            self.picked = result
        self.push_screen(ImportScreen(self._browse_start), _done)


async def test_picker_lists_all_recordings(tmp_path, monkeypatch):
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))
    _make_recordings(tmp_path)
    app = _Harness(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        ol = app.screen.query_one("#picker-list", OptionList)
        assert ol.option_count == 3


async def test_picker_not_trapped_when_browse_start_is_leaf(tmp_path, monkeypatch):
    """Even if the browse-start is a single session dir, the flat list still
    shows every recording (the original bug)."""
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))
    recs = _make_recordings(tmp_path)
    leaf = recs[0].parent  # a single session folder
    app = _Harness(leaf)
    async with app.run_test() as pilot:
        await pilot.pause()
        ol = app.screen.query_one("#picker-list", OptionList)
        assert ol.option_count == 3  # all, not just the leaf's one


async def test_picker_select_dismisses_with_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))
    _make_recordings(tmp_path)
    app = _Harness(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # select highlighted (newest)
        await pilot.pause()
    assert isinstance(app.picked, Path)
    assert app.picked.parent.name.startswith("meeting-20260617")


async def test_picker_cancel_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))
    _make_recordings(tmp_path)
    app = _Harness(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.picked is None


async def test_picker_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))  # no recordings
    app = _Harness(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # empty-state shown, no option list
        assert len(app.screen.query("#picker-list")) == 0
        assert len(app.screen.query("#picker-empty")) == 1


async def test_picker_browse_toggle_mounts_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))
    _make_recordings(tmp_path)
    app = _Harness(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        assert len(app.screen.query("#picker-tree")) == 1
        assert len(app.screen.query("#picker-list")) == 0
