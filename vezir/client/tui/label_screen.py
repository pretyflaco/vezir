"""Speaker labeling screen.

Mirrors vezir-android's LabelScreen.kt + AudioClipPlayer.kt.  Loads
speakers from GET /api/label/{id}, lets the user type a name per
speaker with team-handle autocomplete, plays per-speaker WAV clips
via the cross-platform ffplay wrapper in vezir/client/audio.py, and
POSTs the final labels via /api/label/{id}.

Layout:
  one row per speaker:
    [speaker_id] [sample text]      [play]   [Input(name)]

  bottom action bar:
    [submit]  [cancel]
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static

from ..api import LabelInfo
from ..audio import AudioPlayer, FfplayNotFound, ffplay_available

log = logging.getLogger("vezir.client.tui.label")


@dataclass
class _LabelInfoLoaded(Message):
    info: LabelInfo


@dataclass
class _LabelLoadFailed(Message):
    error: str


@dataclass
class _ClipReady(Message):
    speaker_id: str
    path: Path


@dataclass
class _ClipFailed(Message):
    speaker_id: str
    error: str


@dataclass
class _SubmitDone(Message):
    ok: bool
    error: str = ""


class LabelScreen(Screen):
    """Apply speaker labels to a session."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Cancel"),
        Binding("ctrl+r", "refresh", "Refresh"),
    ]

    CSS = """
    LabelScreen { padding: 1 2; }
    #speakers-container { height: 1fr; overflow-y: auto; }
    .speaker-row {
        height: 3;
        border-bottom: solid $surface;
        padding: 0 0 1 0;
    }
    .speaker-id {
        width: 14;
        color: $accent;
        text-style: bold;
    }
    .sample {
        color: $text-muted;
        width: 1fr;
    }
    .play-btn { min-width: 8; }
    .name-input { width: 28; }
    #actions { height: 3; margin-top: 1; }
    """

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id
        self._info: LabelInfo | None = None
        # speaker_id -> Input widget
        self._inputs: dict[str, Input] = {}
        # speaker_id -> cached clip Path
        self._clip_paths: dict[str, Path] = {}
        # Shared ffplay player
        self._player = AudioPlayer()
        self._tmpdir = Path(tempfile.mkdtemp(prefix="vezir-label-"))

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("loading speakers…", id="status-line")
            yield Vertical(id="speakers-container")
            with Horizontal(id="actions"):
                yield Button("Submit labels", id="submit-btn", variant="primary")
                yield Button("Cancel", id="cancel-btn")
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    def on_unmount(self) -> None:
        self._player.stop()
        # Best-effort cleanup of the tmp clips dir
        try:
            import shutil as _sh
            _sh.rmtree(self._tmpdir, ignore_errors=True)
        except Exception:
            pass

    def action_refresh(self) -> None:
        self.query_one("#status-line", Static).update("loading speakers…")
        self._info_worker()

    @work(thread=True, exclusive=True, group="label-info")
    def _info_worker(self) -> None:
        result = self.app.api.get_label_info(self.session_id)
        if not result.is_ok():
            self.post_message(_LabelLoadFailed(error=result.error_message()))
            return
        self.post_message(_LabelInfoLoaded(info=result.ok))

    @work(thread=True, exclusive=False, group="label-clip")
    def _clip_worker(self, speaker_id: str) -> None:
        dest = self._tmpdir / f"{speaker_id}.wav"
        if dest.exists():
            self.post_message(_ClipReady(speaker_id=speaker_id, path=dest))
            return
        result = self.app.api.save_clip(self.session_id, speaker_id, dest)
        if not result.is_ok():
            self.post_message(_ClipFailed(
                speaker_id=speaker_id, error=result.error_message(),
            ))
            return
        self.post_message(_ClipReady(speaker_id=speaker_id, path=dest))

    @work(thread=True, exclusive=True, group="label-submit")
    def _submit_worker(self, labels: dict[str, str]) -> None:
        result = self.app.api.submit_labels(self.session_id, labels)
        if result.is_ok():
            self.post_message(_SubmitDone(ok=True))
        else:
            self.post_message(_SubmitDone(ok=False, error=result.error_message()))

    # ── messages ──

    def on_label_info_loaded(self, message: _LabelInfoLoaded) -> None:
        self._info = message.info
        self._render()

    def on_label_load_failed(self, message: _LabelLoadFailed) -> None:
        self.query_one("#status-line", Static).update(
            f"[red]Failed to load speakers: {message.error}[/red]",
        )

    def on_clip_ready(self, message: _ClipReady) -> None:
        self._clip_paths[message.speaker_id] = message.path
        try:
            self._player.play(message.path)
        except FfplayNotFound as exc:
            self.notify(str(exc), severity="error")
        except Exception as exc:
            self.notify(f"Playback failed: {exc}", severity="error")

    def on_clip_failed(self, message: _ClipFailed) -> None:
        self.notify(
            f"Could not fetch clip for {message.speaker_id}: {message.error}",
            severity="error",
        )

    def on_submit_done(self, message: _SubmitDone) -> None:
        if message.ok:
            self.notify("Labels submitted", severity="information")
            self.app.pop_screen()
        else:
            self.notify(
                f"Submit failed: {message.error}", severity="error",
            )

    # ── buttons ──

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cancel-btn":
            self.app.pop_screen()
            return
        if bid == "submit-btn":
            self._do_submit()
            return
        # Play buttons have id "play-<speaker_id>"
        if bid and bid.startswith("play-"):
            speaker_id = bid[len("play-"):]
            if self._player.is_playing and self._player.current_path == self._clip_paths.get(speaker_id):
                self._player.stop()
                return
            if speaker_id in self._clip_paths:
                try:
                    self._player.play(self._clip_paths[speaker_id])
                except FfplayNotFound as exc:
                    self.notify(str(exc), severity="error")
                except Exception as exc:
                    self.notify(f"Playback failed: {exc}", severity="error")
            else:
                self._clip_worker(speaker_id)

    def _do_submit(self) -> None:
        labels: dict[str, str] = {}
        for spk_id, inp in self._inputs.items():
            v = (inp.value or "").strip()
            if v:
                labels[spk_id] = v
        if not labels:
            self.notify("No labels entered.", severity="warning")
            return
        self._submit_worker(labels)

    # ── render ──

    def _render(self) -> None:
        info = self._info
        if info is None:
            return
        container = self.query_one("#speakers-container", Vertical)
        container.remove_children()
        self._inputs.clear()

        if not info.speakers:
            self.query_one("#status-line", Static).update(
                "[green]All speakers already labeled.[/green]",
            )
            return

        play_available = ffplay_available() and info.audio_available
        self.query_one("#status-line", Static).update(
            f"{len(info.speakers)} speaker(s) to label  ·  "
            f"clips: {'available' if play_available else 'unavailable'}",
        )

        for sp in info.speakers:
            sid = str(sp.get("id", "?"))
            sample = (sp.get("sample_text") or "")[:80]
            row = Horizontal(classes="speaker-row")
            container.mount(row)
            row.mount(Label(sid, classes="speaker-id"))
            row.mount(Label(sample, classes="sample"))
            play_btn = Button(
                "▶ Play",
                id=f"play-{sid}",
                classes="play-btn",
                disabled=not play_available,
            )
            row.mount(play_btn)
            inp = Input(
                placeholder="github handle",
                id=f"input-{sid}",
                classes="name-input",
                # Textual's Input supports a `suggester` API for
                # autocomplete; wire team handles in.
            )
            try:
                from textual.suggester import SuggestFromList
                inp.suggester = SuggestFromList(info.team, case_sensitive=False)
            except Exception:  # pragma: no cover - older textual
                pass
            self._inputs[sid] = inp
            row.mount(inp)
