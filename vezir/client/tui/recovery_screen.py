"""Startup crash-recovery dialog (vezir 0.23.0).

Incident 2026-09-28: a TUI crash mid-recording left an 87-minute meeting
on disk with no trace in the UI — the Sessions tab is server-side only.
The user concluded the session was lost.  It wasn't: the chunk WAVs were
fully intact, but there was no discoverable path to them.

On TUI launch, ``install_recovery_scan`` scans the local recordings roots
(in a worker thread, off the UI path) for sessions that never reached the
server — interrupted recordings, orphaned recorders, unfinished uploads
(see ``vezir.client.recovery``) — and pushes this modal when any are
found.  One salvage action per session does the right thing for its
state: stop an orphaned recorder (SIGINT finalizes the WAV), stitch the
chunks, compress, upload with the journal tracking every step.

Disabled in tests via VEZIR_TUI_DISABLE_RECOVERY_SCAN=1.
"""
from __future__ import annotations

import logging

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import upload_journal
from ..recovery import RecoverableSession, recover, scan_interrupted, stop_orphaned

log = logging.getLogger("vezir.client.tui.recovery")

_MINUTE_BYTES = 64000 * 60  # pcm_s16le 16kHz stereo


def _fmt_size(nbytes: int) -> str:
    if nbytes >= 1024 * 1024 * 1024:
        return f"{nbytes / (1024 ** 3):.1f} GiB"
    if nbytes >= 1024 * 1024:
        return f"{nbytes / (1024 ** 2):.1f} MiB"
    return f"{nbytes / 1024:.1f} KiB"


def _describe(rec: RecoverableSession) -> str:
    size = _fmt_size(rec.total_bytes)
    if rec.kind == "pending_upload":
        state = rec.detail
    elif rec.kind == "orphaned":
        state = f"⚠ {rec.detail}"
    else:
        minutes = rec.total_bytes / _MINUTE_BYTES
        state = f"~{minutes:.0f} min, {rec.detail}"
    started = (rec.started_at or "")[:16].replace("T", " ")
    stamp = f"{started}  " if started else ""
    return f"{rec.session_dir.name}  [{rec.team_id}]  {stamp}{size} — {state}"


