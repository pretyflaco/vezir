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


async def test_reauth_esc_cancels_inflight_nostr_and_dismisses(monkeypatch):
    """v0.17.1: Esc during a live nostr wait must cancel the client and
    close the modal — not silently no-op for ~3 minutes.

    Pre-fix, ``action_cancel`` returned early while ``_busy``, so Esc (and
    the Google/Cancel buttons) were dead while ``wait_for_connection``
    blocked.  Now Esc calls ``client.cancel()`` and dismisses with None.
    """
    import threading

    cancelled = threading.Event()

    class _FakeClient:
        def __init__(self, *a, **k):
            self.user_pubkey = "npubFAKE"
            self.clock_offset = 0
            self._stop = threading.Event()
        def build_connect_uri(self):
            return "nostrconnect://fake"
        def wait_for_connection(self, timeout=180):
            # Block like the real client until cancelled.
            if self._stop.wait(timeout):
                raise RuntimeError("cancelled")
            raise RuntimeError("timeout")
        def sign_event(self, template, timeout=180):
            return {}
        def cancel(self):
            cancelled.set()
            self._stop.set()
        def close(self):
            pass

    import vezir.client.nostr.nip46 as nip46_mod
    monkeypatch.setattr(nip46_mod, "Nip46Client", _FakeClient)

    app = _Harness.make()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause(0.3)  # let the worker enter wait_for_connection
        await pilot.press("escape")
        await pilot.pause(0.2)
    assert cancelled.is_set(), "Esc did not cancel the in-flight nostr client"
    assert app.result is None, "Esc did not dismiss the modal"


async def test_reauth_stale_worker_message_ignored(monkeypatch):
    """v0.17.1: a ReauthDone from a superseded attempt (different gen) must
    not dismiss/clobber the modal — this is what let a cancelled nostr
    worker's late reply race a freshly started Google attempt."""
    from vezir.client.tui.reauth_screen import ReauthDone, ReauthScreen

    screen = ReauthScreen("http://s", "blink")
    screen._gen = 5
    # A late message tagged with an old generation is dropped.
    stale = ReauthDone({"session_jwt": "stale"}, gen=4)
    # Should not raise and should not attempt to dismiss (no app mounted).
    screen.on_reauth_done(stale)  # no exception == handled/ignored


async def test_reauth_qr_is_plain_compact_unicode(monkeypatch):
    """v0.15.0 regression: the reauth modal's QR must render as plain
    half-block Unicode with NO ANSI escape codes, and the modal must fit
    the viewport with buttons reachable.

    Before the fix, ``render_qr_terminal`` used segno's non-compact mode:
    raw ``\\x1b[7m`` sequences landed in the Static as literal garbage
    (unscannable QR), and the ~106-col × 53-row block overflowed the
    non-scrollable modal, clipping the buttons — the TUI looked frozen.
    """

    LONG_URI = (
        "nostrconnect://" + "ab" * 32
        + "?relay=wss://relay.damus.io&relay=wss://nos.lol"
        + "&metadata=%7B%22name%22%3A%22vezir%22%2C%22url%22%3A"
        + "%22https%3A%2F%2Fvezir.twentyone.ist%22%7D"
    )

    class _FakeClient:
        def __init__(self, *a, **k):
            self.user_pubkey = "npubFAKE"
            self.clock_offset = 0
        def build_connect_uri(self):
            return LONG_URI
        def wait_for_connection(self, timeout=180):
            import time as _t
            _t.sleep(3)  # stay in "waiting for approval" state
            raise RuntimeError("done")
        def sign_event(self, template, timeout=180):
            raise RuntimeError("done")
        def cancel(self):
            pass
        def close(self):
            pass

    import vezir.client.nostr.nip46 as nip46_mod
    monkeypatch.setattr(nip46_mod, "Nip46Client", _FakeClient)

    app = _Harness.make()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        # Wait for the QR progress text to land.
        from textual.widgets import Static
        body = None
        for _ in range(30):
            await pilot.pause(0.1)
            body = app.screen.query_one("#reauth-body", Static)
            content = str(getattr(body, "content", ""))
            if len(content) > 200:
                break
        assert body is not None
        content = str(body.content)

        # 1. No raw ANSI escapes leak into the widget.
        assert "\x1b" not in content, "QR art contains raw ANSI escape codes"
        # 2. The QR is compact half-block art, present in the body.
        assert "▄" in content or "█" in content or "▀" in content
        # 3. Modal + buttons fit the 40-row viewport (buttons visible).
        box = app.screen.query_one("#reauth-box")
        buttons = app.screen.query_one("#reauth-buttons")
        assert box.region.height <= 40
        assert buttons.region.bottom <= 40
        assert buttons.region.bottom > 0, "buttons pushed out of viewport"
        # 4. QR fits the modal width (no wrapping of the art rows).
        qr_lines = [
            ln for ln in content.splitlines()
            if set(ln) <= {"▄", "▀", "█", " "} and ln.strip()
        ]
        assert qr_lines, "no QR rows found in body"
        max_w = max(len(ln) for ln in qr_lines)
        assert max_w <= box.region.width - 4, (
            f"QR rows {max_w} cols wide vs box {box.region.width}"
        )
        assert app.screen.__class__.__name__ == "ReauthScreen"


