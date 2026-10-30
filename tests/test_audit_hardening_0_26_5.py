"""Smaller server fixes from the 2026-10-30 security audit (vezir 0.26.5).

* M-9   startup warns when VEZIR_PUBLIC_URL is unset
* M-10  admin / session-management / resumable-HEAD routes are rate-limited
* M-13  summary_template validated at every upload boundary
* L-4   malformed NIP-98 tag values are a 401, never a 500
* L-22  the transient ``.{id}.labels.json`` is never picked as an artifact
* L-24  /artifact/{id}/{name} refuses directories, symlinks, dotfiles and
        transient working files
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import time
import wave
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


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


def _hdr(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "X-Team-Id": "blink"}


# ── M-9 ──────────────────────────────────────────────────────────────────────


def test_startup_warns_without_public_url(tmp_data, monkeypatch):
    """create_app() resets the root handlers (so caplog can't see it);
    capture the 'vezir' logger's warnings directly."""
    import logging

    from vezir.server.app import create_app

    seen: list[str] = []
    monkeypatch.setattr(
        logging.getLogger("vezir"), "warning", lambda msg, *a, **k: seen.append(msg % a),
    )
    monkeypatch.delenv("VEZIR_PUBLIC_URL", raising=False)
    create_app()
    assert any("VEZIR_PUBLIC_URL is not set" in m for m in seen)

    seen.clear()
    monkeypatch.setenv("VEZIR_PUBLIC_URL", "https://vezir.example.test")
    create_app()
    assert not any("VEZIR_PUBLIC_URL is not set" in m for m in seen)


# ── M-10 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/admin/teams"),
        ("POST", "/admin/teams"),
        ("GET", "/admin/teams/{team_id}"),
        ("PATCH", "/admin/teams/{team_id}"),
        ("GET", "/admin/teams/{team_id}/members"),
        ("POST", "/admin/teams/{team_id}/members"),
        ("DELETE", "/admin/teams/{team_id}/members/{github}"),
        ("DELETE", "/admin/teams/{team_id}"),
        ("GET", "/api/auth/sessions"),
        ("POST", "/api/auth/sessions/{sid}/revoke"),
        ("POST", "/api/auth/sessions/revoke-all"),
        ("HEAD", "/upload/resumable/{upload_id}"),
    ],
)
def test_privileged_routes_carry_the_api_limiter(tmp_data, monkeypatch, method, path):
    """Every privileged route must actually 429, across FastAPI route layouts."""
    from fastapi.testclient import TestClient

    from vezir.server import ratelimit
    from vezir.server.app import create_app

    monkeypatch.delenv("VEZIR_DISABLE_RATELIMIT", raising=False)
    monkeypatch.setattr(ratelimit, "_LIMITERS", {
        **ratelimit._LIMITERS,
        "api": ratelimit._Limiter(capacity=5, window_sec=60.0),
        "auth-fail": ratelimit._Limiter(capacity=1000, window_sec=60.0),
    })
    client = TestClient(create_app(), follow_redirects=False)
    url = path.format(team_id="blink", github="alice", sid="sid", upload_id="upload")
    codes = [client.request(method, url).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_admin_route_actually_429s_when_sprayed(tmp_data, monkeypatch):
    from fastapi.testclient import TestClient

    from vezir.server import ratelimit
    from vezir.server.app import create_app

    monkeypatch.delenv("VEZIR_DISABLE_RATELIMIT", raising=False)
    # Fresh limiter so other tests' traffic doesn't matter.
    monkeypatch.setattr(ratelimit, "_LIMITERS", {
        **ratelimit._LIMITERS,
        "api": ratelimit._Limiter(capacity=5, window_sec=60.0),
        "auth-fail": ratelimit._Limiter(capacity=1000, window_sec=60.0),
    })
    client = TestClient(create_app(), follow_redirects=False)
    codes = [client.get("/admin/teams").status_code for _ in range(8)]
    assert codes[:5] == [401] * 5
    assert 429 in codes[5:]


# ── M-13 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../x", "x/../../**", "UPPER", "a b", "x" * 65, "-x", "*.md"])
def test_upload_rejects_bad_summary_template(client, tmp_data, bad):
    from vezir.server import auth, queue

    tok = auth.issue("alice")
    r = client.post(
        "/upload", headers=_hdr(tok),
        files={"audio": ("a.wav", _wav(), "audio/wav")},
        data={"summary_template": bad},
    )
    assert r.status_code == 422, r.text
    assert queue.list_recent(limit=5) == []
    assert not list((tmp_data / "sessions").iterdir()) if (tmp_data / "sessions").exists() else True

    headers = {
        **_hdr(tok), "Upload-Length": "100",
        "Upload-Filename": "a.wav", "Upload-Content-Type": "audio/wav",
    }
    r = client.post("/upload/resumable", headers=headers, data={"summary_template": bad})
    assert r.status_code == 422, r.text


def test_upload_accepts_good_summary_template(client, tmp_data):
    from vezir.server import auth, queue

    tok = auth.issue("alice")
    r = client.post(
        "/upload", headers=_hdr(tok),
        files={"audio": ("a.wav", _wav(), "audio/wav")},
        data={"summary_template": "iteration-plan"},
    )
    assert r.status_code == 200, r.text
    assert queue.get(r.json()["session_id"])["summary_template"] == "iteration-plan"


def test_template_rule_is_shared():
    from vezir import presets
    from vezir.server import sessions

    assert sessions._TEMPLATE_RE is presets.TEMPLATE_RE


# ── L-4 ──────────────────────────────────────────────────────────────────────


def _nip98_header(event: dict) -> str:
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


@pytest.mark.parametrize("bad_tag", [["u", 5], ["u", ["x"]], ["method", None], ["method", 7]])
def test_malformed_nip98_tag_values_are_401_not_500(client, bad_tag):
    pubkey = "ab" * 32
    event = {
        "pubkey": pubkey, "created_at": int(time.time()), "kind": 27235,
        "tags": [bad_tag, ["u", "http://testserver/api/auth/nostr/login"], ["method", "POST"]],
        "content": "",
    }
    serialized = json.dumps(
        [0, pubkey, event["created_at"], 27235, event["tags"], ""],
        separators=(",", ":"), ensure_ascii=False,
    )
    event["id"] = hashlib.sha256(serialized.encode()).hexdigest()
    event["sig"] = "00" * 64
    r = client.post("/api/auth/nostr/login", headers={"Authorization": _nip98_header(event)})
    assert r.status_code == 401, r.text


# ── L-22 ─────────────────────────────────────────────────────────────────────


def test_find_artifacts_never_picks_transient_label_map(tmp_data):
    from vezir.server.worker import _find_artifacts

    sdir = tmp_data / "sessions" / "01X"
    sdir.mkdir(parents=True)
    (sdir / ".01X.labels.json").write_text("{}")  # sorts first, matches *.json
    assert _find_artifacts(sdir).get("json") is None
    (sdir / "01X.json").write_text("{}")
    assert _find_artifacts(sdir)["json"] == "01X.json"


# ── L-24 ─────────────────────────────────────────────────────────────────────


def test_artifact_route_refuses_dirs_symlinks_dotfiles_and_transients(client, tmp_data):
    from vezir.server import auth

    tok = auth.issue("alice")
    r = client.post("/upload", headers=_hdr(tok), files={"audio": ("a.wav", _wav(), "audio/wav")})
    sid = r.json()["session_id"]
    sdir = tmp_data / "sessions" / sid
    (sdir / "clips").mkdir()
    secret = tmp_data / "secret.txt"
    secret.write_text("s3cret")
    (sdir / "link.txt").symlink_to(secret)
    (sdir / f"{sid}.concat.txt").write_text("file 'x'")
    (sdir / f".{sid}.labels.json").write_text("{}")

    for name, want in [
        ("clips", (400, 404)),
        (".", (400, 404)),
        ("link.txt", (404,)),
        (f"{sid}.concat.txt", (400,)),
        (f".{sid}.labels.json", (400,)),
        (f"{sid}.wav", (200,)),
    ]:
        r = client.get(f"/artifact/{sid}/{name}", headers=_hdr(tok))
        assert r.status_code in want, (name, r.status_code)
        if name == "link.txt":
            assert b"s3cret" not in r.content


# ── L-9 (0.27.0): vezir secrets do not reach the millet subprocess ───────────


def test_millet_env_drops_vezir_secrets_but_keeps_pipeline_vars(tmp_data, monkeypatch):
    from vezir.server import meet_runner

    monkeypatch.setenv("VEZIR_GOOGLE_CLIENT_SECRET_FILE", "/etc/x")
    monkeypatch.setenv("VEZIR_PUBLIC_URL", "https://v.example")
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", "/etc/ca.pem")
    monkeypatch.setenv("VEZIR_MILLET_BIN", "/opt/millet")
    monkeypatch.setenv("VENICE_API_KEY", "v")
    monkeypatch.setenv("NEAR_AI_API_KEY", "n")
    monkeypatch.setenv("SOME_APP_SECRET", "s")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("HF_TOKEN", "hf")
    monkeypatch.setenv("TINFOIL_API_KEY", "tk")
    monkeypatch.setenv("MILLET_OLLAMA_MODEL", "qwen")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("LANG", "C.UTF-8")

    env = meet_runner._env_for_meet(Path("/tmp/shim"), "team-uuid")

    for gone in ("VEZIR_GOOGLE_CLIENT_SECRET_FILE", "VEZIR_PUBLIC_URL",
                 "VEZIR_CADDY_ROOT_CERT_PATH", "VENICE_API_KEY", "NEAR_AI_API_KEY",
                 "SOME_APP_SECRET", "DB_PASSWORD"):
        assert gone not in env, gone
    for kept in ("HF_TOKEN", "TINFOIL_API_KEY", "MILLET_OLLAMA_MODEL", "GIT_TERMINAL_PROMPT",
                 "CUDA_VISIBLE_DEVICES", "LANG", "PATH", "VEZIR_MILLET_BIN"):
        assert kept in env, kept
    assert env["HOME"] == "/tmp/shim"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "XDG_CONFIG_HOME" not in env


# ── L-3 (0.27.0): bearer spraying is bounded per IP ──────────────────────────


def test_bearer_spray_hits_per_ip_auth_fail_bucket(tmp_data, monkeypatch):
    """Each random bearer used to get its own api bucket (keyed on the token
    hash), so a spray was unmetered.  Failed auths are now charged to the
    IP: after the budget the answer is 429, and a VALID token from the same
    IP keeps working (the bucket is only consumed on failure)."""
    from fastapi.testclient import TestClient

    from vezir.server import auth, ratelimit
    from vezir.server.app import create_app

    monkeypatch.delenv("VEZIR_DISABLE_RATELIMIT", raising=False)
    monkeypatch.setattr(ratelimit, "_LIMITERS", {
        **ratelimit._LIMITERS,
        "auth-fail": ratelimit._Limiter(capacity=5, window_sec=60.0),
        "api": ratelimit._Limiter(capacity=1000, window_sec=60.0),
    })
    client = TestClient(create_app(), follow_redirects=False)
    good = auth.issue("alice")

    codes = [
        client.get("/api/me", headers={"Authorization": f"Bearer vzr_{i:040d}"}).status_code
        for i in range(8)
    ]
    assert codes[:5] == [401] * 5
    assert codes[5:] == [429] * 3
    # A valid token from the same IP is unaffected.
    assert client.get("/api/me", headers={"Authorization": f"Bearer {good}"}).status_code == 200
    # Same for team-scoped and admin routes.
    bad = {"Authorization": "Bearer vzr_x", "X-Team-Id": "blink"}
    assert client.get("/api/sessions", headers=bad).status_code == 429
    assert client.get("/admin/teams", headers={"Authorization": "Bearer vzr_x"}).status_code == 429


def test_auth_fail_bucket_not_consumed_by_successful_auth(tmp_data, monkeypatch):
    from fastapi.testclient import TestClient

    from vezir.server import auth, ratelimit
    from vezir.server.app import create_app

    monkeypatch.delenv("VEZIR_DISABLE_RATELIMIT", raising=False)
    monkeypatch.setattr(ratelimit, "_LIMITERS", {
        **ratelimit._LIMITERS,
        "auth-fail": ratelimit._Limiter(capacity=2, window_sec=60.0),
        "api": ratelimit._Limiter(capacity=1000, window_sec=60.0),
    })
    client = TestClient(create_app(), follow_redirects=False)
    good = auth.issue("alice")
    for _ in range(10):
        assert client.get("/api/me", headers={"Authorization": f"Bearer {good}"}).status_code == 200
    assert client.get("/api/me", headers={"Authorization": "Bearer vzr_bad"}).status_code == 401
