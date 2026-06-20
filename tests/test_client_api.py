"""Tests for the new vezir.client.api module.

Two layers:

1. **Unit tests** against an httpx.MockTransport — fast, no FastAPI
   spin-up, exercises the Result-tag plumbing, Session parsing, and
   per-endpoint URL/header construction.

2. **End-to-end tests** against the live FastAPI app via TestClient.
   These confirm the client speaks the same JSON the server actually
   emits today (catches drift if e.g. an endpoint changes its payload
   shape under the client's feet).
"""
from __future__ import annotations

import httpx
import pytest

from vezir.client.api import (
    ApiResult,
    LabelInfo,
    Session,
    VezirClient,
)

# ─── Result-tag plumbing ─────────────────────────────────────────────────────


# ─── 401 re-auth hinting ─────────────────────────────────────────────────────


def test_is_auth_error():
    assert ApiResult.http(401, "invalid bearer token").is_auth_error()
    assert not ApiResult.http(403, "forbidden").is_auth_error()
    assert not ApiResult.success({}).is_auth_error()


def test_401_message_hints_nostr_login_when_session_active(monkeypatch, tmp_path):
    """A 401 on an active nostr session points the user at `vezir login`."""
    import json as _json
    from pathlib import Path

    from vezir import config as server_config

    cfgdir = tmp_path / ".config" / "vezir"
    cfgdir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    server_config.secure_write_text(
        cfgdir / "teams.json",
        _json.dumps({
            "teams": [{"id": "blink", "url": "https://x", "token": "eyJ.a.b",
                       "auth": "nostr", "npub": "ab"}],
            "active": "blink",
        }),
    )
    msg = ApiResult.http(401, "invalid bearer token").error_message()
    assert "vezir login" in msg
    assert "expired" in msg


def test_401_message_generic_for_bearer(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    msg = ApiResult.http(401, "invalid bearer token").error_message()
    assert "VEZIR_TOKEN" in msg


# ─── CA / verify resolution ──────────────────────────────────────────────────


def test_resolve_verify_defaults_to_true_with_no_env(monkeypatch):
    """No CA env vars -> httpx's certifi-backed default (verify=True)."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    from vezir.client.api import VezirClient
    assert VezirClient._resolve_verify(None) is True


def _write_test_ca(path):
    """Write a real self-signed CA PEM (so load_verify_locations succeeds)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vezir-api-test-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_resolve_verify_picks_up_ssl_cert_file(monkeypatch, tmp_path):
    """SSL_CERT_FILE -> an SSLContext that trusts the default store + that CA.

    (The internal CA is *appended*, never replacing the default store; the
    detailed CA-count assertions live in test_client_trust.py.)
    """
    import ssl
    pytest.importorskip("cryptography")
    ca = tmp_path / "ca.crt"
    _write_test_ca(ca)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    from vezir.client.api import VezirClient
    assert isinstance(VezirClient._resolve_verify(None), ssl.SSLContext)


def test_resolve_verify_picks_up_vezir_caddy_path(monkeypatch, tmp_path):
    """Vezir-specific env var (set by Caddy-deployed boxes)."""
    import ssl
    pytest.importorskip("cryptography")
    ca = tmp_path / "vezir-ca.crt"
    _write_test_ca(ca)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(ca))
    from vezir.client.api import VezirClient
    assert isinstance(VezirClient._resolve_verify(None), ssl.SSLContext)


def test_resolve_verify_explicit_overrides_env(monkeypatch, tmp_path):
    """Caller-passed verify wins even when env vars are set."""
    ca = tmp_path / "ca.crt"
    ca.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    from vezir.client.api import VezirClient
    assert VezirClient._resolve_verify(False) is False
    assert VezirClient._resolve_verify(True) is True
    assert VezirClient._resolve_verify("/some/other/path") == "/some/other/path"


def test_resolve_verify_ignores_nonexistent_file(monkeypatch):
    """SSL_CERT_FILE pointing at a missing file -> fall through to True
    rather than crash with a confusing httpx error later."""
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/definitely-not-here.crt")
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    from vezir.client.api import VezirClient
    assert VezirClient._resolve_verify(None) is True


