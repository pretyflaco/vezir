"""Tests for session import (v0.16.0) and sessions pagination.

Import: POST /api/sessions/import registers a pre-processed millet
session's artifact bundle verbatim (status "imported"; no transcribe,
no auto-label, no sync until triggered on demand).

Pagination: GET /api/sessions?offset=N pages through older sessions.
"""
from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

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


def _transcript_json() -> bytes:
    return json.dumps({
        "language": "en",
        "speakers": [{"id": "SPEAKER_00"}],
        "segments": [
            {"start": 0.0, "end": 3.0, "text": "Hello from the archive.",
             "speaker": "SPEAKER_00"},
        ],
    }).encode()


def _bundle(transcript_name: str = "meeting-20260521-143125.json") -> list:
    return [
        ("files", (transcript_name, io.BytesIO(_transcript_json()),
                   "application/json")),
        ("files", ("meeting-20260521-143125.txt",
                   io.BytesIO(b"[00:00] SPEAKER_00: Hello from the archive.\n"),
                   "text/plain")),
        ("files", ("meeting-20260521-143125.summary.md",
                   io.BytesIO(b"# Summary\n\nOld meeting.\n"),
                   "text/markdown")),
        ("files", ("meeting-20260521-143125.frontmatter.json",
                   io.BytesIO(json.dumps({
                       "title": "AB Board pt1",
                       "date": "2026-05-21T14:31:25Z",
                   }).encode()),
                   "application/json")),
    ]


# ── import endpoint ─────────────────────────────────────────────────────────


