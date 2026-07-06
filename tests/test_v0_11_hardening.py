"""Tests for the v0.11.0 hardening round (2026-07 ecosystem review).

Covers:
* sessions_auth._parse_iso UTC correctness on non-UTC hosts
* the revoked-sid cache killing access JWTs immediately
* rate-limit keying: unauthenticated login family is IP-only
* the worker follow-up task queue (dedupe + dispatch)
* meet_runner.apply_labels_json (subprocess boundary; args, probe, cleanup)
* run_meet's VEZIR_MILLET_TIMEOUT process-group kill
* label POST applying via subprocess + queueing the finalize task
* upload cleanup on client disconnect; resumable PATCH locking; orphan
  .part sweeping
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


# ── sessions_auth._parse_iso: UTC on non-UTC hosts ──────────────────────────


def test_parse_iso_roundtrips_on_non_utc_host(monkeypatch):
    """_iso writes UTC; _parse_iso must read UTC.  The old time.mktime
    parse interpreted the struct as LOCAL time, skewing every session
    expiry check by the host's UTC offset."""
    from vezir.server import sessions_auth

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        now = int(time.time())
        assert sessions_auth._parse_iso(sessions_auth._iso(now)) == now
    finally:
        monkeypatch.undo()
        time.tzset()


def test_parse_iso_bad_values():
    from vezir.server import sessions_auth

    assert sessions_auth._parse_iso(None) is None
    assert sessions_auth._parse_iso("") is None
    assert sessions_auth._parse_iso("not-a-date") is None


# ── revoked-sid cache ───────────────────────────────────────────────────────


def test_revoked_session_kills_access_jwt_immediately(tmp_data):
    """Revoking a session must invalidate its already-minted access JWTs,
    not just future refreshes."""
    from vezir.server import nostr_auth, sessions_auth

    pair = sessions_auth.create_session("alice", "", False, "nostr")
    access = pair["access_jwt"]
    assert nostr_auth.verify_session_jwt(access) == ("alice", False)

    assert sessions_auth.revoke_session(pair["sid"]) is True
    assert nostr_auth.verify_session_jwt(access) is None


def test_revoke_all_kills_every_access_jwt(tmp_data):
    from vezir.server import nostr_auth, sessions_auth

    a = sessions_auth.create_session("alice", "", False, "nostr")
    b = sessions_auth.create_session("alice", "", False, "nostr")
    assert sessions_auth.revoke_all_for("alice") == 2
    assert nostr_auth.verify_session_jwt(a["access_jwt"]) is None
    assert nostr_auth.verify_session_jwt(b["access_jwt"]) is None


def test_revoked_cache_loads_from_db_after_restart(tmp_data):
    """A revocation recorded before a process restart must still be
    honored (the cache preloads revoked sids from the DB on first use)."""
    from vezir.server import nostr_auth, sessions_auth

    pair = sessions_auth.create_session("alice", "", False, "nostr")
    sessions_auth.revoke_session(pair["sid"])
    # Simulate a restart: wipe the in-process cache.
    sessions_auth._reset_revoked_cache_for_tests()
    assert nostr_auth.verify_session_jwt(pair["access_jwt"]) is None


# ── rate-limit keying ───────────────────────────────────────────────────────


def _fake_request(ip: str = "203.0.113.7", bearer: str | None = None):
    headers = {}
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=ip),
    )


def test_login_bucket_cannot_be_bypassed_with_random_bearers(monkeypatch):
    """The login/refresh family is unauthenticated: a presented bearer is
    attacker input and must NOT select the bucket (a random bearer per
    request used to get a fresh bucket each time — total bypass)."""
    from fastapi import HTTPException

    from vezir.server import ratelimit

    monkeypatch.delenv("VEZIR_DISABLE_RATELIMIT", raising=False)
    ratelimit._reset_for_tests()

    capacity = int(ratelimit._LIMITERS["login"].capacity)
    for i in range(capacity):
        ratelimit.limit_login(_fake_request(bearer=f"random-{i}"))
    with pytest.raises(HTTPException) as exc:
        ratelimit.limit_login(_fake_request(bearer="random-final"))
    assert exc.value.status_code == 429


