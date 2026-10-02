"""POST /api/sessions/{id}/move (0.26.0): members move their own sessions.

Incident 2026-10-02: a meeting recorded under the wrong team could only be
moved by the server admin (`vezir session move`, DB-level).  Pins the
member-facing endpoint: admin OR uploader, member of the destination,
never while the worker processes the session, and an already-synced copy
is reported (removing it from the old repo is deliberately manual).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def client(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture(autouse=True)
def _no_tasks():
    from vezir.server import worker

    with worker._TASKS_LOCK:
        worker._ACTIVE_TASKS.clear()
    yield
    with worker._TASKS_LOCK:
        worker._ACTIVE_TASKS.clear()


def _member(github, team, *, is_admin=False):
    from vezir.server import auth, queue

    if queue.get_team(team) is None:
        queue.create_team(team, team.capitalize())
    queue.add_membership(github, team, role="admin" if is_admin else "scribe", added_by="t")
    return auth._issue_raw(github, is_admin=is_admin)


def _h(token, team="blink"):
    return {"Authorization": f"Bearer {token}", "X-Team-Id": team}


def _job(sid="01S", github="alice", team="blink", status="done", **kw):
    from vezir.server import queue

    queue.enqueue(sid, github=github, title="t", team_id=team, **kw)
    if status != "queued":
        queue.update_status(sid, status)


def _team_of(sid):
    from vezir.server import queue

    return queue.get_team(queue.get(sid)["team_id"])["slug"]


def _move(client, tok, body, team="blink", sid="01S"):
    return client.post(f"/api/sessions/{sid}/move", json=body, headers=_h(tok, team))


def test_uploader_moves_own_session(client):
    alice = _member("alice", "blink")
    _member("alice", "twentyone")
    _job(sync_enabled=False)
    r = _move(client, alice, {"to_team": "twentyone"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moved"] is True
    assert (body["from_team"], body["to_team"]) == ("blink", "twentyone")
    assert body["was_synced"] is False and body["warning"] is None
    assert _team_of("01S") == "twentyone"
    # Gone from the old team's view, present in the new one.
    assert client.get("/api/sessions/01S", headers=_h(alice, "blink")).status_code == 404
    assert client.get("/api/sessions/01S", headers=_h(alice, "twentyone")).status_code == 200


def test_synced_session_warns_copy_stays(client):
    alice = _member("alice", "blink")
    _member("alice", "twentyone")
    _job(sync_enabled=True)  # done + sync on → was synced
    body = _move(client, alice, {"to_team": "twentyone"}).json()
    assert body["was_synced"] is True
    assert "blink's git repo" in body["warning"]
    assert "manually" in body["warning"]


def test_other_member_forbidden(client):
    _member("alice", "blink")
    bob = _member("bob", "blink")
    _member("bob", "twentyone")
    _job()
    assert _move(client, bob, {"to_team": "twentyone"}).status_code == 403
    assert _team_of("01S") == "blink"


def test_destination_membership_required(client):
    alice = _member("alice", "blink")
    _member("carol", "twentyone")  # creates the team; alice not a member
    _job()
    r = _move(client, alice, {"to_team": "twentyone"})
    assert r.status_code == 403
    assert "not a member of team 'twentyone'" in r.text


def test_admin_may_move_anyones_session_anywhere(client):
    _member("alice", "blink")
    root = _member("root", "blink", is_admin=True)
    _member("carol", "twentyone")
    _job()
    assert _move(client, root, {"to_team": "twentyone"}).status_code == 200
    assert _team_of("01S") == "twentyone"


def test_cross_team_and_foreign_personal_are_404(client):
    alice = _member("alice", "blink")
    _member("alice", "twentyone")
    bob = _member("bob", "blink")
    _member("bob", "twentyone")
    _job()
    # Asked from the wrong team scope.
    assert _move(client, alice, {"to_team": "blink"}, team="twentyone").status_code == 404
    # Someone else's personal session is invisible.
    _job("01P", github="alice", personal=True)
    assert _move(client, bob, {"to_team": "twentyone"}, sid="01P").status_code == 404
    # The owner can move it; it stays personal.
    assert _move(client, alice, {"to_team": "twentyone"}, sid="01P").status_code == 200
    from vezir.server import queue
    assert queue.get("01P")["personal"]


def test_unknown_destination_404_and_same_team_noop(client):
    alice = _member("alice", "blink")
    _job()
    assert _move(client, alice, {"to_team": "nope"}).status_code == 404
    body = _move(client, alice, {"to_team": "blink"}).json()
    assert body["moved"] is False


@pytest.mark.parametrize("status", ["transcribing", "summarizing", "syncing"])
def test_refused_while_processing(client, status):
    alice = _member("alice", "blink")
    _member("alice", "twentyone")
    _job(status=status)
    r = _move(client, alice, {"to_team": "twentyone"})
    assert r.status_code == 409
    assert status in r.text
    assert _team_of("01S") == "blink"


def test_queued_job_may_move(client):
    alice = _member("alice", "blink")
    _member("alice", "twentyone")
    _job(status="queued")
    assert _move(client, alice, {"to_team": "twentyone"}).status_code == 200
    assert _team_of("01S") == "twentyone"


def test_refused_while_follow_up_task_active(client):
    from vezir.server import worker

    alice = _member("alice", "blink")
    _member("alice", "twentyone")
    _job()
    with worker._TASKS_LOCK:
        worker._ACTIVE_TASKS.add(("sync", "01S"))
    r = _move(client, alice, {"to_team": "twentyone"})
    assert r.status_code == 409
    assert _team_of("01S") == "blink"


def test_sync_flag_queues_sync_into_destination(client, monkeypatch):
    from vezir.server import meet_runner, queue, worker

    alice = _member("alice", "blink")
    _member("alice", "twentyone")
    _job(sync_enabled=False)
    monkeypatch.setattr(meet_runner, "team_has_sync_target", lambda team: True)
    queued = []
    monkeypatch.setattr(
        worker, "enqueue_task", lambda kind, sid, **kw: queued.append((kind, sid)) or True,
    )
    body = _move(client, alice, {"to_team": "twentyone", "sync": True}).json()
    assert body["sync_queued"] is True
    assert queued == [("sync", "01S")]
    assert queue.get("01S")["sync_enabled"]


def test_sync_flag_without_destination_remote_explains(client, monkeypatch):
    from vezir.server import meet_runner

    alice = _member("alice", "blink")
    _member("alice", "twentyone")
    _job(sync_enabled=False)
    monkeypatch.setattr(meet_runner, "team_has_sync_target", lambda team: False)
    body = _move(client, alice, {"to_team": "twentyone", "sync": True}).json()
    assert body["moved"] is True and body["sync_queued"] is False
    assert "no git sync remote" in body["warning"]


def test_move_job_team_is_conditional():
    """The status check and the write are one statement."""
    import os
    import tempfile as _tf

    from vezir.server import queue

    with _tf.TemporaryDirectory() as d:
        os.environ["VEZIR_DATA"] = d
        try:
            queue.create_team("blink", "Blink")
            queue.create_team("twentyone", "21")
            b = queue.resolve_team_uuid("blink")
            t = queue.resolve_team_uuid("twentyone")
            queue.enqueue("01S", github="a", team_id="blink")
            queue.update_status("01S", "transcribing")
            assert queue.move_job_team("01S", b, t) is False
            queue.update_status("01S", "done")
            assert queue.move_job_team("01S", t, b) is False  # wrong source
            assert queue.move_job_team("01S", b, t) is True
        finally:
            del os.environ["VEZIR_DATA"]