def test_vezir_client_uses_resolved_verify(monkeypatch, tmp_path):
    """Constructor should call _resolve_verify and store the result."""
    import ssl
    pytest.importorskip("cryptography")
    ca = tmp_path / "ca.crt"
    _write_test_ca(ca)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    from vezir.client.api import VezirClient
    c = VezirClient("https://test", "vzr_x")
    assert isinstance(c._verify, ssl.SSLContext)


def test_apiresult_success_is_truthy():
    r = ApiResult.success({"hello": "world"})
    assert r.is_ok()
    assert r.unwrap() == {"hello": "world"}
    assert r.error_message() == ""


def test_apiresult_http_error_is_falsy_in_is_ok():
    r = ApiResult.http(404, "not found")
    assert not r.is_ok()
    with pytest.raises(RuntimeError, match="HTTP 404"):
        r.unwrap()
    assert "404" in r.error_message()


def test_apiresult_network_error_propagates_original_exception():
    boom = ConnectionError("tunnel down")
    r = ApiResult.network(boom)
    assert not r.is_ok()
    with pytest.raises(ConnectionError, match="tunnel down"):
        r.unwrap()
    assert "tunnel down" in r.error_message()


# ─── Session.from_dict ───────────────────────────────────────────────────────


def test_session_from_dict_handles_dict_artifacts():
    s = Session.from_dict({
        "id": "01ABC",
        "status": "done",
        "artifacts": {"summary": "summary.md", "transcript": "t.txt"},
    })
    assert s.id == "01ABC"
    assert s.artifacts == {"summary": "summary.md", "transcript": "t.txt"}
    assert s.is_terminal
    assert not s.is_personal


def test_session_from_dict_handles_json_string_artifacts():
    """Legacy /api/sessions returns artifacts as a JSON string."""
    s = Session.from_dict({
        "id": "01ABC",
        "status": "needs_labeling",
        "artifacts": '{"transcript": "t.txt"}',
    })
    assert s.artifacts == {"transcript": "t.txt"}
    assert not s.is_terminal
    assert s.is_active is False  # needs_labeling is not in active states


def test_session_from_dict_personal_flag():
    s = Session.from_dict({"id": "x", "status": "done", "personal": 1})
    assert s.is_personal


def test_session_from_dict_ignores_unknown_fields():
    """Server may add new fields; client should not blow up."""
    s = Session.from_dict({
        "id": "x", "status": "done", "future_field": "ignored",
    })
    assert s.id == "x"


def test_session_from_dict_malformed_artifacts_string():
    """Don't crash on invalid JSON in artifacts string."""
    s = Session.from_dict({"id": "x", "status": "done", "artifacts": "not json"})
    assert s.artifacts == {}


# ─── LabelInfo.from_dict ─────────────────────────────────────────────────────


def test_label_info_from_dict():
    info = LabelInfo.from_dict({
        "session_id": "01X",
        "status": "needs_labeling",
        "speakers": [
            {"id": "REMOTE_0", "channel": "system", "sample_text": "Hi all"},
        ],
        "team": ["alice", "bob"],
        "audio_available": True,
    })
    assert info.session_id == "01X"
    assert info.status == "needs_labeling"
    assert len(info.speakers) == 1
    assert info.speakers[0]["id"] == "REMOTE_0"
    assert info.team == ["alice", "bob"]
    assert info.audio_available


# ─── VezirClient against MockTransport ───────────────────────────────────────


def _client_with_transport(handler, team_id: str | None = "blink") -> VezirClient:
    """Wire a MockTransport into a VezirClient by monkeypatching httpx.Client.

    ``team_id`` defaults to ``"blink"`` so existing tests get a sensible
    ``X-Team-Id`` header without code changes.  Pass ``None`` to test
    the no-team flow (e.g. /api/me discovery).
    """
    transport = httpx.MockTransport(handler)
    client = VezirClient("https://test", "vzr_token", team_id=team_id)
    # Monkeypatch the httpx.Client reference the client uses.
    import vezir.client.api as api_mod
    orig = api_mod.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    api_mod._test_orig_client = orig
    api_mod.httpx.Client = factory
    return client


def _restore_httpx():
    import vezir.client.api as api_mod
    if hasattr(api_mod, "_test_orig_client"):
        api_mod.httpx.Client = api_mod._test_orig_client
        del api_mod._test_orig_client


