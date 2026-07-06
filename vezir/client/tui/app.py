"""Top-level Textual app for the vezir desktop thin client.

Architecture:

* ``VezirTuiApp`` owns one ``VezirClient`` instance (constructed from
  ``VEZIR_URL`` / ``VEZIR_TOKEN`` or the persisted client.json) and
  passes it via ``self.app.api`` so every screen reads the same auth
  state.  v0.6.1+: when ``~/.config/vezir/teams.json`` exists with an
  active entry, those credentials win over env+client.json — see
  :func:`vezir.client.config.resolve_credentials`.
* A single root ``MainScreen`` wraps the two top-level views
  (RecordScreen, SessionsScreen) inside a ``TabbedContent``.  This is
  the Textual-idiomatic shape for "bottom-nav" UIs and dodges the
  switch_screen / install_screen state-tracking edge cases.
* Transient screens (DetailScreen, ArtifactScreen, LabelScreen,
  HelpScreen) are pushed on top of MainScreen and pop themselves via
  ``escape`` -- standard Textual screen stack semantics.
* Heavyweight imports (millet-record, textual widgets that pull
  rich extras) are lazy inside the screen modules so a `vezir tui`
  startup on a box with the bare-minimum install still gives a
  legible error message before falling over.

Global bindings (priority on the App):
  ctrl+r  Record tab
  ctrl+s  Sessions tab
  ctrl+l  Refresh
  ctrl+q  Quit
  ctrl+t  Cycle active team (v0.6.1+, requires teams.json)
  f1 / ?  Help
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, TabbedContent, TabPane

from ..api import VezirClient
from ..config import (
    load_teams_config,
    resolve_credentials,
    set_active_team,
)

if TYPE_CHECKING:
    # Forward-only reference: used purely as a string annotation in the
    # message handler below.  Importing it eagerly would create a
    # circular import (record_screen imports from this module too).
    from .record_screen import SessionUploadComplete

log = logging.getLogger("vezir.client.tui")


def _resolve_credentials() -> tuple[str, str | None, str | None, str]:
    """Resolve server URL + token + team_id: env > teams.json > client.json > defaults.

    Returns ``(url, token, team_id, source)`` where ``source`` is one
    of ``"env"``, ``"teams:<id>"``, ``"client"``, or ``"default"``.
    Wraps :func:`vezir.client.config.resolve_credentials` with a
    ``localhost`` fallback so the TUI can still mount on a fresh
    machine with no config (will get 401/400 on the first API call
    but won't crash).
    """
    url, token, team_id, source = resolve_credentials()
    if not url:
        url = "http://localhost:8000"
        source = source or "default"
    return url, token, team_id, source


# ─── MainScreen: tabbed root holding the two top-level views ────────────────


class MainScreen(Screen):
    """Single root screen with Record / Sessions tabs."""

    BINDINGS = [
        Binding("ctrl+r", "show_tab('record')", "Record"),
        Binding("ctrl+s", "show_tab('sessions')", "Sessions"),
        Binding("ctrl+e", "show_tab('teams')", "Teams"),
        Binding("ctrl+l", "refresh_current", "Refresh", show=False),
    ]

    CSS = """
    MainScreen TabbedContent { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        # Lazy imports so `vezir --help` stays snappy on minimal installs.
        from .record_screen import RecordBody
        from .sessions_screen import SessionsBody
        from .teams_screen import TeamsBody

        yield Header(show_clock=True)
        with TabbedContent(id="main-tabs"):
            with TabPane("Record", id="record"):
                yield RecordBody.body_widget()
            with TabPane("Sessions", id="sessions"):
                yield SessionsBody.body_widget()
            with TabPane("Teams", id="teams"):
                yield TeamsBody.body_widget()
        yield Footer()

    def on_mount(self) -> None:
        # Start the background labeling-needed poll.  Skipped under
        # test (VEZIR_TUI_DISABLE_NOTIFY_POLL=1) so unrelated tests
        # don't accumulate timers that fire after teardown.
        if os.environ.get("VEZIR_TUI_DISABLE_NOTIFY_POLL") == "1":
            return
        try:
            from .notify import install_labeling_poll
            install_labeling_poll(self)
        except Exception as exc:
            log.warning("labeling poll setup failed: %s", exc)

    def action_show_tab(self, tab_id: str) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = tab_id

    def action_refresh_current(self) -> None:
        """Forward refresh to whichever tab's body widget exposes it."""
        from .record_screen import RecordBody
        from .sessions_screen import SessionsBody
        from .teams_screen import TeamsBody

        tabs = self.query_one(TabbedContent)
        active = tabs.active_pane
        if active is None:
            return
        try:
            body = active.query_one((RecordBody, SessionsBody, TeamsBody))
        except Exception:
            return
        action = getattr(body, "action_refresh", None)
        if callable(action):
            action()

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Refresh the Sessions tab whenever the user switches to it.

        PR9 (2026-05-24): the dogfood report was that a session
        recorded in the TUI didn't appear in the Sessions list until
        the TUI was restarted.  Root cause: SessionsBody refreshed
        only on its own ``on_mount``, never reacting to tab changes.

        Cost is one /api/sessions roundtrip per activation (≈50ms
        over LAN); imperceptible.  No debounce: users don't tab-
        flick fast enough for it to matter.
        """
        from .sessions_screen import SessionsBody
        from .teams_screen import TeamsBody
        if event.pane.id == "sessions":
            cls: type = SessionsBody
        elif event.pane.id == "teams":
            # v0.7.6: re-fetch /api/me memberships so the Teams tab
            # reflects any server-side membership changes.
            cls = TeamsBody
        else:
            return
        try:
            body = event.pane.query_one(cls)
        except Exception:
            return
        body.action_refresh()

    def on_session_upload_complete(
        self, message: SessionUploadComplete
    ) -> None:
        """Refresh the Sessions tab + toast the user when a freshly
        uploaded session reaches terminal status on the server.

        Posted from RecordBody._poll_worker; bubbles up to here.
        """
        from .sessions_screen import SessionsBody
        # Refresh whether or not the Sessions tab is currently active
        # -- the user may switch to it any moment, and SessionsBody
        # caches its rendered rows.  Refresh is cheap.
        try:
            body = self.query_one(SessionsBody)
            body.action_refresh()
        except Exception:
            pass

        short = (message.session_id or "")[:8]
        if message.status == "done":
            self.notify(
                f"Session {short}… is ready",
                severity="information",
                timeout=4,
            )
        elif message.status == "needs_labeling":
            self.notify(
                f"Session {short}… needs labeling",
                severity="warning",
                timeout=6,
            )
        elif message.status == "error":
            self.notify(
                f"Session {short}… errored",
                severity="error",
                timeout=8,
            )


# ─── App ─────────────────────────────────────────────────────────────────────


class VezirTuiApp(App):
    """Top-level Textual app."""

    CSS = """
    Screen { layout: vertical; }
    .error { color: $error; }
    .ok { color: $success; }
    .muted { color: $text-muted; }
    .key { color: $accent; text-style: bold; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        # Emergency hard-exit.  priority=True so it fires regardless of
        # which widget has focus.  Originally bound to ctrl+c in PR4,
        # but that shadowed TextArea's built-in ctrl+c-copy-selection
        # AND Textual's own selection-aware ctrl+c convention (a
        # priority-True ctrl+c eats every native copy path the
        # framework provides).  Moved to ctrl+shift+q -- three-key
        # chord, can't collide with anything common, and intuitively
        # reads as "quit, no really".  PR6.
        Binding("ctrl+shift+q", "force_quit", "Force quit", priority=True, show=False),
        # Selection-aware copy.  Textual's Screen exposes
        # ``action_copy_text`` which reads the current cross-widget
        # selection (populated as you mouse-drag or shift+arrow-key
        # through text) and writes it to the clipboard via OSC 52.
        # ctrl+shift+c is the conventional terminal "copy" keystroke
        # so it composes naturally with the user's muscle memory.
        Binding("ctrl+shift+c", "screen.copy_text", "Copy selection", show=False),
        # v0.6.1: ^t cycles through teams configured in
        # ~/.config/vezir/teams.json.  Hidden from the footer (not
        # show=True) when no teams.json or only one team is configured,
        # because dynamic-show isn't supported by Textual's Footer.
        # Always-shown is the right tradeoff: discoverable when present,
        # one extra footer cell when not (cheap).
        Binding("ctrl+t", "switch_team", "Switch team"),
        Binding("f1", "help", "Help"),
        Binding("question_mark", "help", show=False),
    ]

    TITLE = "vezir"
    SUB_TITLE = "thin client"

    def __init__(self) -> None:
        super().__init__()
        (
            self.server_url,
            self.token,
            self.active_team_id,
            self.cred_source,
        ) = _resolve_credentials()
        # active_team_id may already be set from VEZIR_TEAM_ID or
        # teams.json; fall back to parsing the source string for the
        # legacy "teams:<id>" pattern.
        if not self.active_team_id and self.cred_source.startswith("teams:"):
            self.active_team_id = self.cred_source.split(":", 1)[1]
        # team_label is the human display name (resolved via /api/me's
        # memberships list).  Filled in by _refresh_identity() shortly
        # after mount.  Default "?" shows up in the title bar until the
        # network round-trip completes.
        self.team_label: str = "?"
        # v0.7.6: memberships discovered from GET /api/me, cached so the
        # Teams tab and ^t can show every team the user belongs to —
        # not just the ones manually added to teams.json.  Each entry is
        # ``{team_id(uuid), slug, role, team_name}``.  Filled by
        # _refresh_identity().
        self.memberships: list[dict] = []
        # In-memory active-team override for teams DISCOVERED via /api/me
        # that aren't in teams.json.  set_active_team() only persists
        # config-backed teams; discovered selections live here for the
        # session (mirrors the android UX: server is the source of truth).
        self._discovered_active: str | None = None
        if not self.token:
            log.warning("VEZIR_TOKEN is not set; TUI will run in degraded mode")
        self.api = VezirClient(
            self.server_url,
            self.token or "vzr_unset",  # placeholder; server will 401
            team_id=self.active_team_id,
            on_token_refreshed=self._on_token_refreshed,
        )

    def _on_token_refreshed(self, new_token: str) -> None:
        """Keep ``self.token`` in sync when the client silently refreshes.

        The uploader and other snapshot readers use ``app.token``; without
        this, a token rotated by the polling path would live only inside
        ``self.api`` and the upload path would keep using a stale token.
        """
        self.token = new_token

    def on_mount(self) -> None:
        self.push_screen(MainScreen())
        # Fetch identity asynchronously so the title bar gets the real
        # team name once it lands.  Failure is silent (the title-bar
        # placeholder stays "?"; users can `vezir --version` or run
        # the CLI's `vezir status` to debug).
        self._refresh_identity()

    # ── identity / team-switching ──

    def _refresh_identity(self) -> None:
        """Pull team_name from GET /api/me and update the title bar.

        Called once at mount and after every successful team switch.
        Server-side endpoint added in v0.6.1; against an older server
        the call returns 404 and we just leave the placeholder.

        Runs synchronously (one quick HTTP call); could move to a
        worker if it ever shows up in profiling, but a single ~50ms
        roundtrip at startup is invisible.
        """
        try:
            import httpx
            verify = self.api._resolve_verify(None)
            r = httpx.get(
                self.server_url.rstrip("/") + "/api/me",
                headers={"Authorization": f"Bearer {self.token or ''}"},
                timeout=5.0,
                verify=verify,
            )
            if r.status_code == 200:
                data = r.json()
                # v0.7.0: /api/me now returns a memberships list.
                # Pick the entry matching our active_team_id; if none
                # set, fall back to the first membership (and remember
                # it so X-Team-Id starts working).
                mems = data.get("memberships") or []
                # v0.7.6: cache the full membership list for the Teams
                # tab + ^t merged cycle.
                self.memberships = list(mems)
                # v0.7.4 migration made the server key memberships by
                # team UUID (``team_id``) while the client still
                # configures teams by slug in teams.json.  Match on
                # EITHER so a slug-configured active team resolves
                # against the UUID-keyed membership list.  Each
                # membership also carries ``slug`` (v0.7.4+).
                matched = None
                if self.active_team_id:
                    matched = next(
                        (m for m in mems
                         if m.get("team_id") == self.active_team_id
                         or m.get("slug") == self.active_team_id),
                        None,
                    )
                if matched is None and mems:
                    matched = mems[0]
                    # Adopt the discovered team but keep active_team_id
                    # as the SLUG (not the UUID) so it stays consistent
                    # with teams.json and next_team_id()'s cycle list.
                    # The server resolves a slug in X-Team-Id to its
                    # UUID, so slug is a valid X-Team-Id value.
                    self.active_team_id = (
                        matched.get("slug") or matched.get("team_id")
                    )
                    # Re-bind the client with the discovered team
                    # so subsequent requests carry X-Team-Id.
                    self.api = VezirClient(
                        self.server_url,
                        self.token or "vzr_unset",
                        team_id=self.active_team_id,
                        on_token_refreshed=self._on_token_refreshed,
                    )
                if matched:
                    self.team_label = (
                        matched.get("team_name")
                        or matched.get("team_id")
                        or "?"
                    )
            else:
                log.info(
                    "GET /api/me returned %s; leaving team label as '?'",
                    r.status_code,
                )
        except Exception as exc:
            log.info("GET /api/me failed (%s); leaving team label as '?'", exc)
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        """Refresh the title bar TITLE to reflect the active team.

        SUB_TITLE is owned by the active screen (SessionsBody sets
        "sessions (N)", DetailScreen sets "session <id>", etc.).  To
        avoid stomping that surface, the team label lives in the main
        TITLE: "vezir — team: Blink".  Screens leave TITLE alone, so
        the team display sticks.
        """
        if self.team_label and self.team_label != "?":
            self.title = f"vezir — team: {self.team_label}"
        else:
            self.title = "vezir"

    def all_teams(self) -> list[dict]:
        """Merge teams.json entries with /api/me memberships (v0.7.6).

        Returns a deduplicated, slug-ordered list of teams the user can
        switch to.  Each item:

            {"slug": str, "label": str, "role": str | None,
             "source": "config" | "discovered",
             "url": str, "token": str}

        teams.json entries (which may target a DIFFERENT server/token)
        take precedence on collision; discovered-only memberships
        inherit the CURRENT server_url + token (one token authorizes
        every team the github user belongs to — team scope is the
        per-request X-Team-Id header, resolved slug-or-uuid server-side).
        """
        merged: dict[str, dict] = {}
        # Discovered memberships first (lower precedence).
        for m in self.memberships:
            slug = m.get("slug") or m.get("team_id")
            if not slug:
                continue
            merged[slug] = {
                "slug": slug,
                "label": m.get("team_name") or slug,
                "role": m.get("role"),
                "source": "discovered",
                "url": self.server_url,
                "token": self.token or "",
            }
        # teams.json entries override (explicit multi-server/token case).
        for t in load_teams_config()["teams"]:
            slug = t["id"]
            merged[slug] = {
                "slug": slug,
                "label": t.get("label") or slug,
                "role": merged.get(slug, {}).get("role"),
                "source": "config",
                "url": t.get("url", self.server_url),
                "token": t.get("token", self.token or ""),
            }
        return [merged[k] for k in sorted(merged)]

    def team_slug_for(self, team_id: str | None) -> str | None:
        """Resolve a team id (UUID or slug) to its human slug.

        The server identifies teams by UUID (so ``Session.team_id`` is a
        UUID), but recordings live under the team **slug** on disk
        (``~/vezir-meetings/<slug>/``).  This maps a UUID to its slug via
        the cached ``/api/me`` memberships so local-folder lookups hit the
        right directory.  Returns the input unchanged if it already looks
        like a slug (or no membership matches).
        """
        if not team_id:
            return team_id
        for m in self.memberships:
            if m.get("team_id") == team_id:
                return m.get("slug") or team_id
            if m.get("slug") == team_id:
                return team_id
        return team_id

    def _inflight_blocks_switch(self) -> bool:
        """True if a recording/upload is in flight (mid-flight switching
        would orphan the upload on the old team's server view)."""
        try:
            from .record_screen import RecordBody
            body = self.query_one(RecordBody)
            return bool(body.is_recording or body.is_uploading)
        except Exception:
            return False  # Record screen not mounted; nothing to guard.

    def switch_to_team(self, slug: str) -> bool:
        """Switch the active team to ``slug`` (token-preserving).

        Works for both teams.json-configured teams and teams discovered
        via /api/me.  For config teams the selection persists via
        set_active_team(); for discovered-only teams it lives in-memory
        for the session (mirrors android: the server is the source of
        truth, no redundant teams.json writes).

        Returns True on success.  Refuses (returns False) while a
        recording/upload is in flight.
        """
        if self._inflight_blocks_switch():
            self.notify(
                "Cannot switch teams while recording or uploading.",
                severity="error",
                timeout=5,
            )
            return False
        if slug == self.active_team_id:
            return True  # already active; no-op

        entry = next((t for t in self.all_teams() if t["slug"] == slug), None)
        if entry is None:
            self.notify(f"Unknown team: {slug}", severity="error", timeout=5)
            return False

        # Persist to teams.json only when the team is config-backed.
        if entry["source"] == "config":
            try:
                set_active_team(slug)
            except ValueError as exc:
                self.notify(str(exc), severity="error", timeout=5)
                return False
            self.cred_source = f"teams:{slug}"
            self._discovered_active = None
        else:
            # Discovered-only: remember in-memory for this session.
            self._discovered_active = slug
            self.cred_source = f"discovered:{slug}"

        self.server_url = entry["url"] or self.server_url
        self.token = entry["token"] or self.token
        self.active_team_id = slug
        self.team_label = "?"  # cleared until /api/me round-trips
        self.api = VezirClient(
            self.server_url,
            self.token or "vzr_unset",
            team_id=self.active_team_id,
            on_token_refreshed=self._on_token_refreshed,
        )
        self._refresh_identity()

        # Force the Sessions tab to reload against the new team.
        try:
            from .sessions_screen import SessionsBody
            body = self.query_one(SessionsBody)
            body.action_refresh()
        except Exception:
            pass
        # Refresh the Teams tab's active marker if it's mounted.
        try:
            from .teams_screen import TeamsBody
            self.query_one(TeamsBody).refresh_active_marker()
        except Exception:
            pass

        self.notify(
            "Switched to team: "
            f"{self.team_label if self.team_label != '?' else slug}",
            severity="information",
            timeout=4,
        )
        return True

    def apply_reauth_session(self, body: dict) -> None:
        """Persist + re-bind a fresh session JWT after in-TUI re-auth (0.8.9).

        Called by the re-auth modal's callback on success.  Stores the new JWT
        for the active team in teams.json and re-binds the in-memory token +
        API client so the running TUI uses it immediately — no restart.
        """
        # 0.10.1: prefer the short-lived access token; fall back to
        # session_jwt for a pre-refresh server.
        jwt = body.get("access_jwt") or body.get("session_jwt")
        if not jwt:
            raise ValueError("re-auth body missing session_jwt")
        team = self.active_team_id or ""
        # Persist for config-backed teams (best-effort; discovered-only teams
        # still get the in-memory rebind below).
        if team:
            try:
                import time as _time

                from ..config import set_team_session
                exp_in = body.get("expires_in")
                exp_at = (
                    _time.time() + int(exp_in) if exp_in else None
                )
                # Persist the rotating refresh token too, so silent refresh
                # works after an in-TUI re-auth (previously dropped, forcing
                # another manual login an hour later).
                r_exp_in = body.get("refresh_expires_in")
                r_exp_at = (
                    _time.time() + int(r_exp_in) if r_exp_in else None
                )
                set_team_session(
                    team, self.server_url, jwt,
                    body.get("npub", body.get("email", "")), label=team,
                    expires_at=exp_at,
                    refresh_token=body.get("refresh_token"),
                    refresh_expires_at=r_exp_at,
                )
            except Exception:
                log.warning("could not persist re-auth session", exc_info=True)
        # Re-bind in memory.
        self.token = jwt
        self.api = VezirClient(
            self.server_url,
            self.token,
            team_id=self.active_team_id,
            on_token_refreshed=self._on_token_refreshed,
        )
        self._refresh_identity()

    def action_switch_team(self) -> None:
        """Cycle ^t to the next team in the MERGED list (v0.7.6).

        The cycle covers every team the user belongs to — teams.json
        entries AND memberships discovered from /api/me — so switching
        no longer requires manually adding each team to teams.json.
        """
        teams = self.all_teams()
        if not teams:
            self.notify(
                "No teams available.  The server's /api/me returned no "
                "memberships and teams.json is empty.",
                severity="warning",
                timeout=6,
            )
            return
        if len(teams) == 1:
            self.notify(
                f"Only one team available ({teams[0]['slug']}).",
                severity="information",
                timeout=4,
            )
            return

        slugs = [t["slug"] for t in teams]
        if self.active_team_id in slugs:
            idx = slugs.index(self.active_team_id)
            nxt = slugs[(idx + 1) % len(slugs)]
        else:
            nxt = slugs[0]
        if nxt != self.active_team_id:
            self.switch_to_team(nxt)

    # ── global actions ──

    def action_help(self) -> None:
        from .help_screen import HelpScreen
        self.push_screen(HelpScreen())

    def action_force_quit(self) -> None:
        """Emergency hard-exit invoked by ctrl+shift+q.

        Logs the event so post-mortem analysis can correlate a hung
        screen with the user's escape moment, then calls App.exit()
        which restores terminal state on its way out.
        """
        log.warning("force_quit invoked (ctrl+shift+q)")
        self.exit()

    # ── clipboard ──

    def copy_to_clipboard(self, text: str) -> None:
        """Dual-write clipboard: OSC 52 + system clipboard utility.

        Textual's default ``copy_to_clipboard`` writes only OSC 52 (an
        escape sequence the terminal emulator interprets and forwards
        to the system clipboard).  OSC 52 works in Ghostty, kitty,
        WezTerm, alacritty, iTerm2, modern xterm -- but is DISABLED
        BY DEFAULT in gnome-terminal / VTE-based terminals (for
        security: any program writing to stdout could otherwise
        silently exfil to the clipboard).

        We additionally shell out to a discovered clipboard utility
        (wl-copy / xclip / pbcopy) so the clipboard actually gets
        populated on those terminals.  Both writes target the same
        OS clipboard slot, so there's no conflict -- whichever path
        the terminal honors wins, the other is a benign no-op.

        Failure modes:
          * empty payload -> skip subprocess (xclip with empty stdin
            would clear the clipboard)
          * no utility found -> silent; OSC 52 may still have worked
          * subprocess hangs / errors -> swallowed at debug; the
            caller already showed a toast and OSC 52 may have worked
        """
        super().copy_to_clipboard(text)  # OSC 52 via Textual driver
        if not text:
            return
        cmd = self._discover_clipboard_cmd()
        if cmd is None:
            return
        import subprocess
        try:
            subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                timeout=2,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.debug("clipboard subprocess failed: %s", exc)

    def _discover_clipboard_cmd(self) -> list[str] | None:
        """Pick a working clipboard write utility (or None).

        Resolution order, first hit wins:
          1. wl-copy   (Wayland; only when WAYLAND_DISPLAY is set --
                        wl-copy raises if no compositor is running)
          2. xclip     (X11; primary Linux deployment target)
          3. pbcopy    (macOS; built in)

        Cached on the app instance after the first call so we don't
        re-probe shutil.which for every copy operation.
        """
        cached = getattr(self, "_clipboard_cmd_cache", "unset")
        if cached != "unset":
            return cached
        import shutil as _sh

        cmd: list[str] | None = None
        if os.environ.get("WAYLAND_DISPLAY") and _sh.which("wl-copy"):
            cmd = ["wl-copy"]
        elif _sh.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        elif _sh.which("pbcopy"):
            cmd = ["pbcopy"]
        self._clipboard_cmd_cache = cmd
        return cmd

    # ── exception handling ──

    def _handle_exception(self, error: Exception) -> None:
        """Catch unhandled exceptions so a single bug doesn't crash the TUI.

        Textual calls ``App._handle_exception`` from its message-pump and
        worker harness for uncaught errors.  Default behavior is to print
        a Rich traceback and exit; we override to log the error and show
        a transient notification so the user can keep working.  Errors
        in the *render* pipeline still crash (the screen's compositor
        path can't be recovered mid-flight), but everything else --
        worker thread exceptions, action callbacks, message handlers --
        stays survivable.

        Test-only escape hatch: set ``VEZIR_TUI_CRASH_ON_ERROR=1`` to
        restore the default fail-fast behavior; the test suite uses
        this to ensure regressions surface rather than getting hidden.
        """
        if os.environ.get("VEZIR_TUI_CRASH_ON_ERROR") == "1":
            super()._handle_exception(error)
            return
        log.exception("uncaught TUI exception: %s", error)
        try:
            self.notify(
                f"Internal error: {error}",
                severity="error",
                timeout=10,
            )
        except Exception:
            # Notification itself failed -- last-resort fallback to
            # the default traceback so the user at least sees something.
            super()._handle_exception(error)