def test_login_bucket_is_per_ip(monkeypatch):
    from vezir.server import ratelimit

    monkeypatch.delenv("VEZIR_DISABLE_RATELIMIT", raising=False)
    ratelimit._reset_for_tests()

    capacity = int(ratelimit._LIMITERS["login"].capacity)
    for _ in range(capacity):
        ratelimit.limit_login(_fake_request(ip="198.51.100.1"))
    # A different client IP has its own bucket.
    ratelimit.limit_login(_fake_request(ip="198.51.100.2"))


def test_api_bucket_still_keys_on_bearer(monkeypatch):
    """Authenticated families keep per-token fairness."""
    from vezir.server import ratelimit

    monkeypatch.delenv("VEZIR_DISABLE_RATELIMIT", raising=False)
    ratelimit._reset_for_tests()
    key = ratelimit._client_key(_fake_request(bearer="tok-abc"), "api")
    assert ":tok:" in key
    key_ip = ratelimit._client_key(_fake_request(bearer="tok-abc"), "login", ip_only=True)
    assert ":ip:" in key_ip


# ── worker follow-up task queue ─────────────────────────────────────────────


def test_enqueue_task_dedupes_identical_tasks():
    from vezir.server import worker

    assert worker.enqueue_task("sync", "01DUP", meeting_type=None) is True
    assert worker.enqueue_task("sync", "01DUP", meeting_type=None) is False
    # A different kind for the same session is allowed.
    assert worker.enqueue_task("retry_summary", "01DUP") is True


def test_enqueue_task_rejects_unknown_kind():
    from vezir.server import worker

    with pytest.raises(ValueError, match="unknown task kind"):
        worker.enqueue_task("frobnicate", "01X")


def test_drain_tasks_dispatches_and_releases_dedupe():
    from vezir.server import worker

    calls = []
    with patch.object(
        worker, "finalize_after_labeling",
        side_effect=lambda sid, mt=None: calls.append(("sync", sid, mt)),
    ), patch.object(
        worker, "retry_summary_for_session",
        side_effect=lambda sid, **kw: calls.append(("retry", sid, kw)),
    ):
        worker.enqueue_task("sync", "01A", meeting_type="weekly")
        worker.enqueue_task(
            "retry_summary", "01B",
            preset_override="confidential", language_override="de",
        )
        worker._drain_tasks()

    assert ("sync", "01A", "weekly") in calls
    assert (
        "retry", "01B",
        {"preset_override": "confidential", "language_override": "de"},
    ) in calls
    # After the drain the dedupe key is released: re-enqueue works.
    assert worker.enqueue_task("sync", "01A", meeting_type="weekly") is True


def test_drain_tasks_survives_handler_exceptions():
    from vezir.server import worker

    with patch.object(
        worker, "finalize_after_labeling", side_effect=RuntimeError("boom"),
    ):
        worker.enqueue_task("sync", "01ERR")
        worker._drain_tasks()  # must not raise
    # Dedupe key released even after failure.
    assert worker.enqueue_task("sync", "01ERR") is True


# ── meet_runner.apply_labels_json ───────────────────────────────────────────


def test_apply_labels_json_builds_expected_args(tmp_data, monkeypatch, tmp_path):
    from vezir import config
    from vezir.server import meet_runner

    monkeypatch.setattr(config, "meet_label_supports_apply_json", lambda: True)

    seen: dict = {}

    def fake_run_meet(args, job_id, team_id, log_path=None):
        seen["args"] = list(args)
        # The map file must exist WHILE millet runs.
        map_path = Path(args[args.index("--apply-json") + 1])
        seen["map_existed"] = map_path.exists()
        seen["map_path"] = map_path
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)

    sdir = tmp_path / "sess"
    sdir.mkdir()
    rc = meet_runner.apply_labels_json(
        sdir, "01JOB", "team-uuid", tmp_path / "job.log",
        label_map={"REMOTE_0": "kasita"},
        update_profiles=True,
    )
    assert rc == 0
    args = seen["args"]
    assert args[0] == "label"
    assert "--apply-json" in args
    assert "--no-summary" in args        # regenerate_summary=False default
    assert "--update-profiles" in args
    assert args[-1] == str(sdir)
    assert seen["map_existed"]
    # Cleaned up afterwards; dotted name so artifact globs can't see it.
    assert not seen["map_path"].exists()
    assert seen["map_path"].name.startswith(".")