@pytest.fixture
def mocked_client():
    yield _client_with_transport
    _restore_httpx()


def test_get_sessions_parses_list(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sessions"
        assert request.url.params.get("limit") == "50"
        assert request.headers["authorization"] == "Bearer vzr_token"
        return httpx.Response(200, json={"sessions": [
            {"id": "01A", "status": "done"},
            {"id": "01B", "status": "needs_labeling"},
        ]})

    client = mocked_client(handler)
    result = client.get_sessions(50)
    assert result.is_ok()
    sessions = result.ok
    assert len(sessions) == 2
    assert sessions[0].id == "01A"
    assert sessions[1].status == "needs_labeling"


def test_get_session_404_returns_http_error(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sessions/01MISSING"
        return httpx.Response(404, text="not found")

    client = mocked_client(handler)
    result = client.get_session("01MISSING")
    assert not result.is_ok()
    assert result.http_error == (404, "not found")


def test_share_with_team_posts_empty_body(mocked_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/sessions/01X/share"
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = mocked_client(handler)
    result = client.share_with_team("01X")
    assert result.is_ok()
    # Empty {} body for POSTs with no payload.
    assert seen["body"] == b"{}"


def test_retry_summary_with_preset_includes_body(mocked_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    client = mocked_client(handler)
    result = client.retry_summary("01X", preset="high-quality")
    assert result.is_ok()
    assert seen["path"] == "/api/sessions/01X/retry-summary"
    assert '"preset"' in seen["body"]
    assert "high-quality" in seen["body"]


def test_retry_summary_without_preset_sends_empty_object(mocked_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = mocked_client(handler)
    client.retry_summary("01X").unwrap()
    assert seen["body"] == b"{}"


def test_retry_summary_with_language_includes_body(mocked_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    client = mocked_client(handler)
    client.retry_summary("01X", language="de").unwrap()
    assert '"language"' in seen["body"]
    assert "de" in seen["body"]


def test_sync_now_without_meeting_type_sends_empty_object(mocked_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/session/01X/sync"
        seen["body"] = request.content
        return httpx.Response(200, json={"queued": True})

    client = mocked_client(handler)
    client.sync_now("01X").unwrap()
    # No override → empty {} body (auto-detect on the server).
    assert seen["body"] == b"{}"


def test_sync_now_with_meeting_type_includes_body(mocked_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/01X/sync"
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"queued": True})

    client = mocked_client(handler)
    client.sync_now("01X", meeting_type="post-scrum").unwrap()
    assert '"meeting_type"' in seen["body"]
    assert "post-scrum" in seen["body"]


def test_retry_summary_auto_language_omitted(mocked_client):
    """language='auto' means 'use detected' — don't send it (server keeps
    the primary summary)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = mocked_client(handler)
    client.retry_summary("01X", language="auto").unwrap()
    assert seen["body"] == b"{}"


def test_submit_labels_sends_labels_dict(mocked_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    client = mocked_client(handler)
    result = client.submit_labels("01X", {"REMOTE_0": "alice", "REMOTE_1": "bob"})
    assert result.is_ok()
    assert seen["path"] == "/api/label/01X"
    import json
    body = json.loads(seen["body"])
    assert body == {"labels": {"REMOTE_0": "alice", "REMOTE_1": "bob"}}


def test_delete_session_issues_delete(mocked_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        assert request.headers["x-team-id"] == "blink"
        return httpx.Response(200, json={"ok": True, "warning": None})

    client = mocked_client(handler)
    result = client.delete_session("01X")
    assert result.is_ok()
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/api/sessions/01X"


def test_delete_session_surfaces_warning(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "warning": "git copy remains"})

    client = mocked_client(handler)
    result = client.delete_session("01X")
    assert result.is_ok()
    assert result.ok["warning"] == "git copy remains"


def test_delete_session_propagates_403(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "not permitted"})

    client = mocked_client(handler)
    result = client.delete_session("01X")
    assert not result.is_ok()
    assert result.http_error[0] == 403


def test_get_team_returns_list(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/team"
        return httpx.Response(200, json={"team": ["alice", "bob"]})

    client = mocked_client(handler)
    result = client.get_team()
    assert result.unwrap() == ["alice", "bob"]


def test_download_artifact_returns_bytes(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/artifact/01X/summary.md"
        return httpx.Response(
            200,
            content=b"# Summary\n",
            headers={"content-type": "text/markdown"},
        )

    client = mocked_client(handler)
    result = client.download_artifact("01X", "summary.md")
    assert result.unwrap() == b"# Summary\n"


def test_save_artifact_writes_to_disk(mocked_client, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello")

    client = mocked_client(handler)
    dest = tmp_path / "out" / "summary.md"
    result = client.save_artifact("01X", "summary.md", dest)
    assert result.is_ok()
    assert result.ok == dest
    assert dest.read_bytes() == b"hello"


def test_get_label_info_parses_response(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/label/01X"
        return httpx.Response(200, json={
            "session_id": "01X",
            "status": "needs_labeling",
            "speakers": [{"id": "S0", "channel": "mic"}],
            "team": ["alice"],
            "audio_available": True,
        })

    client = mocked_client(handler)
    info = client.get_label_info("01X").unwrap()
    assert isinstance(info, LabelInfo)
    assert info.session_id == "01X"
    assert info.audio_available


def test_download_clip(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/label/01X/clip/REMOTE_0"
        return httpx.Response(
            200, content=b"RIFFwavedata", headers={"content-type": "audio/wav"},
        )

    client = mocked_client(handler)
    assert client.download_clip("01X", "REMOTE_0").unwrap() == b"RIFFwavedata"


def test_x_team_id_header_is_sent(mocked_client):
    """v0.7.0: every team-scoped request sends the X-Team-Id header."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["team"] = request.headers.get("X-Team-Id")
        return httpx.Response(200, json={"sessions": []})

    client = mocked_client(handler, team_id="blink")
    client.get_sessions()
    assert seen["team"] == "blink"


def test_x_team_id_header_omitted_when_unset(mocked_client):
    """When the client was built without a team_id, no header is sent
    (e.g. for /api/me discovery flow before the user has picked one).
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["team"] = request.headers.get("X-Team-Id")
        return httpx.Response(200, json={
            "github": "alice", "is_admin": False, "memberships": [],
        })

    client = mocked_client(handler, team_id=None)
    client.get_me()
    assert seen["team"] is None


def test_network_error_returned_not_raised(mocked_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tunnel down")

    client = mocked_client(handler)
    result = client.get_sessions()
    assert result.network_error is not None
    assert "tunnel down" in str(result.network_error)


# ─── End-to-end against TestClient ───────────────────────────────────────────


@pytest.fixture
def live_server(monkeypatch):
    """Spin up an actual FastAPI app + TestClient and adapt it to VezirClient."""
    import tempfile as _tf

    tdir = _tf.TemporaryDirectory()
    monkeypatch.setenv("VEZIR_DATA", tdir.name)

    from fastapi.testclient import TestClient

    from vezir.server import auth
    from vezir.server.app import create_app

    token = auth.issue("alice")
    app = create_app()
    test_client = TestClient(app)

    # Adapt the FastAPI TestClient into an httpx transport so VezirClient
    # talks to it without an actual socket.  TestClient is already an
    # httpx.Client wrapping a WSGI/ASGI transport — we can lift the
    # transport out.
    transport = test_client._transport

    import vezir.client.api as api_mod
    orig = api_mod.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    api_mod.httpx.Client = factory
    try:
        # v0.7.0: team_id must be set for team-scoped endpoints to
        # work; conftest's auth.issue shim adds 'alice' to 'blink'.
        yield VezirClient("http://testserver", token, team_id="blink")
    finally:
        api_mod.httpx.Client = orig
        tdir.cleanup()


def test_e2e_health(live_server):
    result = live_server.health()
    assert result.is_ok()


def test_e2e_get_sessions_empty(live_server):
    result = live_server.get_sessions()
    assert result.is_ok()
    assert result.unwrap() == []


def test_e2e_get_session_404(live_server):
    result = live_server.get_session("01MISSING")
    assert result.http_error is not None
    assert result.http_error[0] == 404


def test_e2e_get_team(live_server):
    result = live_server.get_team()
    assert result.is_ok()
    assert isinstance(result.unwrap(), list)
