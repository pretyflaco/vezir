"""Tests for JSON labeling API endpoints (native client support).

GET  /api/label/{session_id}   → speaker list + team handles
POST /api/label/{session_id}   → apply labels from JSON body
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    return {
        "Authorization": f"Bearer {token}",
        "X-Team-Id": team,
    }


def _seed_session(tmp_data, session_id: str, status: str = "needs_labeling"):
    """Create a minimal session row in the queue for testing.

    Ensures the ``blink`` team exists WITH a sync_remote so the sync-exercising
    tests below reach ``meet_runner.sync`` (0.8.10 added a guard that skips sync
    entirely for remote-less teams).
    """
    from vezir.server import queue
    if queue.get_team("blink") is None:
        queue.create_team("blink", "Blink", sync_remote="https://git.example/blink.git")
    else:
        queue.update_team_sync("blink", sync_remote="https://git.example/blink.git")
    queue.enqueue(session_id, "alice", "test meeting", team_id="blink")
    queue.update_status(session_id, status)


# ── GET /api/label/{session_id} ─────────────────────────────────────────────


def test_api_label_get_requires_bearer(client_and_token):
    client, _ = client_and_token
    resp = client.get("/api/label/01TEST")
    assert resp.status_code == 401


def test_api_label_get_session_not_found(client_and_token):
    client, token = client_and_token
    resp = client.get("/api/label/01NONEXISTENT", headers=_bearer(token))
    assert resp.status_code == 404


def test_api_label_get_wrong_status(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="transcribing")
    resp = client.get("/api/label/01TEST", headers=_bearer(token))
    assert resp.status_code == 409


@patch("vezir.server.labels._get_speakers")
def test_api_label_get_returns_speakers(mock_get_speakers, client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")

    # Create session dir so _find_wav can check it
    sdir = tmp_data / "sessions" / "01TEST"
    sdir.mkdir(parents=True, exist_ok=True)

    # Mock millet's get_speakers
    sp1 = MagicMock()
    sp1.id = "REMOTE_0"
    sp1.channel = 1
    sp1.sample_text = "Hello there"
    sp2 = MagicMock()
    sp2.id = "YOU"
    sp2.channel = 0
    sp2.sample_text = "Hi"
    mock_get_speakers.return_value = [sp1, sp2]

    resp = client.get("/api/label/01TEST", headers=_bearer(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "01TEST"
    assert body["status"] == "needs_labeling"
    assert len(body["speakers"]) == 2
    assert body["speakers"][0]["id"] == "REMOTE_0"
    assert body["speakers"][0]["channel"] == 1
    assert body["speakers"][0]["sample_text"] == "Hello there"
    assert isinstance(body["team"], list)
    assert body["audio_available"] is False  # no wav file in test dir


@patch("vezir.server.labels._get_speakers")
def test_api_label_get_audio_available(mock_get_speakers, client_and_token, tmp_data):
    """audio_available should be True when a WAV or OGG file exists."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")

    sdir = tmp_data / "sessions" / "01TEST"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "01TEST.ogg").write_bytes(b"OggS" + b"\x00" * 100)

    mock_get_speakers.return_value = []

    resp = client.get("/api/label/01TEST", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["audio_available"] is True


# ── POST /api/label/{session_id} ────────────────────────────────────────────


def test_api_label_post_requires_bearer(client_and_token):
    client, _ = client_and_token
    resp = client.post(
        "/api/label/01TEST",
        json={"labels": {"REMOTE_0": "alice"}},
    )
    assert resp.status_code == 401


def test_api_label_post_session_not_found(client_and_token):
    client, token = client_and_token
    resp = client.post(
        "/api/label/01NONEXISTENT",
        headers=_bearer(token),
        json={"labels": {"REMOTE_0": "alice"}},
    )
    assert resp.status_code == 404


def test_api_label_post_wrong_status(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="transcribing")
    resp = client.post(
        "/api/label/01TEST",
        headers=_bearer(token),
        json={"labels": {"REMOTE_0": "alice"}},
    )
    assert resp.status_code == 409


def test_api_label_post_bad_body(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")
    resp = client.post(
        "/api/label/01TEST",
        headers=_bearer(token),
        json={"wrong_key": "bad"},
    )
    assert resp.status_code == 400


@patch("vezir.server.labels._apply_and_finalize")
def test_api_label_post_success(mock_apply, client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")

    resp = client.post(
        "/api/label/01TEST",
        headers=_bearer(token),
        json={"labels": {"REMOTE_0": "kasita", "REMOTE_1": "alice"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["session_id"] == "01TEST"
    from vezir.server import queue
    blink_uuid = queue.get_team("blink")["id"]
    mock_apply.assert_called_once_with(
        "01TEST",
        {"REMOTE_0": "kasita", "REMOTE_1": "alice"},
        "alice",  # github handle of the authenticated user
        blink_uuid,  # team_id (v0.7.4+): the auth-resolved team uuid
    )


@patch("vezir.server.labels._apply_and_finalize")
def test_api_label_post_strips_empty_labels(mock_apply, client_and_token, tmp_data):
    """Empty or whitespace-only label values should be skipped."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")

    resp = client.post(
        "/api/label/01TEST",
        headers=_bearer(token),
        json={"labels": {"REMOTE_0": "kasita", "REMOTE_1": "  ", "YOU": ""}},
    )
    assert resp.status_code == 200
    from vezir.server import queue
    blink_uuid = queue.get_team("blink")["id"]
    mock_apply.assert_called_once_with(
        "01TEST", {"REMOTE_0": "kasita"}, "alice", blink_uuid,
    )


@patch("vezir.server.labels._get_speakers")
def test_api_label_get_works_for_done_sessions(mock_get_speakers, client_and_token, tmp_data):
    """Sessions in 'done' status should be re-labelable."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")

    sdir = tmp_data / "sessions" / "01TEST"
    sdir.mkdir(parents=True, exist_ok=True)
    mock_get_speakers.return_value = []

    resp = client.get("/api/label/01TEST", headers=_bearer(token))
    assert resp.status_code == 200


# ── GET /label/{session_id}/clip/{speaker_id} ───────────────────────────────
# Regression: once voiceprint auto-labeling persists matches into the
# transcript, speaker ids reaching the clip endpoint can be real names with
# spaces (e.g. "Juan Pablo").  The old ``^[A-Za-z0-9_]+$`` guard rejected
# these with 400; the cache filename must also stay path-safe.


def test_safe_clip_id_accepts_names_with_spaces():
    from vezir.server.labels import _is_safe_clip_id

    assert _is_safe_clip_id("Juan Pablo")
    assert _is_safe_clip_id("O'Brien")
    assert _is_safe_clip_id("SPEAKER_08")
    assert _is_safe_clip_id("Anne-Marie")


def test_safe_clip_id_rejects_traversal_and_separators():
    from vezir.server.labels import _is_safe_clip_id

    assert not _is_safe_clip_id("../etc/passwd")
    assert not _is_safe_clip_id("a/b")
    assert not _is_safe_clip_id("a\\b")
    assert not _is_safe_clip_id("bad\x00id")
    assert not _is_safe_clip_id("")


def test_safe_clip_filename_is_path_safe_and_unique():
    from vezir.server.labels import _safe_clip_filename

    fn = _safe_clip_filename("Juan Pablo")
    assert "/" not in fn and "\\" not in fn and " " not in fn
    assert fn.endswith(".wav")
    # Distinct ids that slugify alike must not collide (hash suffix differs).
    assert _safe_clip_filename("Juan Pablo") != _safe_clip_filename("Juan-Pablo")


def test_label_clip_rejects_unsafe_speaker_id(client_and_token, tmp_data):
    """A ``..`` traversal id that reaches the handler is rejected with 400.

    (URL-encoded slashes like ``a%2Fb`` are normalized away by Starlette's
    router and 404 before reaching us; ``%2e%2e`` decodes to a bare ``..``
    segment that *does* reach the handler, where ``_is_safe_clip_id`` blocks
    it.)
    """
    client, token = client_and_token
    resp = client.get(
        "/label/01TEST/clip/%2e%2e",
        headers=_bearer(token),
    )
    assert resp.status_code == 400


@patch("vezir.server.labels.shutil.move")
@patch("vezir.server.labels._get_speakers")
def test_label_clip_accepts_named_speaker(
    mock_get_speakers, mock_move, client_and_token, tmp_data,
):
    """A speaker named "Juan Pablo" gets past validation and is cached under
    a path-safe filename (no 400, no traversal)."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")

    sdir = tmp_data / "sessions" / "01TEST"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "01TEST.wav").write_bytes(b"RIFF" + b"\x00" * 100)

    sp = MagicMock()
    sp.id = "Juan Pablo"
    mock_get_speakers.return_value = [sp]

    from vezir.server.labels import _safe_clip_filename

    def _fake_move(src, dst):
        Path(dst).write_bytes(b"RIFF" + b"\x00" * 10)

    mock_move.side_effect = _fake_move

    with patch(
        "millet.label.extract_speaker_clip",
        return_value=sdir / "tmp_clip.wav",
    ):
        (sdir / "tmp_clip.wav").write_bytes(b"RIFF" + b"\x00" * 10)
        resp = client.get(
            "/label/01TEST/clip/Juan%20Pablo",
            headers=_bearer(token),
        )

    assert resp.status_code == 200
    # Cached under the slugified filename, within the clips dir (no escape).
    cached = sdir / "clips" / _safe_clip_filename("Juan Pablo")
    assert cached.exists()


# ── POST /api/sessions/{id}/retry-summary — language override ────────────────


def _set_summary_error(session_id: str, msg: str | None) -> None:
    from vezir.server import queue
    queue.update_status(session_id, "done", summary_error=msg)


def test_retry_summary_rejects_invalid_language(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    _set_summary_error("01TEST", "boom")
    resp = client.post(
        "/api/sessions/01TEST/retry-summary",
        headers=_bearer(token),
        json={"language": "klingon"},
    )
    assert resp.status_code == 400


@patch("vezir.server.worker.retry_summary_for_session")
def test_retry_summary_language_allows_successful_session(
    mock_worker, client_and_token, tmp_data,
):
    """A language override re-summarizes even when the summary already
    succeeded (no summary_error)."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    _set_summary_error("01TEST", None)  # summary succeeded

    resp = client.post(
        "/api/sessions/01TEST/retry-summary",
        headers=_bearer(token),
        json={"language": "de"},
    )
    assert resp.status_code == 200
    # Worker invoked with the language override.
    assert mock_worker.called
    _, kwargs = mock_worker.call_args
    assert kwargs.get("language_override") == "de"


def test_retry_summary_no_language_still_requires_error(client_and_token, tmp_data):
    """Without a language override, a successful session still 409s
    (preserves the original 'fix a failed summary' contract)."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    _set_summary_error("01TEST", None)
    resp = client.post(
        "/api/sessions/01TEST/retry-summary",
        headers=_bearer(token),
        json={},
    )
    assert resp.status_code == 409


@patch("vezir.server.worker.retry_summary_for_session")
def test_retry_summary_auto_language_treated_as_none(
    mock_worker, client_and_token, tmp_data,
):
    """language='auto' is not an override: it should NOT bypass the
    summary_error guard."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    _set_summary_error("01TEST", None)
    resp = client.post(
        "/api/sessions/01TEST/retry-summary",
        headers=_bearer(token),
        json={"language": "auto"},
    )
    assert resp.status_code == 409
    assert not mock_worker.called


# ── _find_artifacts: per-language summaries ──────────────────────────────────


def test_find_artifacts_exposes_per_language_summaries(tmp_path):
    from vezir.server.worker import _find_artifacts
    base = "m"
    for n in [
        f"{base}.txt", f"{base}.srt", f"{base}.summary.md",
        f"{base}.summary.de.md", f"{base}.summary.fr.md", f"{base}.pdf",
        f"{base}.json", f"{base}.frontmatter.json",
        f"{base}.de.frontmatter.json", f"{base}.summary.meta.json",
        f"{base}.autoid.json",
    ]:
        (tmp_path / n).write_text("x")
    arts = _find_artifacts(tmp_path)
    assert arts["summary"] == f"{base}.summary.md"
    assert arts["summary_de"] == f"{base}.summary.de.md"
    assert arts["summary_fr"] == f"{base}.summary.fr.md"
    # The real transcript json wins over frontmatter/autoid sidecars.
    assert arts["json"] == f"{base}.json"


# ── sync_failed status: vocabulary + endpoint admission ──────────────────────


def test_sync_failed_is_a_valid_status():
    from vezir.server.queue import VALID_STATUSES
    assert "sync_failed" in VALID_STATUSES


def test_sync_now_admits_sync_failed(client_and_token, tmp_data):
    """A sync_failed session can be re-synced via Sync now."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    from vezir.server import queue
    queue.update_status("01TEST", "sync_failed", sync_error="git push rejected")
    with patch("vezir.server.worker.finalize_after_labeling"):
        resp = client.post("/session/01TEST/sync", headers=_bearer(token))
    assert resp.status_code == 200


def test_sync_now_threads_meeting_type_override(client_and_token, tmp_data):
    """A valid meeting_type body is slugified and threaded to the worker."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    captured = {}

    def fake_thread(target, args, name, daemon):
        captured["target"] = target
        captured["args"] = args

        class _T:
            def start(self_inner):
                pass
        return _T()

    with patch("vezir.server.sessions.threading.Thread", side_effect=fake_thread):
        resp = client.post(
            "/session/01TEST/sync",
            headers=_bearer(token),
            json={"meeting_type": "Post Scrum"},
        )
    assert resp.status_code == 200
    assert resp.json()["meeting_type"] == "post-scrum"
    # worker.finalize_after_labeling(session_id, meeting_type)
    assert captured["args"] == ("01TEST", "post-scrum")


def test_sync_now_rejects_unslugifiable_meeting_type(client_and_token, tmp_data):
    """A meeting_type that slugifies to empty is a 422."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    with patch("vezir.server.worker.finalize_after_labeling"):
        resp = client.post(
            "/session/01TEST/sync",
            headers=_bearer(token),
            json={"meeting_type": "///"},
        )
    assert resp.status_code == 422


def test_sync_now_no_body_is_auto(client_and_token, tmp_data):
    """No body → meeting_type=None (auto-detect, current behavior)."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    captured = {}

    def fake_thread(target, args, name, daemon):
        captured["args"] = args

        class _T:
            def start(self_inner):
                pass
        return _T()

    with patch("vezir.server.sessions.threading.Thread", side_effect=fake_thread):
        resp = client.post("/session/01TEST/sync", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["meeting_type"] is None
    assert captured["args"] == ("01TEST", None)


def test_retry_summary_admits_sync_failed(client_and_token, tmp_data):
    """A sync_failed session admits a summary retry (with a language)."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")
    from vezir.server import queue
    queue.update_status("01TEST", "sync_failed", sync_error="boom")
    with patch("vezir.server.worker.retry_summary_for_session"):
        resp = client.post(
            "/api/sessions/01TEST/retry-summary",
            headers=_bearer(token),
            json={"language": "de"},
        )
    assert resp.status_code == 200


def test_finalize_after_labeling_sets_sync_failed_on_push_failure(tmp_data):
    """The explicit sync path (post-label / Sync now) sets status=sync_failed
    when the git push fails."""
    from vezir.server import queue, worker
    _seed_session(tmp_data, "01SYNCFAIL", status="needs_labeling")
    sd = tmp_data / "sessions" / "01SYNCFAIL"
    sd.mkdir(parents=True, exist_ok=True)

    with patch("vezir.server.meet_runner.sync", return_value=1), \
         patch("vezir.server.meet_runner.cleanup_home_shim"), \
         patch("vezir.server.worker._delete_audio"), \
         patch("vezir.server.worker._find_artifacts", return_value={}):
        worker.finalize_after_labeling("01SYNCFAIL")

    row = queue.get("01SYNCFAIL")
    assert row["status"] == "sync_failed"
    assert row["sync_error"]


def test_finalize_after_labeling_done_on_success(tmp_data):
    from vezir.server import queue, worker
    _seed_session(tmp_data, "01SYNCOK", status="needs_labeling")
    sd = tmp_data / "sessions" / "01SYNCOK"
    sd.mkdir(parents=True, exist_ok=True)

    with patch("vezir.server.meet_runner.sync", return_value=0), \
         patch("vezir.server.meet_runner.cleanup_home_shim"), \
         patch("vezir.server.worker._delete_audio"), \
         patch("vezir.server.worker._sync_log_indicates_failure", return_value=None), \
         patch("vezir.server.worker._find_artifacts", return_value={}):
        worker.finalize_after_labeling("01SYNCOK")

    row = queue.get("01SYNCOK")
    assert row["status"] == "done"


def test_finalize_after_labeling_passes_meeting_type_override(tmp_data):
    """The meeting_type override is forwarded to meet_runner.sync."""
    from vezir.server import worker
    _seed_session(tmp_data, "01OVR", status="needs_labeling")
    sd = tmp_data / "sessions" / "01OVR"
    sd.mkdir(parents=True, exist_ok=True)

    with patch("vezir.server.meet_runner.sync", return_value=0) as mock_sync, \
         patch("vezir.server.meet_runner.cleanup_home_shim"), \
         patch("vezir.server.worker._delete_audio"), \
         patch("vezir.server.worker._sync_log_indicates_failure", return_value=None), \
         patch("vezir.server.worker._find_artifacts", return_value={}):
        worker.finalize_after_labeling("01OVR", meeting_type_override="post-scrum")

    # Called as sync(sd, session_id, team_id, log_path, meeting_type=...)
    _, kwargs = mock_sync.call_args
    assert kwargs.get("meeting_type") == "post-scrum"


# ── reauto_label_session (vezir relabel) ─────────────────────────────────────


def _write_transcript(sd, session_id, speakers):
    """Write a minimal millet-style transcript JSON with the given speakers.

    Each speaker gets 4 segments totaling ~24s of speech so that a raw
    placeholder counts as a *substantial* unlabeled participant (above the
    tiny-noise thresholds), exercising the real needs_labeling path rather
    than the tiny-noise short-circuit.
    """
    import json
    sd.mkdir(parents=True, exist_ok=True)
    segments = []
    base = 0.0
    for s in speakers:
        for _ in range(4):
            segments.append({"start": base, "end": base + 6.0, "text": "x", "speaker": s})
            base += 6.0
    (sd / f"{session_id}.json").write_text(json.dumps({
        "segments": segments,
        "speakers": [{"id": s, "label": s} for s in speakers],
    }))


def test_reauto_label_partial_stays_needs_labeling(tmp_data):
    """When some speakers remain raw after re-auto, status stays
    needs_labeling (and we don't sync by default)."""
    from vezir.server import worker
    _seed_session(tmp_data, "01RELBL", status="needs_labeling")
    sd = tmp_data / "sessions" / "01RELBL"
    # Pre-existing transcript with raw placeholders (as left by transcription).
    _write_transcript(sd, "01RELBL", ["SPEAKER_00", "SPEAKER_01"])

    def fake_label_auto(session_dir, job_id, team_id, log_path):
        # Simulate millet applying a confident match for one speaker only.
        _write_transcript(sd, "01RELBL", ["Lukas", "SPEAKER_01"])
        return 0

    with patch("vezir.server.meet_runner.label_auto", side_effect=fake_label_auto), \
         patch("vezir.server.meet_runner.cleanup_home_shim"), \
         patch("vezir.server.meet_runner.sync") as mock_sync, \
         patch("vezir.server.worker._find_artifacts", return_value={}):
        res = worker.reauto_label_session("01RELBL", sync=False)

    assert res["status"] == "needs_labeling"
    assert "Lukas" in res["matched"]
    assert "SPEAKER_01" in res["unresolved"]
    mock_sync.assert_not_called()


def test_reauto_label_fully_resolved_no_sync_marks_done(tmp_data):
    """All speakers resolved + sync=False → done, no sync attempted."""
    from vezir.server import queue, worker
    _seed_session(tmp_data, "01RELOK", status="needs_labeling")
    sd = tmp_data / "sessions" / "01RELOK"
    _write_transcript(sd, "01RELOK", ["SPEAKER_00", "SPEAKER_01"])

    def fake_label_auto(session_dir, job_id, team_id, log_path):
        _write_transcript(sd, "01RELOK", ["Lukas", "Kemal"])
        return 0

    with patch("vezir.server.meet_runner.label_auto", side_effect=fake_label_auto), \
         patch("vezir.server.meet_runner.cleanup_home_shim"), \
         patch("vezir.server.meet_runner.sync") as mock_sync, \
         patch("vezir.server.worker._find_artifacts", return_value={}):
        res = worker.reauto_label_session("01RELOK", sync=False)

    assert res["status"] == "done"
    assert res["synced"] is False
    mock_sync.assert_not_called()
    assert queue.get("01RELOK")["status"] == "done"


def test_reauto_label_fully_resolved_with_sync(tmp_data):
    """All speakers resolved + sync=True → meet_runner.sync is invoked."""
    from vezir.server import worker
    _seed_session(tmp_data, "01RELSYNC", status="needs_labeling")
    sd = tmp_data / "sessions" / "01RELSYNC"
    _write_transcript(sd, "01RELSYNC", ["SPEAKER_00", "SPEAKER_01"])

    def fake_label_auto(session_dir, job_id, team_id, log_path):
        _write_transcript(sd, "01RELSYNC", ["Lukas", "Kemal"])
        return 0

    with patch("vezir.server.meet_runner.label_auto", side_effect=fake_label_auto), \
         patch("vezir.server.meet_runner.cleanup_home_shim"), \
         patch("vezir.server.meet_runner.sync", return_value=0) as mock_sync, \
         patch("vezir.server.worker._sync_log_indicates_failure", return_value=None), \
         patch("vezir.server.worker._find_artifacts", return_value={}):
        res = worker.reauto_label_session("01RELSYNC", sync=True)

    assert res["status"] == "done"
    assert res["synced"] is True
    mock_sync.assert_called_once()


def test_reauto_label_respects_auto_label_opt_out(tmp_data):
    """auto_label_enabled=0 → skip, don't re-label."""
    from vezir.server import queue, worker
    queue.enqueue("01RELOPT", "alice", "m", team_id="blink", auto_label_enabled=False)
    queue.update_status("01RELOPT", "needs_labeling")

    with patch("vezir.server.meet_runner.label_auto") as mock_la, \
         patch("vezir.server.meet_runner.cleanup_home_shim"):
        res = worker.reauto_label_session("01RELOPT")

    mock_la.assert_not_called()
    assert "auto_label_enabled=0" in (res["error"] or "")


def test_reauto_label_missing_transcript(tmp_data):
    """No transcript on disk → reported error, no crash."""
    from vezir.server import worker
    _seed_session(tmp_data, "01RELNOJSON", status="needs_labeling")
    (tmp_data / "sessions" / "01RELNOJSON").mkdir(parents=True, exist_ok=True)

    with patch("vezir.server.meet_runner.label_auto") as mock_la, \
         patch("vezir.server.meet_runner.cleanup_home_shim"):
        res = worker.reauto_label_session("01RELNOJSON")

    mock_la.assert_not_called()
    assert res["error"] == "no transcript on disk"