def test_apply_labels_json_summary_retry_shape(tmp_data, monkeypatch, tmp_path):
    from vezir import config
    from vezir.server import meet_runner

    monkeypatch.setattr(config, "meet_label_supports_apply_json", lambda: True)
    seen: dict = {}
    monkeypatch.setattr(
        meet_runner, "run_meet",
        lambda args, job_id, team_id, log_path=None: seen.setdefault("args", list(args)) and 0 or 0,
    )

    sdir = tmp_path / "sess"
    sdir.mkdir()
    meet_runner.apply_labels_json(
        sdir, "01JOB", "team-uuid", tmp_path / "job.log",
        label_map={},
        regenerate_summary=True,
        summary_preset="confidential",
        summary_language="de",
    )
    args = seen["args"]
    assert "--no-summary" not in args
    assert args[args.index("--summary-preset") + 1] == "confidential"
    assert args[args.index("--summary-language") + 1] == "de"


def test_apply_labels_json_requires_millet_0_13(tmp_data, monkeypatch, tmp_path):
    from vezir import config
    from vezir.server import meet_runner

    monkeypatch.setattr(config, "meet_label_supports_apply_json", lambda: False)
    with pytest.raises(RuntimeError, match="millet-pipeline >= 0.13.0"):
        meet_runner.apply_labels_json(
            tmp_path, "01JOB", "team-uuid", tmp_path / "job.log",
            label_map={"A": "B"},
        )


# ── run_meet timeout ────────────────────────────────────────────────────────


@pytest.mark.timeout(30)
def test_run_meet_kills_wedged_millet_on_timeout(tmp_data, monkeypatch, tmp_path):
    """A hung millet step must not block the single worker forever."""
    from vezir import config
    from vezir.server import meet_runner

    monkeypatch.setattr(config, "meet_binary", lambda: "sleep")
    monkeypatch.setattr(config, "millet_timeout_seconds", lambda: 1)
    monkeypatch.setattr(meet_runner, "build_home_shim", lambda j, t: tmp_path)
    import os as _os
    monkeypatch.setattr(
        meet_runner, "_env_for_meet", lambda home, team: _os.environ.copy(),
    )

    log_path = tmp_path / "job.log"
    t0 = time.monotonic()
    rc = meet_runner.run_meet(["30"], "01JOB", "team-uuid", log_path)
    elapsed = time.monotonic() - t0

    assert rc == meet_runner.TIMEOUT_EXIT_CODE
    assert elapsed < 10, "kill must happen at the timeout, not after sleep 30"
    assert "TIMED OUT" in log_path.read_text()


# ── label POST: subprocess apply + queued finalize ──────────────────────────


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


def _seed_labelable_session(session_id: str = "01LBL") -> None:
    from vezir.server import queue

    queue.enqueue(session_id, github="alice", team_id="blink")
    queue.update_status(session_id, "needs_labeling")


def test_label_post_applies_via_subprocess_and_queues_finalize(
    client_and_token, tmp_data,
):
    client, token = client_and_token
    _seed_labelable_session()

    with patch(
        "vezir.server.meet_runner.apply_labels_json", return_value=0,
    ) as mock_apply, patch(
        "vezir.server.worker.enqueue_task", return_value=True,
    ) as mock_enqueue:
        resp = client.post(
            "/api/label/01LBL",
            headers=_bearer(token),
            json={"labels": {"REMOTE_0": "kasita"}},
        )
    assert resp.status_code == 200, resp.text
    assert mock_apply.called
    _args, kwargs = mock_apply.call_args
    assert kwargs.get("label_map") == {"REMOTE_0": "kasita"}
    assert kwargs.get("regenerate_summary") is False
    args, kwargs = mock_enqueue.call_args
    assert args == ("finalize_labels", "01LBL")
    assert kwargs.get("label_map") == {"REMOTE_0": "kasita"}


