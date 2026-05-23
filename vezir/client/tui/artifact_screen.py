"""Artifact viewer: text artifacts inline, binary handed to the OS opener.

Mirrors vezir-android's ArtifactViewerScreen.kt.  PDF, audio, and any
non-text artifact is downloaded to a tmp file and handed to xdg-open
(Linux) or open (macOS).  Text artifacts (md, txt, json, log) are
rendered inline with syntax-light highlighting.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, TextArea

log = logging.getLogger("vezir.client.tui.artifact")

_TEXT_EXTS = {".md", ".txt", ".json", ".log", ".html", ".csv", ".srt", ".vtt"}


def _is_text(name: str) -> bool:
    return Path(name).suffix.lower() in _TEXT_EXTS


def _os_opener_cmd() -> list[str] | None:
    if sys.platform == "darwin":
        return ["open"]
    if sys.platform.startswith("linux"):
        if shutil.which("xdg-open"):
            return ["xdg-open"]
    return None


@dataclass
class _TextLoaded(Message):
    name: str
    body: str


@dataclass
class _BinaryReady(Message):
    name: str
    path: Path


@dataclass
class _LoadFailed(Message):
    error: str


class ArtifactScreen(Screen):
    """View a single artifact for one session."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("s", "save_to_disk", "Save as…"),
    ]

    CSS = """
    ArtifactScreen { padding: 1 2; }
    TextArea { height: 1fr; }
    #status-line { height: 1; color: $text-muted; }
    """

    def __init__(self, session_id: str, name: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.name = name
        self._tmp_path: Path | None = None
        self._body: str | None = None
        self._area: TextArea | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static(f"loading {self.name}…", id="status-line")
            area = TextArea(read_only=True, id="artifact-text", language=None)
            self._area = area
            yield area
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self.query_one("#status-line", Static).update(f"loading {self.name}…")
        if _is_text(self.name):
            self._text_worker()
        else:
            self._binary_worker()

    def action_save_to_disk(self) -> None:
        if self._tmp_path is None and self._body is None:
            self.notify("Nothing loaded yet.", severity="warning")
            return
        default_dir = Path.home() / "Downloads"
        if not default_dir.exists():
            default_dir = Path.home()
        dest = default_dir / self.name
        try:
            if self._body is not None:
                dest.write_text(self._body, encoding="utf-8")
            elif self._tmp_path is not None:
                shutil.copy2(self._tmp_path, dest)
            self.notify(f"Saved to {dest}", severity="information")
        except Exception as exc:
            self.notify(f"Save failed: {exc}", severity="error")

    @work(thread=True, exclusive=True, group="artifact")
    def _text_worker(self) -> None:
        result = self.app.api.download_artifact(self.session_id, self.name)
        if not result.is_ok():
            self.post_message(_LoadFailed(error=result.error_message()))
            return
        try:
            body = result.ok.decode("utf-8", errors="replace")
        except Exception as exc:
            self.post_message(_LoadFailed(error=f"decode error: {exc}"))
            return
        self.post_message(_TextLoaded(name=self.name, body=body))

    @work(thread=True, exclusive=True, group="artifact")
    def _binary_worker(self) -> None:
        result = self.app.api.download_artifact(self.session_id, self.name)
        if not result.is_ok():
            self.post_message(_LoadFailed(error=result.error_message()))
            return
        # Write to a temp file with the right extension so the OS opener
        # picks the right app (PDF -> Preview/Okular etc).
        try:
            fd, tmp = tempfile.mkstemp(
                prefix="vezir-artifact-",
                suffix=Path(self.name).suffix or ".bin",
            )
            os.close(fd)
            Path(tmp).write_bytes(result.ok)
        except Exception as exc:
            self.post_message(_LoadFailed(error=f"write tmp: {exc}"))
            return
        self.post_message(_BinaryReady(name=self.name, path=Path(tmp)))

    def on_text_loaded(self, message: _TextLoaded) -> None:
        self._body = message.body
        assert self._area is not None
        # TextArea.load_text in older textual, .text = … in newer; use .text.
        self._area.text = message.body
        self.query_one("#status-line", Static).update(
            f"{message.name}  ({len(message.body)} chars)  "
            f"[s] save  [escape] back"
        )

    def on_binary_ready(self, message: _BinaryReady) -> None:
        self._tmp_path = message.path
        assert self._area is not None
        cmd = _os_opener_cmd()
        if cmd is None:
            self._area.text = (
                f"Binary artifact saved to {message.path}\n\n"
                f"No OS opener available; copy the path and open it manually."
            )
            self.query_one("#status-line", Static).update(
                f"{message.name}  (no xdg-open / open found)"
            )
            return
        try:
            subprocess.Popen(
                [*cmd, str(message.path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            self._area.text = f"Could not open {message.path}: {exc}"
            return
        self._area.text = (
            f"Opened {message.name} in your default app.\n\n"
            f"Path: {message.path}\n\n"
            f"Press [s] to save a copy elsewhere, [escape] to go back."
        )
        self.query_one("#status-line", Static).update(
            f"{message.name}  (handed to OS opener)"
        )

    def on_load_failed(self, message: _LoadFailed) -> None:
        assert self._area is not None
        self._area.text = f"Failed to load artifact: {message.error}"
        self.query_one("#status-line", Static).update("load failed")
