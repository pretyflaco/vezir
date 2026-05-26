"""Tests for vezir/client/tui/notify.py.

The Textual integration (set_interval timer + worker thread) is left
out of automated coverage; it's wired into MainScreen.on_mount and
gated by VEZIR_TUI_DISABLE_NOTIFY_POLL=1 in the rest of the TUI test
suite.  These tests cover the pure-function building blocks:
filtering, new-alert detection, formatting.
"""
from __future__ import annotations

from vezir.client.api import Session
from vezir.client.tui.notify import (
    LabelingState,
    filter_my_needs_labeling,
    find_new_alerts,
    format_alert,
)


def _sess(sid: str, *, status: str = "needs_labeling", github: str = "alice",
          title: str | None = None) -> Session:
    return Session(id=sid, status=status, github=github, title=title)


def test_filter_drops_non_labeling_status():
    sessions = [
        _sess("A", status="done"),
        _sess("B", status="needs_labeling"),
        _sess("C", status="transcribing"),
    ]
    out = filter_my_needs_labeling(sessions, "alice")
    assert [s.id for s in out] == ["B"]


def test_filter_drops_other_users_when_my_github_set():
    sessions = [
        _sess("X", github="alice"),
        _sess("Y", github="bob"),
        _sess("Z", github="alice"),
    ]
    out = filter_my_needs_labeling(sessions, "alice")
    assert [s.id for s in out] == ["X", "Z"]


def test_filter_keeps_all_when_my_github_is_none():
    sessions = [_sess("X", github="alice"), _sess("Y", github="bob")]
    out = filter_my_needs_labeling(sessions, None)
    assert [s.id for s in out] == ["X", "Y"]


def test_find_new_alerts_first_call_returns_all():
    state = LabelingState.new()
    sessions = [_sess("A"), _sess("B")]
    out = find_new_alerts(sessions, state)
    assert [s.id for s in out] == ["A", "B"]
    assert state.seen_needs_labeling == {"A", "B"}


def test_find_new_alerts_second_call_returns_nothing_new():
    state = LabelingState.new()
    sessions = [_sess("A"), _sess("B")]
    find_new_alerts(sessions, state)
    out = find_new_alerts(sessions, state)
    assert out == []


def test_find_new_alerts_re_alerts_when_session_bounces_back():
    """If a session leaves needs_labeling then re-enters, we re-notify."""
    state = LabelingState.new()
    find_new_alerts([_sess("A")], state)
    # A leaves the needs_labeling set (server transitioned it to done).
    find_new_alerts([], state)
    assert state.seen_needs_labeling == set()
    # A comes back (e.g. user triggered retry-summary which can bounce
    # it back through needs_labeling).
    out = find_new_alerts([_sess("A")], state)
    assert [s.id for s in out] == ["A"]


def test_find_new_alerts_partial_overlap():
    state = LabelingState.new()
    find_new_alerts([_sess("A"), _sess("B")], state)
    out = find_new_alerts([_sess("B"), _sess("C")], state)
    # Only C is new; A dropped out of the set (forgotten).
    assert [s.id for s in out] == ["C"]
    assert state.seen_needs_labeling == {"B", "C"}


def test_format_alert_uses_title_when_present():
    title, body = format_alert(_sess("01X", title="weekly standup"))
    assert title == "vezir: session needs labeling"
    assert "weekly standup" in body
    assert "vezir tui" in body


def test_format_alert_falls_back_to_id():
    title, body = format_alert(_sess("01ABC", title=None))
    assert "01ABC" in body
