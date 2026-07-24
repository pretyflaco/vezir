"""In-TUI re-auth + session-expiry tracking (0.8.9).

Covers:
  * HTTP 401 classification on the upload path (`_is_auth_error`).
  * Storing/reading the session JWT expiry in teams.json
    (`set_team_session(expires_at=...)`, `active_team_expiry()`).
  * The ReauthScreen modal flow (mocked login worker) dismissing with the
    login body on success and None on cancel.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def home(monkeypatch):
    """Isolate ~/.config/vezir for teams.json reads/writes."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HOME", d)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(d)))
        yield Path(d)


# ── 401 classification ──

def test_is_auth_error_detects_401_response():
    from vezir.client.tui.record_screen import _is_auth_error

    req = httpx.Request("POST", "http://x/upload")
    resp = httpx.Response(401, request=req)
    exc = httpx.HTTPStatusError("unauthorized", request=req, response=resp)
    assert _is_auth_error(exc) is True


def test_is_auth_error_ignores_other_statuses():
    from vezir.client.tui.record_screen import _is_auth_error

    req = httpx.Request("POST", "http://x/upload")
    resp = httpx.Response(500, request=req)
    exc = httpx.HTTPStatusError("boom", request=req, response=resp)
    assert _is_auth_error(exc) is False


def test_is_auth_error_non_http_exception():
    from vezir.client.tui.record_screen import _is_auth_error

    assert _is_auth_error(ConnectionError("dns")) is False


# ── expiry storage/read ──

def test_set_team_session_stores_expiry(home):
    from vezir.client import config as cc

    now = time.time()
    cc.set_team_session(
        "blink", "http://s", "jwt-1", "npub1", expires_at=now + 3600,
    )
    assert abs(cc.active_team_expiry() - (now + 3600)) < 2


def test_active_team_expiry_none_when_absent(home):
    from vezir.client import config as cc

    # Login without expiry (older flow) → no stored expiry.
    cc.set_team_session("blink", "http://s", "jwt-1", "npub1")
    assert cc.active_team_expiry() is None


def test_active_team_expiry_none_when_no_active(home):
    from vezir.client import config as cc

    assert cc.active_team_expiry() is None


def test_set_team_session_update_preserves_when_none(home):
    from vezir.client import config as cc

    now = time.time()
    cc.set_team_session("blink", "http://s", "jwt-1", "n", expires_at=now + 100)
    # Re-login WITHOUT expiry must not wipe the stored one.
    cc.set_team_session("blink", "http://s", "jwt-2", "n")
    assert cc.active_team_expiry() is not None
    assert abs(cc.active_team_expiry() - (now + 100)) < 2


# ── refresh-token storage/read (0.8.10) ──

def test_set_team_session_stores_refresh_token(home):
    from vezir.client import config as cc

    cc.set_team_session(
        "blink", "http://s", "jwt-1", "n",
        refresh_token="vzrt_abc", refresh_expires_at=time.time() + 604800,
    )
    assert cc.active_team_refresh_token() == "vzrt_abc"


def test_active_team_refresh_token_none_without_refresh(home):
    from vezir.client import config as cc

    cc.set_team_session("blink", "http://s", "jwt-1", "n")
    assert cc.active_team_refresh_token() is None


def test_set_team_session_rotates_refresh_token(home):
    from vezir.client import config as cc

    cc.set_team_session("blink", "http://s", "jwt-1", "n", refresh_token="vzrt_1")
    # A refresh rotates the token; the new value replaces the old.
    cc.set_team_session("blink", "http://s", "jwt-2", "n", refresh_token="vzrt_2")
    assert cc.active_team_refresh_token() == "vzrt_2"


def test_set_team_session_update_preserves_refresh_when_none(home):
    from vezir.client import config as cc

    cc.set_team_session("blink", "http://s", "jwt-1", "n", refresh_token="vzrt_1")
    # An expiry-only update must not wipe the stored refresh token.
    cc.set_team_session("blink", "http://s", "jwt-2", "n", expires_at=time.time())
    assert cc.active_team_refresh_token() == "vzrt_1"


# ── ReauthScreen modal flow (Textual pilot, mocked login) ──

class _Harness:
    """Build a minimal Textual app that pushes ReauthScreen and captures the
    dismissal result."""

    @staticmethod
    def make(server_url="http://s", team="blink"):
        from textual.app import App

        from vezir.client.tui.reauth_screen import ReauthScreen

        class H(App):
            def __init__(self):
                super().__init__()
                self.result: object = "UNSET"

            def on_mount(self):
                def _done(r):
                    self.result = r
                self.push_screen(ReauthScreen(server_url, team), _done)

        return H()


async def test_reauth_cancel_returns_none():
    app = _Harness.make()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


async def test_reauth_nostr_success_dismisses_with_body(monkeypatch):
    # Mock the nostr login building blocks so no network/relay is touched.
    from vezir.client.nostr import login as nostr_login

    class _FakeClient:
        def __init__(self, *a, **k):
            self.user_pubkey = "npubFAKE"
            self.clock_offset = 0
        def build_connect_uri(self):
            return "nostrconnect://fake"
        def wait_for_connection(self, timeout=180):
            return self.user_pubkey
        def sign_event(self, template, timeout=180):
            return {"signed": True}
        def close(self):
            pass

    import vezir.client.nostr.nip46 as nip46_mod
    monkeypatch.setattr(nip46_mod, "Nip46Client", _FakeClient)
    monkeypatch.setattr(
        nostr_login, "post_login",
        lambda url, header, verify=True: {
            "session_jwt": "jwt-NEW", "npub": "npubFAKE", "github": "alice",
            "expires_in": 86400,
        },
    )

    app = _Harness.make()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        # Wait for the worker to post ReauthDone.
        await pilot.pause(0.3)
        for _ in range(20):
            if app.result != "UNSET":
                break
            await pilot.pause(0.1)
    assert isinstance(app.result, dict)
    assert app.result["session_jwt"] == "jwt-NEW"


async def test_reauth_nostr_failure_keeps_modal_open(monkeypatch):
    from vezir.client.nostr import login as nostr_login

    class _FakeClient:
        def __init__(self, *a, **k):
            self.user_pubkey = "npubFAKE"
            self.clock_offset = 0
        def build_connect_uri(self):
            return "nostrconnect://fake"
        def wait_for_connection(self, timeout=180):
            raise RuntimeError("signer timeout")
        def sign_event(self, template, timeout=180):
            return {}
        def close(self):
            pass

    import vezir.client.nostr.nip46 as nip46_mod
    monkeypatch.setattr(nip46_mod, "Nip46Client", _FakeClient)
    monkeypatch.setattr(
        nostr_login, "post_login",
        lambda *a, **k: {"session_jwt": "should-not-happen"},
    )

    app = _Harness.make()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause(0.3)
        for _ in range(15):
            await pilot.pause(0.1)
        # Failure → not dismissed (still UNSET), modal stays for retry.
        assert app.result == "UNSET"
        assert app.screen.__class__.__name__ == "ReauthScreen"