class RecoveryScreen(ModalScreen[None]):
    """Lists salvageable local sessions; uploads them on demand."""

    DEFAULT_CSS = """
    RecoveryScreen {
        align: center middle;
    }
    #recovery-box {
        width: 84%;
        height: auto;
        max-height: 85%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #recovery-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
    }
    #recovery-list {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }
    #recovery-title-input {
        margin-bottom: 1;
    }
    #recovery-status {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_all", "Close"),
        Binding("r", "salvage", "Salvage & upload"),
        Binding("o", "open_folder", "Open folder"),
    ]

    def __init__(self, sessions: list[RecoverableSession]) -> None:
        super().__init__()
        # (session, done) pairs; done rows stay visible but inert.
        self._rows: list[dict] = [
            {"rec": s, "done": False, "busy": False} for s in sessions
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="recovery-box"):
            yield Static(
                "Recovered recordings — never reached the server",
                id="recovery-title",
            )
            yield OptionList(
                *[Option(_describe(r["rec"]), id=str(i))
                  for i, r in enumerate(self._rows)],
                id="recovery-list",
            )
            yield Input(
                placeholder="Title for the upload (optional)",
                id="recovery-title-input",
            )
            yield Static(
                "r salvage & upload selected · o open folder · esc close",
                id="recovery-status",
            )
            with Horizontal(id="recovery-actions"):
                yield Button("⬆ Salvage & upload", id="recovery-go", variant="primary")
                yield Button("Open folder", id="recovery-open")
                yield Button("Close", id="recovery-close")

    def on_mount(self) -> None:
        try:
            self.query_one("#recovery-list", OptionList).focus()
        except Exception:
            pass
        self._prefill_title()

    # ── selection helpers ──

    def _selected_row(self) -> dict | None:
        try:
            idx = self.query_one("#recovery-list", OptionList).highlighted
        except Exception:
            return None
        if idx is None or not (0 <= idx < len(self._rows)):
            return None
        return self._rows[idx]

    def _prefill_title(self) -> None:
        row = self._selected_row()
        if not row:
            return
        hint = row["rec"].title_hint or ""
        try:
            self.query_one("#recovery-title-input", Input).value = hint
        except Exception:
            pass

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        self._prefill_title()

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#recovery-status", Static).update(text)
        except Exception:
            pass

    # ── actions ──

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "recovery-go":
            self.action_salvage()
        elif event.button.id == "recovery-open":
            self.action_open_folder()
        elif event.button.id == "recovery-close":
            self.dismiss(None)

    def action_dismiss_all(self) -> None:
        self.dismiss(None)

    def action_open_folder(self) -> None:
        row = self._selected_row()
        if not row:
            return
        import subprocess

        try:
            subprocess.Popen(
                ["xdg-open", str(row["rec"].session_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._set_status(f"could not open folder: {exc}")

    def action_salvage(self) -> None:
        row = self._selected_row()
        if not row or row["done"] or row["busy"]:
            return
        row["busy"] = True
        try:
            title = self.query_one("#recovery-title-input", Input).value.strip() or None
        except Exception:
            title = None
        self._salvage_worker(row, title)

    @work(thread=True, group="recovery-salvage")
    def _salvage_worker(self, row: dict, title: str | None) -> None:
        rec: RecoverableSession = row["rec"]
        try:
            # 1. Orphaned recorder still writing? Stop it (SIGINT
            #    finalizes the chunk WAV).
            if rec.kind == "orphaned":
                self.app.call_from_thread(
                    self._set_status, f"stopping recorder (pid {rec.recorder_pid})…"
                )
                if not stop_orphaned(rec):
                    raise RuntimeError(
                        f"recorder pid {rec.recorder_pid} did not exit; "
                        "stop it manually and retry"
                    )

            # 2. Stitch chunks (interrupted/orphaned) — pending uploads
            #    already have their final audio file.
            if rec.kind in ("interrupted", "orphaned"):
                self.app.call_from_thread(
                    self._set_status, f"stitching chunks in {rec.session_dir.name}…"
                )
                recover(rec)
            if rec.audio_path is None:
                rec.audio_path = upload_journal.find_audio(rec.session_dir)
            if rec.audio_path is None:
                raise RuntimeError("no uploadable audio file found")

            # 3. Journal + upload.
            upload_journal.mark_pending(
                rec.session_dir, title=title, team_id=rec.team_id
            )
            self.app.call_from_thread(self._set_status, "uploading…")
            session_id = self._upload(rec, title)
            upload_journal.mark_done(rec.session_dir, session_id or "")
            if session_id:
                try:
                    from ..pull import record_uploaded_session

                    record_uploaded_session(
                        rec.session_dir, session_id,
                        title=title, team_id=rec.team_id,
                    )
                except Exception as exc:
                    log.warning("could not write upload session.json: %s", exc)

            row["done"] = True
            rec_kind = rec.session_dir.name
            self.app.call_from_thread(
                self._set_status,
                f"✔ {rec_kind} uploaded as {session_id or '(no session id)'}",
            )
            self.app.call_from_thread(self._mark_row_done, row)
        except Exception as exc:
            upload_journal.mark_failed(rec.session_dir, str(exc))
            log.warning("salvage of %s failed: %s", rec.session_dir, exc)
            self.app.call_from_thread(
                self._set_status, f"✖ {rec.session_dir.name}: {exc}"
            )
        finally:
            row["busy"] = False

    def _upload(self, rec: RecoverableSession, title: str | None) -> str:
        """Compress (if WAV) + upload rec.audio_path. Returns session_id."""
        from .. import uploader

        audio_path = rec.audio_path
        assert audio_path is not None
        if audio_path.suffix.lower() == ".wav":
            self.app.call_from_thread(self._set_status, "compressing…")
            audio_path = uploader.compress_wav_for_upload(audio_path, keep_wav=False)
            rec.audio_path = audio_path

        server_url = self.app.server_url
        token = self.app.token or ""
        team_id = rec.team_id

        def refresh_cb() -> str | None:
            from ..api import refresh_active_session

            new = refresh_active_session(server_url, None)
            if new:
                self.app.token = new
                if getattr(self.app, "api", None) is not None:
                    self.app.api.token = new
            return new

        upload_journal.mark_uploading(rec.session_dir)
        kwargs = dict(
            title=title,
            auto_label=True,
            sync=True,
            personal=False,
            team_id=team_id,
            refresh_cb=refresh_cb,
        )
        if uploader.server_supports_resumable(server_url, token, team_id=team_id):
            result = uploader.upload_resumable(server_url, token, audio_path, **kwargs)
        else:
            result = uploader.upload(server_url, token, audio_path, **kwargs)
        return result.get("session_id", "")

    def _mark_row_done(self, row: dict) -> None:
        try:
            idx = self._rows.index(row)
            lst = self.query_one("#recovery-list", OptionList)
            lst.replace_option_prompt(
                str(idx), f"✔ {_describe(row['rec'])}"
            )
        except Exception:
            pass


def install_recovery_scan(host) -> None:
    """Scan recordings roots once at startup; push RecoveryScreen if needed.

    ``host`` is whatever on_mount called us (the MainScreen) — the scan
    worker hangs off it, but push_screen/call_from_thread belong to the
    App.  Runs in a worker thread (filesystem + /proc walk) so startup
    isn't blocked; the modal appears a beat after the main screen.
    """
    app = host.app

    def _scan() -> None:
        try:
            sessions = scan_interrupted()
        except Exception as exc:
            log.warning("recovery scan failed: %s", exc)
            return
        if not sessions:
            return
        log.info(
            "recovery scan found %d salvageable session(s): %s",
            len(sessions),
            [s.session_dir.name for s in sessions],
        )
        app.call_from_thread(app.push_screen, RecoveryScreen(sessions))

    host.run_worker(_scan, thread=True, name="recovery-scan")
