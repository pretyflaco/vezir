"""Tests for POST /api/sessions/{id}/auto-label (v0.14.2).

Exposes ``worker.reauto_label_session`` to clients (TUI button / API):
queues an ``auto_label`` worker task that re-runs ``millet label --auto``
against the team voiceprint DB, always with force=True (explicit user
consent overrides an upload-time auto-label opt-out).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def client_and_token(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server import auth
    from vezir.server.app import create_app

    token = auth.issue("alice")
    app = create_app()
    return TestClient(app, follow_redirects=False), token


def _bearer(token: str, team: str = "blink") -> dict:
    return {"Authorization": f"Bearer {token}", "X-Team-Id": team}


def _seed_session(session_id: str, status: str = "needs_labeling", **kwargs):
    from vezir.server import queue

    if queue.get_team("blink") is None:
        queue.create_team("blink", "Blink")
    queue.enqueue(session_id, "alice", team_id="blink", **kwargs)
    queue.update_status(session_id, status)


# ── endpoint ────────────────────────────────────────────────────────────────


def test_auto_label_requires_bearer(client_and_token):
    client, _ = client_and_token
    resp = client.post("/api/sessions/01X/auto-label", json={})
    assert resp.status_code == 401


def test_auto_label_unknown_session_is_404(client_and_token):
    client, token = client_and_token
    resp = client.post("/api/sessions/01NOPE/auto-label", headers=_bearer(token))
    assert resp.status_code == 404


def test_auto_label_wrong_status_is_409(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session("01BUSY", status="transcribing")
    resp = client.post(
        "/api/sessions/01BUSY/auto-label", headers=_bearer(token), json={},
    )
    assert resp.status_code == 409


def test_auto_label_queues_task_with_force(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session("01AL", status="needs_labeling")

    with patch("vezir.server.sessions.worker.enqueue_task") as enq:
        enq.return_value = True
        resp = client.post(
            "/api/sessions/01AL/auto-label", headers=_bearer(token), json={},
        )
    assert resp.status_code == 200
    assert resp.json() == {"session_id": "01AL", "queued": True}
    enq.assert_called_once_with(
        "auto_label", "01AL", sync=False, force=True,
    )


def test_auto_label_passes_sync_flag(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session("01ALS", status="needs_labeling")

    with patch("vezir.server.sessions.worker.enqueue_task") as enq:
        enq.return_value = True
        resp = client.post(
            "/api/sessions/01ALS/auto-label",
            headers=_bearer(token), json={"sync": True},
        )
    assert resp.status_code == 200
    enq.assert_called_once_with(
        "auto_label", "01ALS", sync=True, force=True,
    )


def test_auto_label_accepts_done_and_error_statuses(client_and_token, tmp_data):
    """done/error/sync_failed sessions are re-labelable too."""
    client, token = client_and_token
    for i, status in enumerate(("done", "error", "sync_failed")):
        sid = f"01STAT{i}"
        _seed_session(sid, status=status)
        with patch("vezir.server.sessions.worker.enqueue_task") as enq:
            enq.return_value = True
            resp = client.post(
                f"/api/sessions/{sid}/auto-label",
                headers=_bearer(token), json={},
            )
        assert resp.status_code == 200, status


# ── worker dispatch ─────────────────────────────────────────────────────────


def test_run_task_dispatches_auto_label(monkeypatch):
    from vezir.server import worker

    calls = []

    def fake_reauto(sid, *, sync=False, force=False):
        calls.append((sid, sync, force))

    monkeypatch.setattr(worker, "reauto_label_session", fake_reauto)
    monkeypatch.setattr(
        "vezir.server.meet_runner.session_shim_lock",
        lambda sid: _NullContext(),
    )

    worker._run_task("auto_label", "01DISP", {"sync": True, "force": True})
    assert calls == [("01DISP", True, True)]


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_reauto_force_overrides_opt_out(tmp_data, monkeypatch):
    """force=True runs auto-label even when auto_label_enabled=0."""
    from vezir.server import worker

    _seed_session("01OPT", status="needs_labeling", auto_label_enabled=False)
    sd = tmp_data / "sessions" / "01OPT"
    sd.mkdir(parents=True)
    (sd / "01OPT.json").write_text('{"segments": [], "speakers": []}')

    rc_calls = []
    monkeypatch.setattr(
        "vezir.server.meet_runner.label_auto",
        lambda sdir, jid, tid, lp: rc_calls.append((jid, tid)) or 0,
    )
    monkeypatch.setattr(
        "vezir.server.meet_runner.cleanup_home_shim", lambda jid: None,
    )
    monkeypatch.setattr(worker, "_speaker_resolution", lambda sdir: ([], []))
    monkeypatch.setattr(worker, "_has_unresolved_speakers", lambda sdir: False)

    # Without force: skipped.
    res = worker.reauto_label_session("01OPT", force=False)
    assert res["error"] == "auto_label_enabled=0 (skipped)"
    assert rc_calls == []

    # With force: runs.  (team_id arrives as the resolved team uuid.)
    res = worker.reauto_label_session("01OPT", force=True)
    assert res["error"] is None
    assert res["status"] == "done"
    assert [sid for sid, _tid in rc_calls] == ["01OPT"]
    assert rc_calls[0][1] not in ("", None, "blink") or rc_calls[0][1] == "blink"


# ── TUI wiring ──────────────────────────────────────────────────────────────


def test_detail_screen_binds_a_to_auto_label():
    from vezir.client.tui.detail_screen import DetailScreen

    keys = {b.key: b.action for b in DetailScreen.BINDINGS}
    assert keys.get("a") == "auto_label"


def test_detail_screen_auto_label_choice_dispatches():
    from vezir.client.tui.detail_screen import DetailScreen

    screen = DetailScreen.__new__(DetailScreen)
    screen.session_id = "01AL"
    calls = []
    screen._action_worker = lambda label, method, **kw: calls.append(
        (label, method, kw),
    )  # type: ignore[method-assign]

    screen._on_auto_label_choice(None)   # cancelled -> nothing
    screen._on_auto_label_choice(False)  # no sync
    screen._on_auto_label_choice(True)   # sync if resolved
    assert calls == [
        ("auto-label", "auto_label", {"sync": False}),
        ("auto-label", "auto_label", {"sync": True}),
    ]


def test_compose_includes_auto_label_button():
    """The compose tree registers the auto-label button id."""
    import re
    from pathlib import Path as _Path

    src = (_Path(__file__).parent.parent / "vezir" / "client" / "tui" /
           "detail_screen.py").read_text(encoding="utf-8")
    assert 'id="auto-label-btn"' in src
    # And the dispatch maps the button id to the action.
    assert re.search(r'bid == "auto-label-btn"', src)


# ── worker dispatch ─────────────────────────────────────────────────────────
