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
class LabelInfoLoaded(Message):
    info: LabelInfo


@dataclass
class LabelLoadFailed(Message):
    error: str


@dataclass
class ClipReady(Message):
    speaker_id: str
    path: Path


@dataclass
class ClipFailed(Message):
    speaker_id: str
    error: str


@dataclass
class SubmitDone(Message):
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
        /* PR8: Textual's default Input has height: 3 (border-top +
         * content row + border-bottom).  Previous height:3 with a
         * padding-bottom of 1 left only 2 rows of usable content,
         * clipping the Input's content row entirely so typed text
         * was invisible, and squashing the Button's borders so its
         * centered label rendered nowhere ("all black box").  Bump
         * to 5 and use margin (which doesn't consume children's
         * space) instead of padding for separation.  align middle
         * vertically centers the 1-row Labels next to the 3-row
         * Input. */
        height: 5;
        margin-bottom: 1;
        border-bottom: solid $surface;
        align: left middle;
    }
    .speaker-id {
        width: 14;
        color: $accent;
        text-style: bold;
        height: 100%;
        content-align: left middle;
    }
    .sample {
        color: $text-muted;
        width: 1fr;
        height: 100%;
        content-align: left middle;
    }
    .play-btn { min-width: 10; margin: 0 1; }
    .name-input { width: 32; }
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
            self.post_message(LabelLoadFailed(error=result.error_message()))
            return
        self.post_message(LabelInfoLoaded(info=result.ok))

    @work(thread=True, exclusive=False, group="label-clip")
    def _clip_worker(self, speaker_id: str) -> None:
        dest = self._tmpdir / f"{speaker_id}.wav"
        if dest.exists():
            self.post_message(ClipReady(speaker_id=speaker_id, path=dest))
            return
        result = self.app.api.save_clip(self.session_id, speaker_id, dest)
        if not result.is_ok():
            self.post_message(ClipFailed(
                speaker_id=speaker_id, error=result.error_message(),
            ))
            return
        self.post_message(ClipReady(speaker_id=speaker_id, path=dest))

    @work(thread=True, exclusive=True, group="label-submit")
    def _submit_worker(self, labels: dict[str, str]) -> None:
        result = self.app.api.submit_labels(self.session_id, labels)
        if result.is_ok():
            self.post_message(SubmitDone(ok=True))
        else:
            self.post_message(SubmitDone(ok=False, error=result.error_message()))

    # ── messages ──

    def on_label_info_loaded(self, message: LabelInfoLoaded) -> None:
        self._info = message.info
        self._refresh_view()

    def on_label_load_failed(self, message: LabelLoadFailed) -> None:
        self.query_one("#status-line", Static).update(
            f"[red]Failed to load speakers: {message.error}[/red]",
        )

    def on_clip_ready(self, message: ClipReady) -> None:
        self._clip_paths[message.speaker_id] = message.path
        try:
            self._player.play(message.path)
        except FfplayNotFound as exc:
            self.notify(str(exc), severity="error")
        except Exception as exc:
            self.notify(f"Playback failed: {exc}", severity="error")

    def on_clip_failed(self, message: ClipFailed) -> None:
        self.notify(
            f"Could not fetch clip for {message.speaker_id}: {message.error}",
            severity="error",
        )

    def on_submit_done(self, message: SubmitDone) -> None:
        if message.ok:
            self.notify("Labels submitted", severity="information")
            self.app.pop_screen()
        else:
            self.notify(
                f"Submit failed: {message.error}", severity="error",
            )

    # ── buttons ──

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """PR11: pressing enter while typing a github handle submits.

        Matches the dialog convention 'enter = primary action'.  The
        Submit button is still present for mouse users + as an
        affordance hint to the user that there's a single action
        applied to all rows at once -- which the dogfood report
        confirmed was non-obvious.
        """
        # Only react if the Input is one of our speaker-label inputs;
        # ignore if it's something else (defensive, no other Inputs
        # currently on this screen but future-proof).
        if event.input.id and event.input.id.startswith("input-"):
            self._do_submit()

    def action_submit(self) -> None:
        """Submit all entered labels.  Wraps ``_do_submit`` so the
        action machinery (used by future keybindings or tests) has a
        stable entry point.
        """
        self._do_submit()

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
            if (self._player.is_playing
                    and self._player.current_path == self._clip_paths.get(speaker_id)):
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

    def _refresh_view(self) -> None:
        # See detail_screen.py:_refresh_view -- never name a screen helper
        # ``_render``; it shadows the Textual Widget API and crashes
        # rendering with ``AttributeError: NoneType has no attribute
        # render_strips`` on any real terminal display.
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
                # PR8: variant="primary" gives the button a colored
                # background (theme $primary) so it's clearly visible
                # even on terminals whose default ($surface) is near-
                # identical to the screen background -- earlier it
                # rendered as an indistinguishable black box.
                variant="primary",
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
