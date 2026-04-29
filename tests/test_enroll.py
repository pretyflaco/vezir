"""Tests for /admin/enroll device-enrollment page."""
from __future__ import annotations

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


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── auth gating ─────────────────────────────────────────────────────────────


def test_enroll_get_requires_auth(client_and_token):
    client, _ = client_and_token
    resp = client.get("/admin/enroll")
    assert resp.status_code == 401


def test_enroll_post_requires_auth(client_and_token):
    client, _ = client_and_token
    resp = client.post(
        "/admin/enroll",
        data={"token": "vzr_x", "url": "http://example.local:8000"},
    )
    assert resp.status_code == 401


# ── empty form (just the paste UI) ──────────────────────────────────────────


def test_enroll_get_authed_renders_form(client_and_token):
    client, token = client_and_token
    resp = client.get("/admin/enroll", headers=_bearer(token))
    assert resp.status_code == 200
    body = resp.text
    assert "Enroll a device" in body
    assert "<form" in body
    assert "/admin/enroll" in body
    # No QR yet because no token+url were supplied.
    assert "<svg" not in body


# ── full happy path: GET with token + url renders QR ────────────────────────


def _has_payload_fields(body: str, *, url: str, token: str) -> bool:
    """The QR JSON is rendered inside a <pre> with HTML-entity escaping
    (Jinja autoescape), so don't compare to literal JSON. Compare to the
    HTML-escaped form instead.
    """
    import html as _html
    needle_v = "&#34;v&#34;:1"
    needle_url = f"&#34;url&#34;:&#34;{_html.escape(url)}&#34;"
    needle_token = f"&#34;token&#34;:&#34;{token}&#34;"
    return all(s in body for s in (needle_v, needle_url, needle_token))


def test_enroll_get_with_token_and_url_renders_qr(client_and_token):
    client, token = client_and_token
    url = "http://muscle.tail178bd.ts.net:8000"
    resp = client.get(
        f"/admin/enroll?token={token}&url={url}",
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<svg" in body  # inline SVG QR code
    assert _has_payload_fields(body, url=url, token=token)


def test_enroll_post_with_token_and_url_renders_qr(client_and_token):
    client, token = client_and_token
    url = "http://muscle.tail178bd.ts.net:8000"
    resp = client.post(
        "/admin/enroll",
        headers=_bearer(token),
        data={"token": token, "url": url},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<svg" in body
    assert _has_payload_fields(body, url=url, token=token)


# ── invalid token must NOT render QR ────────────────────────────────────────


def test_enroll_invalid_token_no_qr(client_and_token):
    client, valid_token = client_and_token
    # Auth with the valid token, but submit a bogus token in the form.
    resp = client.post(
        "/admin/enroll",
        headers=_bearer(valid_token),
        data={"token": "vzr_bogus", "url": "http://x.local:8000"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "Invalid token" in body
    assert "<svg" not in body


# ── reject obviously bad URLs ───────────────────────────────────────────────


@pytest.mark.parametrize("bad_url", [
    "ftp://example.com",
    "javascript:alert(1)",
    "not-a-url",
    "",
])
def test_enroll_post_rejects_unsafe_url(client_and_token, bad_url):
    client, valid_token = client_and_token
    resp = client.post(
        "/admin/enroll",
        headers=_bearer(valid_token),
        data={"token": valid_token, "url": bad_url},
    )
    assert resp.status_code == 200
    body = resp.text
    # Either the explicit URL error or the missing-fields error is acceptable.
    assert ("Server URL must be" in body) or ("required" in body)
    assert "<svg" not in body


# ── payload schema is canonical ─────────────────────────────────────────────


def test_payload_is_canonical_json():
    from vezir.server.enroll import build_payload, PAYLOAD_VERSION

    payload = build_payload("http://server.example:8000", "vzr_abc123")
    obj = json.loads(payload)
    assert obj == {
        "v": PAYLOAD_VERSION,
        "url": "http://server.example:8000",
        "token": "vzr_abc123",
    }
    # Compact (no spaces) so QR is smaller.
    assert " " not in payload
