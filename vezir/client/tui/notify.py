"""Background polling for sessions that need labeling.

Mirrors vezir-android's ``net/LabelCheckWorker.kt`` (WorkManager-based
periodic check that fires a notification when one of the user's
sessions transitions to ``status=needs_labeling``).  Implementation
here uses Textual's ``set_interval`` so the poll cooperates with the
event loop without spawning real OS timers.

The poll runs from ``MainScreen.on_mount`` (one timer per app launch).
It's a no-op when the TUI is not the foreground app -- the user is
already looking at the screen if they want to act, the value is the
nudge when they're focused elsewhere.

Notifications use ``vezir.client.audio.notify_desktop`` (notify-send
on Linux, osascript on macOS, silent no-op elsewhere).  In addition
the TUI surfaces an in-app ``app.notify(...)`` toast so the user sees
the alert regardless of whether desktop notifications are available.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from textual import work

from ..api import Session, VezirClient
from ..audio import notify_desktop

log = logging.getLogger("vezir.client.tui.notify")


# Default poll interval (seconds).  60s balances responsiveness against
# server load -- typical labeling latency is several minutes per session
# anyway, so faster polling has no real benefit.
DEFAULT_POLL_INTERVAL = 60.0


@dataclass
class LabelingState:
    """Per-app state for the labeling poll.

    Tracks which session ids have already fired a notification so we
    don't spam the user on every poll while a session sits in
    ``needs_labeling``.  The set is reset on app restart by design --
    re-notifying after a restart is a feature (user may have missed
    the original alert).
    """

    seen_needs_labeling: set[str]

    @classmethod
    def new(cls) -> "LabelingState":
        return cls(seen_needs_labeling=set())


def filter_my_needs_labeling(
    sessions: list[Session],
    my_github: str | None,
) -> list[Session]:
    """Return sessions that need labeling AND were uploaded by the current user.

    Other users' needs-labeling sessions are excluded because they're
    not actionable from this client (the uploader has the audio
    context).  Personal sessions of other users are already filtered
    server-side by ``GET /api/sessions``.
    """
    out = []
    for s in sessions:
        if s.status != "needs_labeling":
            continue
        if my_github and s.github and s.github != my_github:
            continue
        out.append(s)
    return out


def find_new_alerts(
    sessions: list[Session],
    state: LabelingState,
) -> list[Session]:
    """Return sessions that newly entered needs_labeling since last poll.

    Mutates ``state.seen_needs_labeling`` to record the alerts we're
    returning so the next poll won't re-notify.  Also drops ids that
    have moved out of needs_labeling so the same session can re-alert
    if it bounces back (rare but possible via retry-summary).
    """
    current_ids = {s.id for s in sessions}
    # Forget sessions that left needs_labeling so they can re-alert
    # if they come back later.
    state.seen_needs_labeling &= current_ids
    new = [s for s in sessions if s.id not in state.seen_needs_labeling]
    state.seen_needs_labeling.update(s.id for s in new)
    return new


def format_alert(session: Session) -> tuple[str, str]:
    """Format a (title, body) pair for the desktop notifier."""
    title = "vezir: session needs labeling"
    body_label = session.title or session.id
    body = f"{body_label}\nOpen vezir tui -> Sessions to label speakers."
    return title, body


# ─── Textual integration ─────────────────────────────────────────────────────


def install_labeling_poll(
    screen,
    *,
    interval: float = DEFAULT_POLL_INTERVAL,
) -> LabelingState:
    """Wire a background poll into a screen's lifecycle.

    Call from ``MainScreen.on_mount``.  Returns the ``LabelingState``
    so the caller can attach it to the screen for testing.  The poll
    runs as a Textual ``set_interval`` timer (cooperative with the
    event loop) and uses an exclusive thread worker to fetch sessions
    so the network call doesn't block UI updates.
    """
    state = LabelingState.new()

    def on_tick() -> None:
        _labeling_poll_worker(screen, state)

    # First poll runs after the first interval, NOT immediately, so app
    # startup doesn't get a spurious notification while the user is
    # already looking at the session list.
    screen.set_interval(interval, on_tick, name="labeling-poll")
    return state


@work(thread=True, exclusive=True, group="labeling-poll")
def _labeling_poll_worker(screen, state: LabelingState) -> None:
    """Worker body: fetch sessions, find new alerts, notify."""
    app = screen.app
    client: VezirClient = app.api
    result = client.get_sessions(limit=20)
    if not result.is_ok():
        log.debug("labeling poll: get_sessions failed: %s", result.error_message())
        return

    my_github = _resolve_my_github(client)
    sessions = filter_my_needs_labeling(result.ok, my_github)
    new = find_new_alerts(sessions, state)
    if not new:
        return
    for s in new:
        title, body = format_alert(s)
        # Best-effort desktop notification (silent no-op if no notifier).
        notify_desktop(title, body)
        # In-app toast in case the desktop notification was missed.
        try:
            screen.app.call_from_thread(
                screen.app.notify,
                body,
                title=title,
                severity="warning",
                timeout=10,
            )
        except Exception as exc:
            log.debug("in-app notify failed: %s", exc)


def _resolve_my_github(client: VezirClient) -> str | None:
    """Best-effort lookup of the bearer's github handle.

    There's no dedicated endpoint for "who am I" on the server today,
    so we use the team list as a proxy: if the bearer's most recent
    upload set the github field to ``X`` and ``X`` appears in the
    team list, that's the handle.  Failing that, we fall back to None,
    which broadens the filter to "any needs_labeling session" (still
    fine for a single-user box like muscle).

    Future work: server should expose ``GET /api/me``.  Tracked in
    the vezir_plan PR3 outstanding list.
    """
    # For now just return None -- safe default that doesn't suppress
    # anyone's alerts.  The filter then trips on "if my_github and ..."
    # which falls through, so every needs_labeling session in the
    # team-visible list will alert.  Acceptable until the server adds
    # /api/me.
    return None
