"""v0.7.0 multi-team isolation tests.

These tests are the CENTRAL security property of the multi-team model.
They prove:

* A token whose handle is not a member of team B cannot see team B's
  sessions via any API.
* Cross-team requests return 404 (not 403) where the test was set up
  with a valid X-Team-Id of a team the user IS in but is asking about
  someone else's session; missing/invalid X-Team-Id returns 400 / 403
  respectively at the auth layer.
* queue.list_recent's visibility filter strictly partitions sessions
  by team_id; the auth dependency is the only gate that determines
  which team_id a request sees.

v0.7.0 changes from v0.6.x:

* Tokens are no longer team-scoped.  The auth shim in conftest adds
  a membership row whenever a test issues a token, so the same human
  can be a member of multiple teams.
* The legacy "token without team_id" test was deleted; v0.7.0 tokens
  don't have team_id at all.
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
        yield Path(d)


@pytest.fixture
def client_factory(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    def _make():
        return TestClient(create_app(), follow_redirects=False)

    return _make


def _headers(token: str, team: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Team-Id": team,
    }


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


def _issue_for(github: str, team: str) -> str:
    """Issue a token and add a membership for ``github`` in ``team``.

    Wraps the shimmed auth.issue but pins the team_id so the membership
    lands in the team we care about (instead of the shim's default
    'blink').
    """
    from vezir.server import auth
    return auth.issue(github, team_id=team)


# ── Migration seeded both teams; verify they exist ──────────────────────────


def test_migration_seeds_blink_and_twentyone(client_factory):
    """Confirm the seed migration ran and the two teams exist."""
    client_factory()  # triggers create_app() -> run_pending_migrations()
    from vezir.server import queue
    # v0.7.4: teams keyed by uuid; slug is the human identifier.
    teams = {t["slug"]: t for t in queue.list_teams()}
    assert "blink" in teams
    assert "twentyone" in teams
    assert teams["blink"]["name"] == "Blink"
    assert teams["twentyone"]["name"] == "Twentyone"


# ── Upload routes the session to the X-Team-Id team ───────────────────────


def test_upload_routes_to_team_from_header(client_factory):
    client = client_factory()
    from vezir.server import queue
    tok = _issue_for("alice", "blink")

    resp = client.post(
        "/upload",
        headers=_headers(tok, "blink"),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    row = queue.get(sid)
    assert row["team_id"] == queue.get_team("blink")["id"]


def test_upload_routes_to_twentyone_when_header_says_so(client_factory):
    client = client_factory()
    from vezir.server import queue
    tok = _issue_for("alice", "twentyone")

    resp = client.post(
        "/upload",
        headers=_headers(tok, "twentyone"),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    row = queue.get(sid)
    assert row["team_id"] == queue.get_team("twentyone")["id"]


# ── /api/sessions visibility filter ────────────────────────────────────────


def test_api_sessions_shows_only_own_team(client_factory):
    client = client_factory()
    from vezir.server import queue

    # alice is a member of both teams (one token covers both via
    # X-Team-Id).
    tok = _issue_for("alice", "blink")
    _ = _issue_for("alice", "twentyone")  # adds another membership

    # Seed: one job per team.
    queue.enqueue("01BLINK1", "alice", team_id="blink", title="blink-meeting")
    queue.enqueue("01TWENT1", "alice", team_id="twentyone", title="twentyone-meeting")

    # X-Team-Id: blink -> only blink session.
    r1 = client.get("/api/sessions", headers=_headers(tok, "blink"))
    assert r1.status_code == 200
    blink_ids = {s["id"] for s in r1.json()["sessions"]}
    assert "01BLINK1" in blink_ids
    assert "01TWENT1" not in blink_ids

    # X-Team-Id: twentyone -> only twentyone session.
    r2 = client.get("/api/sessions", headers=_headers(tok, "twentyone"))
    assert r2.status_code == 200
    twentyone_ids = {s["id"] for s in r2.json()["sessions"]}
    assert "01TWENT1" in twentyone_ids
    assert "01BLINK1" not in twentyone_ids


def test_api_sessions_personal_still_works_within_team(client_factory):
    """Personal sessions are visible to their owner within the team;
    other team members see only the non-personal ones; non-members
    see nothing."""
    client = client_factory()
    from vezir.server import queue

    alice_tok = _issue_for("alice", "blink")
    bob_tok = _issue_for("bob", "blink")
    carol_tok = _issue_for("carol", "twentyone")

    queue.enqueue(
        "01APRIV", "alice", team_id="blink",
        title="alice priv", personal=True,
    )
    queue.enqueue(
        "01ATEAM", "alice", team_id="blink",
        title="alice team", personal=False,
    )

    # Alice sees both her personal + team sessions.
    ids_alice = {
        s["id"]
        for s in client.get(
            "/api/sessions", headers=_headers(alice_tok, "blink"),
        ).json()["sessions"]
    }
    assert ids_alice == {"01APRIV", "01ATEAM"}

    # Bob (blink) sees only the team session.
    ids_bob = {
        s["id"]
        for s in client.get(
            "/api/sessions", headers=_headers(bob_tok, "blink"),
        ).json()["sessions"]
    }
    assert ids_bob == {"01ATEAM"}

    # Carol (twentyone) is not a member of blink -> 403.
    r_carol = client.get(
        "/api/sessions", headers=_headers(carol_tok, "blink"),
    )
    assert r_carol.status_code == 403


# ── Cross-team session-detail / artifact access returns 404 ────────────────


def test_api_session_detail_cross_team_returns_404(client_factory):
    """If the caller is a member of team B but the session lives in
    team A, the request looks like 'session not found' to the caller.
    """
    client = client_factory()
    from vezir.server import queue

    tok = _issue_for("alice", "blink")
    _ = _issue_for("alice", "twentyone")

    queue.enqueue("01BLINKX", "alice", team_id="blink", title="blink")

    # blink scope sees it
    r1 = client.get("/api/sessions/01BLINKX", headers=_headers(tok, "blink"))
    assert r1.status_code == 200

    # twentyone scope: same human, but session is in a different team
    # -> 404 (not 403, to avoid leaking existence)
    r2 = client.get(
        "/api/sessions/01BLINKX", headers=_headers(tok, "twentyone"),
    )
    assert r2.status_code == 404


def test_api_session_detail_unknown_session_404(client_factory):
    """Unknown session id returns 404 regardless of team."""
    client = client_factory()
    tok = _issue_for("alice", "blink")
    r = client.get(
        "/api/sessions/01NOPE0000000000000000000",
        headers=_headers(tok, "blink"),
    )
    assert r.status_code == 404


# ── Cross-team share is rejected ───────────────────────────────────────────


def test_share_cross_team_returns_404(client_factory):
    """A twentyone-scoped request cannot share a blink-owned personal session."""
    client = client_factory()
    from vezir.server import queue
    queue.enqueue(
        "01PRIV2", "alice", team_id="blink",
        title="blink priv", personal=True,
    )

    tok = _issue_for("alice", "twentyone")
    r = client.post(
        "/api/sessions/01PRIV2/share",
        headers=_headers(tok, "twentyone"),
    )
    assert r.status_code == 404


# ── Cross-team retry-summary is rejected ────────────────────────────────────


def test_retry_summary_cross_team_returns_404(client_factory):
    client = client_factory()
    from vezir.server import queue
    queue.enqueue("01DONE", "alice", team_id="blink", title="blink done")
    queue.update_status("01DONE", "done", summary_error="LLM hiccup")

    tok = _issue_for("alice", "twentyone")
    r = client.post(
        "/api/sessions/01DONE/retry-summary",
        headers=_headers(tok, "twentyone"),
    )
    assert r.status_code == 404


# ── Non-member requests are 403 ─────────────────────────────────────────────


def test_non_member_request_is_403(client_factory):
    """A token whose handle is not in the requested team's memberships
    is rejected at the auth layer with 403, before any visibility check.
    """
    client = client_factory()
    tok = _issue_for("alice", "blink")
    # alice is NOT in twentyone here (we only added blink membership)
    r = client.get("/api/sessions", headers=_headers(tok, "twentyone"))
    assert r.status_code == 403


def test_missing_x_team_id_is_400(client_factory):
    """v0.7.0: team-scoped endpoints require the X-Team-Id header."""
    client = client_factory()
    tok = _issue_for("alice", "blink")
    r = client.get(
        "/api/sessions",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400
    assert "X-Team-Id" in r.text


# ── queue.list_recent guard rails ───────────────────────────────────────────


def test_list_recent_rejects_viewer_github_without_team(client_factory):
    """The library function refuses the legacy 1-arg shape."""
    client_factory()
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