def test_label_post_surfaces_subprocess_failure(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_labelable_session()

    with patch(
        "vezir.server.meet_runner.apply_labels_json", return_value=1,
    ), patch("vezir.server.worker.enqueue_task") as mock_enqueue:
        resp = client.post(
            "/api/label/01LBL",
            headers=_bearer(token),
            json={"labels": {"REMOTE_0": "kasita"}},
        )
    assert resp.status_code == 502
    assert not mock_enqueue.called


def test_label_post_never_mutates_process_home(client_and_token, tmp_data):
    """The whole point of the subprocess boundary: applying labels must
    not touch os.environ['HOME'] in the server process."""
    import os as _os

    client, token = client_and_token
    _seed_labelable_session()
    home_before = _os.environ.get("HOME")

    observed: list = []

    def spy_apply(*a, **k):
        observed.append(_os.environ.get("HOME"))
        return 0

    with patch(
        "vezir.server.meet_runner.apply_labels_json", side_effect=spy_apply,
    ), patch("vezir.server.worker.enqueue_task", return_value=True):
        client.post(
            "/api/label/01LBL",
            headers=_bearer(token),
            json={"labels": {"REMOTE_0": "kasita"}},
        )
    assert observed == [home_before]
    assert _os.environ.get("HOME") == home_before


# ── uploads: disconnect cleanup, PATCH lock, orphan sweep ───────────────────


def test_upload_cleanup_on_non_http_exception(tmp_data):
    """A mid-upload client disconnect raises ClientDisconnect (NOT an
    HTTPException); the cleanup must still remove the partial session
    dir instead of leaking it forever."""
    from fastapi.testclient import TestClient

    from vezir import config
    from vezir.server import auth, uploads
    from vezir.server.app import create_app

    token = auth.issue("alice")
    # raise_server_exceptions=False: the simulated failure is a plain
    # Exception, which TestClient would otherwise re-raise into the test.
    client = TestClient(
        create_app(), follow_redirects=False, raise_server_exceptions=False,
    )

    with patch.object(
        uploads, "_validate_magic",
        side_effect=RuntimeError("simulated disconnect-class failure"),
    ):
        resp = client.post(
            "/upload",
            headers=_bearer(token),
            files={"audio": ("m.wav", b"RIFF" + b"\x00" * 100, "audio/wav")},
        )
    assert resp.status_code == 500
    # No orphan session dir was left behind.
    leftovers = list(config.sessions_dir().glob("*")) if config.sessions_dir().exists() else []
    assert leftovers == [], f"orphan session dirs: {leftovers}"


def test_resumable_patch_conflicts_when_chunk_in_flight(client_and_token, tmp_data):
    """A second concurrent PATCH for the same upload id must 409 instead
    of interleaving writes into the same .part file."""
    from vezir.server import uploads

    client, token = client_and_token

    create = client.post(
        "/upload/resumable",
        headers={
            **_bearer(token),
            "Upload-Length": "1000",
            "Upload-Filename": "m.wav",
            "Upload-Content-Type": "audio/wav",
        },
    )
    assert create.status_code == 201, create.text
    upload_id = create.json()["upload_id"]

    # Simulate an in-flight chunk by holding the lock.
    lock = uploads._patch_lock(upload_id)
    assert lock.acquire(blocking=False)
    try:
        resp = client.patch(
            f"/upload/resumable/{upload_id}",
            headers={**_bearer(token), "Upload-Offset": "0"},
            content=b"RIFF" + b"\x00" * 96,
        )
        assert resp.status_code == 409
        assert "in flight" in resp.json()["detail"]
    finally:
        lock.release()


def test_sweep_reclaims_orphan_part_files(tmp_data):
    """A .part with no meta sidecar (crash between replace() and unlink)
    must be reclaimed once older than the TTL."""
    from vezir import config
    from vezir.server import uploads

    tmp = config.uploads_tmp_dir()
    tmp.mkdir(parents=True, exist_ok=True)
    orphan = tmp / "01ORPHAN.part"
    orphan.write_bytes(b"\x00" * 10)

    # Young orphan survives.
    assert uploads.sweep_abandoned_uploads() == 0
    assert orphan.exists()

    # Old orphan is swept.
    removed = uploads.sweep_abandoned_uploads(
        now=time.time() + uploads.RESUMABLE_TTL_SEC + 10
    )
    assert removed == 1
    assert not orphan.exists()