async def test_reauth_qr_fits_without_scrolling_on_tall_terminal(monkeypatch):
    """v0.17.1 regression: a realistic 4-relay nostrconnect:// URI renders a
    ~39-row QR that must be shown WHOLE (no clipping) on a normal terminal.

    Before the fix, the QR lived in a container capped at ``max-height: 60%``,
    so on a 55-row terminal only ~29 rows were visible and the QR always
    scrolled — the exact symptom the user reported ("QR never displayed in
    full on screen").  Now the scroll container is ``1fr`` and takes all the
    height left after the title/buttons, so the QR needs no scrolling.
    """
    # A realistic connect URI: 4 relays + secret + perms + name (~336 chars
    # in production → segno version 14 → 39 rows).
    LONG_URI = (
        "nostrconnect://" + "bc" * 32
        + "?relay=wss://relay.damus.io&relay=wss://nos.lol"
        + "&relay=wss://offchain.pub&relay=wss://relay.primal.net"
        + "&secret=" + "a" * 32
        + "&perms=sign_event%3A27235&name=vezir"
    )

    class _FakeClient:
        def __init__(self, *a, **k):
            self.user_pubkey = "npubFAKE"
            self.clock_offset = 0
        def build_connect_uri(self):
            return LONG_URI
        def wait_for_connection(self, timeout=180):
            import time as _t
            _t.sleep(3)  # stay in "waiting for approval" state
            raise RuntimeError("done")
        def sign_event(self, template, timeout=180):
            raise RuntimeError("done")
        def cancel(self):
            pass
        def close(self):
            pass

    import vezir.client.nostr.nip46 as nip46_mod
    monkeypatch.setattr(nip46_mod, "Nip46Client", _FakeClient)

    # A normal terminal: 120 cols × 55 rows.  The real QR for this URI is
    # ~39 rows; with title/margin/buttons overhead (~7 rows) it fits in the
    # remaining ~48 rows without scrolling.
    app = _Harness.make()
    async with app.run_test(size=(120, 55)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        from textual.containers import VerticalScroll
        from textual.widgets import Static
        body = None
        for _ in range(30):
            await pilot.pause(0.1)
            body = app.screen.query_one("#reauth-body", Static)
            content = str(getattr(body, "content", ""))
            if content.count("\n") > 20:  # QR rows have landed
                break
        assert body is not None

        scroll = app.screen.query_one("#reauth-scroll", VerticalScroll)
        # The QR fits without scrolling: the container's virtual (content)
        # height does not exceed its visible height.
        assert scroll.virtual_size.height <= scroll.container_size.height, (
            f"QR needs scrolling: virtual {scroll.virtual_size.height} rows "
            f"> visible {scroll.container_size.height} rows"
        )
        # Buttons remain visible below the QR.
        buttons = app.screen.query_one("#reauth-buttons")
        assert 0 < buttons.region.bottom <= 55
