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

import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static

from ..api import LabelInfo
from ..audio import AudioPlayer, FfplayNotFound, ffplay_available

log = logging.getLogger("vezir.client.tui.label")

# Speaker IDs matching this regex are unresolved placeholders from the
# transcription engine.  Anything else has been resolved by auto-labeling
# and should be prefilled in the input widget.
_UNRESOLVED_RE = re.compile(r"^(YOU|REMOTE(_\d+)?|SPEAKER_\d+)$")


def _safe_clip_filename(speaker_id: str) -> str:
    """Path-safe ``.wav`` filename for a speaker id.

    Mirrors ``vezir.server.labels._safe_clip_filename`` so the client temp
    clip never breaks on names containing spaces or punctuation (e.g.
    "Juan Pablo").  The clip is keyed back to the real speaker id in
    ``_clip_paths``; only the on-disk filename is sanitized.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", speaker_id).strip("_") or "speaker"
    digest = hashlib.sha1(speaker_id.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}.wav"


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


@dataclass
class SegmentsLoaded(Message):
    speaker_id: str
    body: dict


@dataclass
class SegmentsFailed(Message):
    speaker_id: str
    error: str


def _fmt_ts(seconds: float) -> str:
    """mm:ss timestamp for the segments modal."""
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


class SegmentsScreen(ModalScreen[None]):
    """Modal: all transcript segments for one speaker (labeling aid).

    Opened by the "More" button on a speaker row when the single sample
    line is not enough to identify the speaker.  Esc closes.
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    CSS = """
    SegmentsScreen { align: center middle; }
    #segments-box {
        width: 80%;
        max-width: 100;
        height: 70%;
        max-height: 90%;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }
    #segments-body { height: 1fr; overflow-y: auto; }
    """

    def __init__(self, speaker_id: str, body: dict | None = None) -> None:
        super().__init__()
        self._speaker_id = speaker_id
        self._body = body

    def compose(self) -> ComposeResult:
        from textual.widgets import RichLog

        with Vertical(id="segments-box"):
            yield Label(f"[b]{self._speaker_id}[/b] — all segments")
            log_widget = RichLog(id="segments-body", markup=False, wrap=True)
            yield log_widget
            yield Label("[dim]Esc to close[/dim]")

    def on_mount(self) -> None:
        from textual.widgets import RichLog

        log_widget = self.query_one("#segments-body", RichLog)
        if self._body is None:
            log_widget.write("loading…")
            return
        segs = self._body.get("segments") or []
        total = self._body.get("total", len(segs))
        for seg in segs:
            log_widget.write(
                f"[{_fmt_ts(seg.get('start', 0.0))}]  {seg.get('text', '')}"
            )
        if total > len(segs):
            log_widget.write(f"\n… ({total - len(segs)} more segments not shown)")

    def set_body(self, body: dict) -> None:
        """Replace the loading placeholder once segments arrive."""
        self._body = body
        from textual.widgets import RichLog

        log_widget = self.query_one("#segments-body", RichLog)
        log_widget.clear()
        self.on_mount()


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
    .more-btn { min-width: 10; margin: 0 0 0 1; }
    .name-input { width: 32; }
    #actions { height: 3; margin-top: 1; }
    """

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id
        self._info: LabelInfo | None = None
        # speaker_id -> Input widget
        self._inputs: dict[str, Input] = {}
        # row-index token -> speaker_id.  Widget ids are built from the row
        # index (always a valid Textual identifier) rather than the raw
        # speaker id, which may now contain spaces (e.g. "Juan Pablo") after
        # voiceprint auto-labeling.  This map recovers the real id on click.
        self._row_sid: dict[str, str] = {}
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
        dest = self._tmpdir / _safe_clip_filename(speaker_id)
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

    # ── segments modal ──

    def _open_segments(self, speaker_id: str) -> None:
        screen = SegmentsScreen(speaker_id)
        self._segments_screen = screen
        self.app.push_screen(screen)
        self._segments_worker(speaker_id)

    @work(thread=True, exclusive=False, group="label-segments")
    def _segments_worker(self, speaker_id: str) -> None:
        result = self.app.api.get_speaker_segments(self.session_id, speaker_id)
        if result.is_ok():
            self.post_message(SegmentsLoaded(speaker_id=speaker_id, body=result.ok))
        else:
            self.post_message(SegmentsFailed(
                speaker_id=speaker_id, error=result.error_message(),
            ))

    def on_segments_loaded(self, message: SegmentsLoaded) -> None:
        screen = getattr(self, "_segments_screen", None)
        if screen is not None and screen._speaker_id == message.speaker_id:
            screen.set_body(message.body)

    def on_segments_failed(self, message: SegmentsFailed) -> None:
        self.notify(
            f"Could not load segments for {message.speaker_id}: {message.error}",
            severity="error",
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
        # "More" buttons have id "more-<row-index>".
        if bid and bid.startswith("more-"):
            speaker_id = self._row_sid.get(bid[len("more-"):])
            if speaker_id is not None:
                self._open_segments(speaker_id)
            return
        # Play buttons have id "play-<row-index>"; recover the real speaker
        # id (which may contain spaces) from the row map.
        if bid and bid.startswith("play-"):
            speaker_id = self._row_sid.get(bid[len("play-"):])
            if speaker_id is None:
                return
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
        self._row_sid.clear()

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

        for idx, sp in enumerate(info.speakers):
            sid = str(sp.get("id", "?"))
            # Widget ids are built from the row index (always a valid Textual
            # identifier).  The raw sid may contain spaces after auto-labeling
            # (e.g. "Juan Pablo"), which Textual rejects as a widget id.
            tok = str(idx)
            self._row_sid[tok] = sid
            sample = (sp.get("sample_text") or "")[:80]
            suggested = sp.get("suggested_name")
            confidence = sp.get("confidence")
            row = Horizontal(classes="speaker-row")
            container.mount(row)
            # Speaker id, annotated with auto-id confidence when available.
            id_label = sid
            if suggested and confidence is not None:
                id_label = f"{sid} [dim]({round(confidence * 100)}%)[/dim]"
            row.mount(Label(id_label, classes="speaker-id"))
            row.mount(Label(sample, classes="sample"))
            # "More" opens a modal with ALL of the speaker's segments —
            # the single 120-char sample is often not enough to identify
            # a speaker.  Lazy: fetched on demand from the server.
            more_btn = Button(
                "▤ More",
                id=f"more-{tok}",
                classes="more-btn",
            )
            row.mount(more_btn)
            play_btn = Button(
                "▶ Play",
                id=f"play-{tok}",
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
            # Prefill the name input from the best available source:
            #   1. Already-resolved speaker id (auto-labeling renamed
            #      e.g. "REMOTE_1" -> "Pedro" in the transcript), OR
            #   2. A voiceprint auto-id suggestion (sidecar), which lets us
            #      pre-fill recognized names even when the transcript id is
            #      still a raw placeholder.
            # Unresolved speakers with no suggestion start empty.
            resolved = not _UNRESOLVED_RE.match(sid)
            if resolved:
                prefill = sid
            elif suggested:
                prefill = suggested
            else:
                prefill = ""
            inp = Input(
                value=prefill,
                placeholder=sid if not prefill else "name",
                id=f"input-{tok}",
                classes="name-input",
            )
            try:
                from textual.suggester import SuggestFromList
                inp.suggester = SuggestFromList(info.team, case_sensitive=False)
            except Exception:  # pragma: no cover - older textual
                pass
            self._inputs[sid] = inp
            row.mount(inp)