def test_import_registers_session_as_imported(client_and_token, tmp_data):
    client, token = client_and_token
    resp = client.post(
        "/api/sessions/import",
        headers=_bearer(token),
        files=_bundle(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sid = body["session_id"]
    assert body["status"] == "imported"
    # Frontmatter supplied title + original meeting date.
    assert body["title"] == "AB Board pt1"

    from vezir.server import queue

    row = queue.get(sid)
    assert row["status"] == "imported"
    assert row["created_at"] == "2026-05-21T14:31:25Z"
    # Import is archival: sync and auto-label stay off.
    assert row["sync_enabled"] == 0
    assert row["auto_label_enabled"] == 0
    assert row["summary_preset"] is None

    # Artifacts land under canonical <session_id>.<ext> names.
    from vezir import config

    sdir = config.sessions_dir() / sid
    assert (sdir / f"{sid}.json").exists()
    assert (sdir / f"{sid}.txt").exists()
    assert (sdir / f"{sid}.summary.md").exists()
    assert (sdir / f"{sid}.frontmatter.json").exists()
    # And they're discoverable / downloadable immediately.
    assert ".json" in json.dumps(body["artifacts"])
    art = client.get(f"/artifact/{sid}/{sid}.txt", headers=_bearer(token))
    assert art.status_code == 200
    assert b"Hello from the archive" in art.content


def test_import_audio_included(client_and_token):
    """The OGG is accepted (magic-checked) so clips/auto-label work later."""
    client, token = client_and_token
    ogg = b"OggS" + b"\x00" * 100
    files = _bundle() + [
        ("files", ("meeting-20260521-143125.ogg", io.BytesIO(ogg),
                   "application/ogg")),
    ]
    resp = client.post(
        "/api/sessions/import", headers=_bearer(token), files=files,
    )
    assert resp.status_code == 200, resp.text
    sid = resp.json()["session_id"]
    from vezir import config

    assert (config.sessions_dir() / sid / f"{sid}.ogg").exists()


def test_import_requires_transcript_json(client_and_token):
    client, token = client_and_token
    files = [
        ("files", ("meeting.txt", io.BytesIO(b"hi"), "text/plain")),
    ]
    resp = client.post(
        "/api/sessions/import", headers=_bearer(token), files=files,
    )
    assert resp.status_code == 400
    assert "transcript" in resp.json()["detail"]


def test_import_rejects_non_transcript_json(client_and_token):
    client, token = client_and_token
    files = [
        ("files", ("meeting.json", io.BytesIO(b'{"nope": true}'),
                   "application/json")),
    ]
    resp = client.post(
        "/api/sessions/import", headers=_bearer(token), files=files,
    )
    assert resp.status_code == 400
    assert "segments" in resp.json()["detail"]


def test_import_rejects_unsafe_filename(client_and_token):
    client, token = client_and_token
    files = [
        ("files", ("../../evil.json", io.BytesIO(_transcript_json()),
                   "application/json")),
    ]
    resp = client.post(
        "/api/sessions/import", headers=_bearer(token), files=files,
    )
    assert resp.status_code == 400


def test_import_rejects_duplicate_artifact_type(client_and_token):
    client, token = client_and_token
    files = _bundle() + [
        ("files", ("second.json", io.BytesIO(_transcript_json()),
                   "application/json")),
    ]
    resp = client.post(
        "/api/sessions/import", headers=_bearer(token), files=files,
    )
    assert resp.status_code == 400
    assert "duplicate" in resp.json()["detail"]


def test_import_cross_team_is_404(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server import auth
    from vezir.server.app import create_app

    client = TestClient(create_app(), follow_redirects=False)
    bob_tok = auth.issue("bob", team_id="twentyone")
    resp = client.post(
        "/api/sessions/import",
        headers=_bearer(bob_tok, team="twentyone"),
        files=_bundle(),
    )
    assert resp.status_code == 200  # bob imports into HIS team

    sid = resp.json()["session_id"]
    alice_tok = auth.issue("alice", team_id="blink")
    detail = client.get(
        f"/api/sessions/{sid}", headers=_bearer(alice_tok, team="blink"),
    )
    assert detail.status_code == 404


def test_imported_session_is_labelable_and_syncable(client_and_token):
    """The on-demand follow-ups (label, auto-label, sync) admit imported."""
    client, token = client_and_token
    resp = client.post(
        "/api/sessions/import", headers=_bearer(token), files=_bundle(),
    )
    sid = resp.json()["session_id"]

    # Segments endpoint works on imported sessions.
    seg = client.get(
        f"/label/{sid}/segments/SPEAKER_00", headers=_bearer(token),
    )
    assert seg.status_code == 200
    assert seg.json()["total"] == 1

    # Auto-label queues (worker is stubbed in tests; just check the gate).
    from unittest.mock import patch

    with patch("vezir.server.sessions.worker.enqueue_task") as enq:
        enq.return_value = True
        r = client.post(
            f"/api/sessions/{sid}/auto-label", headers=_bearer(token), json={},
        )
    assert r.status_code == 200

    # Sync-now gate admits imported (queues a task).  Needs a team sync
    # remote — set one (the endpoint 409s for remote-less teams, 0.8.10).
    from vezir.server import queue as _q

    if _q.get_team("blink") is None:
        _q.create_team("blink", "Blink",
                       sync_remote="https://git.example/blink.git")
    else:
        _q.update_team_sync("blink",
                            sync_remote="https://git.example/blink.git")
    with patch("vezir.server.sessions.worker.enqueue_task") as enq:
        enq.return_value = True
        r = client.post(f"/session/{sid}/sync", headers=_bearer(token), json={})
    assert r.status_code == 200


# ── collect_import_files (client) ────────────────────────────────────────────


def test_collect_import_files_picks_one_per_type(tmp_path):
    from vezir.client.uploader import collect_import_files

    d = tmp_path / "meeting-20260521-143125"
    d.mkdir()
    (d / "meeting-20260521-143125.json").write_text("{}")
    (d / "meeting-20260521-143125.txt").write_text("t")
    (d / "meeting-20260521-143125.srt").write_text("s")
    (d / "meeting-20260521-143125.pdf").write_bytes(b"%PDF")
    (d / "meeting-20260521-143125.ogg").write_bytes(b"OggS")
    (d / "meeting-20260521-143125.summary.md").write_text("# s")
    (d / "meeting-20260521-143125.frontmatter.json").write_text("{}")
    (d / "meeting-20260521-143125.session.json").write_text("{}")
    (d / "session.json").write_text("{}")  # vezir pull metadata -> .json class
    (d / "unrelated.log").write_text("x")  # not an artifact -> skipped

    files = collect_import_files(d)
    names = [p.name for p in files]
    # Transcript json first.
    assert names[0] == "meeting-20260521-143125.json"
    assert "meeting-20260521-143125.ogg" in names
    assert "meeting-20260521-143125.summary.md" in names
    assert "unrelated.log" not in names


def test_collect_import_files_requires_transcript(tmp_path):
    from vezir.client.uploader import collect_import_files

    d = tmp_path / "empty-meeting"
    d.mkdir()
    (d / "meeting.txt").write_text("t")
    with pytest.raises(FileNotFoundError, match="transcript"):
        collect_import_files(d)


# ── pagination ──────────────────────────────────────────────────────────────


def test_sessions_offset_pages_through_older_sessions(client_and_token):
    client, token = client_and_token
    from vezir.server import queue

    for i in range(5):
        queue.enqueue(f"01PAGE{i}", "alice", title=f"m{i}", team_id="blink")

    page1 = client.get(
        "/api/sessions?limit=2", headers=_bearer(token),
    ).json()["sessions"]
    page2 = client.get(
        "/api/sessions?limit=2&offset=2", headers=_bearer(token),
    ).json()["sessions"]
    page3 = client.get(
        "/api/sessions?limit=2&offset=4", headers=_bearer(token),
    ).json()["sessions"]

    ids = [[s["id"] for s in page1], [s["id"] for s in page2],
           [s["id"] for s in page3]]
    # Newest-first, no overlap across pages.
    flat = [i for page in ids for i in page]
    assert len(flat) == len(set(flat)) == 5
    assert ids[0][0] != ids[1][0]


def test_sessions_offset_negative_is_clamped(client_and_token):
    client, token = client_and_token
    from vezir.server import queue

    queue.enqueue("01NEG", "alice", title="t", team_id="blink")
    # Negative offset must not error (clamped to 0).
    resp = client.get(
        "/api/sessions?limit=1&offset=-5", headers=_bearer(token),
    )
    assert resp.status_code == 200
    assert resp.json()["sessions"]
