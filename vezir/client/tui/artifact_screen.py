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
class TextLoaded(Message):
    name: str
    body: str


@dataclass
class BinaryReady(Message):
    name: str
    path: Path


@dataclass
class LoadFailed(Message):
    error: str


@dataclass
class OpenerLaunched(Message):
    """The OS opener subprocess started and is still running after a brief probe."""
    name: str


@dataclass
class OpenerFailed(Message):
    """The OS opener subprocess exited non-zero, or Popen itself raised."""
    name: str
    detail: str


class ArtifactScreen(Screen):
    """View a single artifact for one session."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("s", "save_to_disk", "Save as…"),
        Binding("c", "copy_artifact", "Copy"),
    ]

    CSS = """
    ArtifactScreen { padding: 1 2; }
    TextArea { height: 1fr; }
    #status-line { height: 1; color: $text-muted; }
    """

    def __init__(
        self, session_id: str, name: str, *, is_attachment: bool = False
    ) -> None:
        super().__init__()
        self.session_id = session_id
        # Attachments live on their own routes (user-chosen filenames would
        # collide with millet's canonical artifact names in /artifact/...'s
        # flat namespace), so the fetch below picks the endpoint by this flag.
        self.is_attachment = is_attachment
        # NOTE: don't shadow Screen.name -- that's the install-name
        # property and Textual raises AttributeError on overwrite.
        # Use artifact_name for our artifact filename instead.
        self.artifact_name = name
        self._tmp_path: Path | None = None
        self._body: str | None = None
        self._area: TextArea | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static(f"loading {self.artifact_name}…", id="status-line")
            area = TextArea(read_only=True, id="artifact-text", language=None)
            self._area = area
            yield area
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self.query_one("#status-line", Static).update(f"loading {self.artifact_name}…")
        if _is_text(self.artifact_name):
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
        dest = default_dir / self.artifact_name
        try:
            if self._body is not None:
                dest.write_text(self._body, encoding="utf-8")
            elif self._tmp_path is not None:
                shutil.copy2(self._tmp_path, dest)
            self.notify(f"Saved to {dest}", severity="information")
        except Exception as exc:
            self.notify(f"Save failed: {exc}", severity="error")

    def action_copy_artifact(self) -> None:
        """Copy the artifact body (text) or temp path (binary) to clipboard.

        Text artifacts: the full decoded body goes to the clipboard so
        the user can paste a transcript or summary directly into chat /
        notes / a code editor.

        Binary artifacts: copying multi-megabyte PDF bytes through OSC
        52 is unsupported by most terminals (size cap is typically ~64K
        bytes) and useless anyway since the user wants the file, not
        the bytes inline.  Copy the local temp-file path instead so the
        user can run `evince <paste>` or `cp <paste> ~/somewhere`.
        """
        if self._body is not None:
            payload = self._body
            kind = "text"
        elif self._tmp_path is not None:
            payload = str(self._tmp_path)
            kind = "path"
        else:
            self.notify("Nothing loaded yet.", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(payload)
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error")
            return
        self.notify(
            f"Copied {self.artifact_name} ({kind}, {len(payload)} chars)",
            severity="information",
            timeout=4,
        )

    def _download(self):
        if self.is_attachment:
            return self.app.api.download_attachment(
                self.session_id, self.artifact_name,
            )
        return self.app.api.download_artifact(self.session_id, self.artifact_name)

    @work(thread=True, exclusive=True, group="artifact")
    def _text_worker(self) -> None:
        result = self._download()
        if not result.is_ok():
            self.post_message(LoadFailed(error=result.error_message()))
            return
        try:
            body = result.ok.decode("utf-8", errors="replace")
        except Exception as exc:
            self.post_message(LoadFailed(error=f"decode error: {exc}"))
            return
        self.post_message(TextLoaded(name=self.artifact_name, body=body))

    @work(thread=True, exclusive=True, group="artifact")
    def _binary_worker(self) -> None:
        result = self._download()
        if not result.is_ok():
            self.post_message(LoadFailed(error=result.error_message()))
            return
        # Write to a temp file with the right extension so the OS opener
        # picks the right app (PDF -> Preview/Okular etc).
        try:
            fd, tmp = tempfile.mkstemp(
                prefix="vezir-artifact-",
                suffix=Path(self.artifact_name).suffix or ".bin",
            )
            os.close(fd)
            Path(tmp).write_bytes(result.ok)
        except Exception as exc:
            self.post_message(LoadFailed(error=f"write tmp: {exc}"))
            return
        self.post_message(BinaryReady(name=self.artifact_name, path=Path(tmp)))

    def on_unmount(self) -> None:
        """Remove the downloaded artifact temp file on screen close (L-3).

        Binary artifacts (PDFs) can contain confidential meeting content;
        without this they accumulate in /tmp for the OS to reap.  Best
        effort — a launched external viewer may still hold the file open,
        which is fine (the unlink just drops our reference)."""
        tmp = getattr(self, "_tmp_path", None)
        if tmp is not None:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass

    def on_text_loaded(self, message: TextLoaded) -> None:
        self._body = message.body
        assert self._area is not None
        # TextArea.load_text in older textual, .text = … in newer; use .text.
        self._area.text = message.body
        self.query_one("#status-line", Static).update(
            f"{message.name}  ({len(message.body)} chars)  "
            f"[s] save  [escape] back"
        )

    def on_binary_ready(self, message: BinaryReady) -> None:
        """Kick the OS opener off in a worker so the TUI never blocks on it.

        PR2 inlined ``subprocess.Popen(...)`` in this handler, which is
        usually safe (DEVNULL'd FDs, start_new_session=True).  But on the
        muscle smoke we hit a hang: pressing enter on a PDF row launched
        xdg-open -> Evince on the local console's DISPLAY=:1, after which
        the TUI's keyboard input was no longer responsive.  Root cause
        unknown (likely Evince stealing focus on the local display), but
        the symptom is unacceptable.  Fix: never launch the opener on the
        UI thread.  The opener worker probes for ~0.5s and reports back
        via OpenerLaunched / OpenerFailed messages; the TUI stays
        responsive even if the launched process does something weird.
        """
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
        # Provisional UI state while the opener worker probes.
        self._area.text = (
            f"Launching {' '.join(cmd)} {message.path} ...\n\n"
            f"(if your viewer doesn't appear, press [s] to save a copy "
            f"to disk and open it manually; [escape] to go back)"
        )
        self.query_one("#status-line", Static).update(
            f"{message.name}  (launching opener)"
        )
        self._opener_worker(cmd, message.path, message.name)

    def on_load_failed(self, message: LoadFailed) -> None:
        assert self._area is not None
        self._area.text = f"Failed to load artifact: {message.error}"
        self.query_one("#status-line", Static).update("load failed")

    def on_opener_launched(self, message: OpenerLaunched) -> None:
        assert self._area is not None
        self._area.text = (
            f"Opened {message.name} in your default app.\n\n"
            f"Path: {self._tmp_path}\n\n"
            f"Press [s] to save a copy elsewhere, [escape] to go back."
        )
        self.query_one("#status-line", Static).update(
            f"{message.name}  (handed to OS opener)"
        )

    def on_opener_failed(self, message: OpenerFailed) -> None:
        assert self._area is not None
        self._area.text = (
            f"OS opener for {message.name} failed: {message.detail}\n\n"
            f"Path: {self._tmp_path}\n\n"
            f"Press [s] to save a copy elsewhere and open manually, "
            f"[escape] to go back."
        )
        self.query_one("#status-line", Static).update(
            f"{message.name}  (opener failed)"
        )

    @work(thread=True, exclusive=True, group="opener")
    def _opener_worker(self, cmd: list[str], path: Path, name: str) -> None:
        """Launch the OS opener and probe its exit status briefly.

        Returns control immediately on launch, but waits up to ~0.5s so
        that fast failures (binary not found, command refused) surface
        as OpenerFailed instead of a misleading "Opened" message.  If
        the launched process is still alive after the probe, we assume
        success and post OpenerLaunched.
        """
        try:
            proc = subprocess.Popen(
                [*cmd, str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            self.post_message(OpenerFailed(name=name, detail=str(exc)))
            return
        # Probe briefly for fast-exit failure.
        import time as _t
        deadline = _t.monotonic() + 0.5
        while _t.monotonic() < deadline:
            rc = proc.poll()
            if rc is None:
                _t.sleep(0.05)
                continue
            if rc != 0:
                self.post_message(OpenerFailed(
                    name=name, detail=f"opener exited with code {rc}",
                ))
            else:
                # Exited 0 within 0.5s -- means the opener handed off
                # to a real viewer that's now running detached.  Success.
                self.post_message(OpenerLaunched(name=name))
            return
        # Still running after probe -- assume success and stop watching.
        self.post_message(OpenerLaunched(name=name))
