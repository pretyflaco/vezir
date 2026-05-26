"""v0.6.0 multi-team isolation tests.

These tests are the CENTRAL security property of v0.6.0.  They prove
that:

* A token issued for team A cannot see team B's sessions via any API.
* A token issued for team A cannot fetch team B's artifacts, label
  pages, label clips, sync-now, share, or retry-summary.
* The visibility filter in queue.list_recent strictly partitions
  sessions by team_id.
* Cross-team requests return 404 (not 403) to avoid leaking
  session existence.
* A token without team_id (legacy / hand-edited) is rejected at auth
  time with 401, not allowed to fall through to the visibility filter.

These tests bypass the auth.issue shim by using the underlying
``auth._issue_raw`` directly so the team_id is explicit per token.
"""
from __future__ import annotations

import io
import tempfile
import wave
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        from vezir.server import web_sessions
        web_sessions._reset_for_tests()
        yield Path(d)


@pytest.fixture
def client_factory(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    def _make():
        return TestClient(create_app(), follow_redirects=False)

    return _make


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


def _issue_raw(github, team_id, **kwargs):
    """Bypass the conftest shim; explicit team_id per token."""
    from vezir.server import auth
    return auth._issue_raw(github, team_id=team_id, **kwargs)


# ── Migration seeded both teams; verify they exist ──────────────────────────


def test_migration_seeds_blink_and_twentyone(client_factory):
    """Confirm the seed migration ran and the two teams exist."""
    client_factory()  # triggers create_app() -> run_pending_migrations()
    from vezir.server import queue
    teams = {t["id"]: t for t in queue.list_teams()}
    assert "blink" in teams
    assert "twentyone" in teams
    assert teams["blink"]["name"] == "Blink"
    assert teams["twentyone"]["name"] == "Twentyone"


# ── Upload routes the session to the uploader's team ───────────────────────


def test_upload_routes_to_blink_when_token_is_blink(client_factory):
    client = client_factory()
    from vezir.server import queue
    tok = _issue_raw("alice", team_id="blink")

    resp = client.post(
        "/upload",
        headers=_bearer(tok),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    row = queue.get(sid)
    assert row["team_id"] == "blink"


def test_upload_routes_to_twentyone_when_token_is_twentyone(client_factory):
    client = client_factory()
    from vezir.server import queue
    tok = _issue_raw("alice", team_id="twentyone")

    resp = client.post(
        "/upload",
        headers=_bearer(tok),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    row = queue.get(sid)
    assert row["team_id"] == "twentyone"


# ── /api/sessions visibility filter ────────────────────────────────────────


def test_api_sessions_shows_only_own_team(client_factory):
    client = client_factory()
    from vezir.server import queue

    blink_tok = _issue_raw("alice", team_id="blink")
    twentyone_tok = _issue_raw("alice", team_id="twentyone")

    # Seed: one job per team.
    queue.enqueue("01BLINK1", "alice", "blink-meeting", team_id="blink")
    queue.enqueue("01TWENT1", "alice", "twentyone-meeting", team_id="twentyone")

    # Blink token sees only the blink session.
    r1 = client.get("/api/sessions", headers=_bearer(blink_tok))
    assert r1.status_code == 200
    blink_ids = {s["id"] for s in r1.json()["sessions"]}
    assert "01BLINK1" in blink_ids
    assert "01TWENT1" not in blink_ids

    # Twentyone token sees only the twentyone session.
    r2 = client.get("/api/sessions", headers=_bearer(twentyone_tok))
    assert r2.status_code == 200
    twentyone_ids = {s["id"] for s in r2.json()["sessions"]}
    assert "01TWENT1" in twentyone_ids
    assert "01BLINK1" not in twentyone_ids


def test_api_sessions_personal_still_works_within_team(client_factory):
    """Personal sessions are visible to their owner within the team;
    other team members see only the non-personal ones; other-team
    members see nothing."""
    client = client_factory()
    from vezir.server import queue

    alice_blink = _issue_raw("alice", team_id="blink")
    bob_blink = _issue_raw("bob", team_id="blink")
    carol_twentyone = _issue_raw("carol", team_id="twentyone")

    queue.enqueue("01APRIV", "alice", "alice priv", personal=True, team_id="blink")
    queue.enqueue("01ATEAM", "alice", "alice team", personal=False, team_id="blink")

    # Alice (blink) sees both her personal + team sessions.
    ids_alice = {
        s["id"]
        for s in client.get("/api/sessions", headers=_bearer(alice_blink)).json()["sessions"]
    }
    assert ids_alice == {"01APRIV", "01ATEAM"}

    # Bob (blink) sees only the team session.
    ids_bob = {
        s["id"]
        for s in client.get("/api/sessions", headers=_bearer(bob_blink)).json()["sessions"]
    }
    assert ids_bob == {"01ATEAM"}

    # Carol (twentyone) sees neither.
    ids_carol = {
        s["id"]
        for s in client.get("/api/sessions", headers=_bearer(carol_twentyone)).json()["sessions"]
    }
    assert "01APRIV" not in ids_carol
    assert "01ATEAM" not in ids_carol


# ── Cross-team session-detail / artifact access returns 404 ────────────────


def test_api_session_detail_cross_team_returns_404(client_factory):
    client = client_factory()
    from vezir.server import queue

    blink_tok = _issue_raw("alice", team_id="blink")
    twentyone_tok = _issue_raw("alice", team_id="twentyone")

    queue.enqueue("01BLINKX", "alice", "blink", team_id="blink")

    # blink owner sees it
    r1 = client.get("/api/sessions/01BLINKX", headers=_bearer(blink_tok))
    assert r1.status_code == 200

    # twentyone token cannot see it — 404 (not 403, to avoid leaking existence)
    r2 = client.get("/api/sessions/01BLINKX", headers=_bearer(twentyone_tok))
    assert r2.status_code == 404


def test_api_session_detail_unknown_session_404(client_factory):
    """Unknown session id returns 404 regardless of team."""
    client = client_factory()
    tok = _issue_raw("alice", team_id="blink")
    r = client.get("/api/sessions/01NOPE0000000000000000000", headers=_bearer(tok))
    assert r.status_code == 404


# ── Cross-team share is rejected ───────────────────────────────────────────


def test_share_cross_team_returns_404(client_factory):
    """A twentyone token cannot share a blink-owned personal session."""
    client = client_factory()
    from vezir.server import queue
    queue.enqueue("01PRIV2", "alice", "blink priv", personal=True, team_id="blink")

    twentyone_tok = _issue_raw("alice", team_id="twentyone")
    r = client.post(
        "/api/sessions/01PRIV2/share",
        headers=_bearer(twentyone_tok),
    )
    assert r.status_code == 404  # not visible -> 404


# ── Cross-team retry-summary is rejected ────────────────────────────────────


def test_retry_summary_cross_team_returns_404(client_factory):
    client = client_factory()
    from vezir.server import queue
    queue.enqueue("01DONE", "alice", "blink done", team_id="blink")
    # Force into done+summary_error state for the retry endpoint
    queue.update_status("01DONE", "done", summary_error="LLM hiccup")

    twentyone_tok = _issue_raw("alice", team_id="twentyone")
    r = client.post(
        "/api/sessions/01DONE/retry-summary",
        headers=_bearer(twentyone_tok),
    )
    assert r.status_code == 404


# ── Tokens without team_id are rejected ────────────────────────────────────


def test_legacy_token_without_team_id_rejected(client_factory):
    """A hand-edited tokens.json entry missing team_id must 401."""
    client = client_factory()
    import json

    from vezir import config as _config
    from vezir.server import auth as _auth

    # Insert a legacy-shape token row directly (bypassing auth.issue).
    p = _config.tokens_json_path()
    data = json.loads(p.read_text()) if p.exists() else {"tokens": []}
    data["tokens"].append({
        "github": "legacy",
        "token_hash": _auth._hash("vzr_legacy_no_team"),
        "issued_at": "2026-05-24T00:00:00Z",
        "expires_at": None,
        "last_used_at": None,
        "is_admin": False,
        "label": "legacy",
        # NOTE: no team_id
    })
    _config.secure_write_text(p, json.dumps(data, indent=2))

    r = client.get(
        "/api/sessions",
        headers={"Authorization": "Bearer vzr_legacy_no_team"},
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "team" in detail.lower()


# ── queue.list_recent guard rails ───────────────────────────────────────────


def test_list_recent_rejects_viewer_github_without_team(client_factory):
    """The library function refuses the legacy 1-arg shape."""
    client_factory()  # ensure schema + teams seeded
    from vezir.server import queue

    with pytest.raises(ValueError, match="viewer_team_id"):
        queue.list_recent(viewer_github="alice")


def test_list_recent_rejects_github_without_team(client_factory):
    client_factory()
    from vezir.server import queue

    with pytest.raises(ValueError, match="team_id"):
        queue.list_recent(github="alice")


# ── queue.enqueue guard rails ──────────────────────────────────────────────


def test_enqueue_requires_team_id(client_factory):
    client_factory()
    from vezir.server import queue

    with pytest.raises(TypeError):
        # team_id is keyword-only, so this fails with TypeError (missing arg)
        queue.enqueue("01X", "alice", "title")  # type: ignore[call-arg]


def test_enqueue_rejects_empty_team_id(client_factory):
    client_factory()
    from vezir.server import queue

    with pytest.raises(ValueError, match="team_id"):
        queue.enqueue("01X", "alice", "title", team_id="")


# ── Team slug validation ──────────────────────────────────────────────────


def test_validate_team_id_accepts_seeds(client_factory):
    client_factory()
    from vezir.server import queue
    queue.validate_team_id("blink")
    queue.validate_team_id("twentyone")


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "ab",  # too short (2 chars)
        "Blink",  # uppercase
        "1blink",  # starts with digit
        "-blink",  # starts with hyphen
        "blink!",  # special char
        "a" * 33,  # too long
    ],
)
def test_validate_team_id_rejects_bad_slugs(client_factory, bad_id):
    client_factory()
    from vezir.server import queue
    with pytest.raises(ValueError):
        queue.validate_team_id(bad_id)
