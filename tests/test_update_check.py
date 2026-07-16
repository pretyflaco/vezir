"""Tests for vezir/client/tui/update_check.py.

The Textual integration (set_timer/set_interval + worker thread) is
wired into MainScreen.on_mount and gated by
VEZIR_TUI_DISABLE_UPDATE_CHECK=1 in the rest of the TUI test suite.
These tests cover the pure building blocks: version comparison, the
PyPI fetch (mocked), upgrade-command selection, and the cross-launch
cache gate.
"""
from __future__ import annotations

import pytest

from vezir.client.tui import update_check as uc

# ── version comparison ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("0.12.0", "0.11.1", True),
        ("0.11.2", "0.11.1", True),
        ("1.0.0", "0.99.99", True),
        ("0.11.1", "0.11.1", False),
        ("0.11.0", "0.11.1", False),
        ("0.10.0", "0.11.1", False),
        # pre-release suffixes compare on the numeric prefix
        ("0.12.0rc1", "0.11.1", True),
        ("0.11.1", "0.11.1rc1", False),  # equal numeric prefix -> not newer
        # unparseable latest never triggers an update
        ("garbage", "0.11.1", False),
        ("", "0.11.1", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert uc.is_newer(latest, current) is expected


def test_parse_version_stops_at_non_numeric():
    assert uc._parse_version("1.2.3") == (1, 2, 3)
    assert uc._parse_version("1.2.0rc1") == (1, 2, 0)
    assert uc._parse_version("2.0") == (2, 0)
    assert uc._parse_version("nope") == ()


# ── PyPI fetch (mocked) ─────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_latest_pypi_version_ok(monkeypatch):
    import httpx

    def _fake_get(url, **kwargs):
        assert "pypi.org" in url
        return _FakeResp({"info": {"version": "0.12.5"}})

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert uc.fetch_latest_pypi_version() == "0.12.5"


def test_fetch_latest_pypi_version_network_error_is_silent(monkeypatch):
    import httpx

    def _boom(url, **kwargs):
        raise httpx.ConnectError("dns go boom")

    monkeypatch.setattr(httpx, "get", _boom)
    assert uc.fetch_latest_pypi_version() is None


def test_fetch_latest_pypi_version_missing_field(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResp({"info": {}}))
    assert uc.fetch_latest_pypi_version() is None


# ── upgrade command selection ───────────────────────────────────────────────


def test_upgrade_command_pipx(monkeypatch):
    monkeypatch.setattr(uc.sys, "executable",
                        "/home/u/.local/pipx/venvs/vezir/bin/python")
    assert uc.upgrade_command() == "pipx upgrade vezir"


def test_upgrade_command_editable(monkeypatch):
    monkeypatch.setattr(uc.sys, "executable", "/usr/bin/python3")
    monkeypatch.delenv("PIPX_HOME", raising=False)

    import vezir
    # A source checkout imports from a tree that isn't site-/dist-packages.
    monkeypatch.setattr(vezir, "__file__", "/home/u/models/vezir/vezir/__init__.py")
    assert uc.upgrade_command() == "git pull && pip install -e ."


def test_upgrade_command_plain_pip(monkeypatch):
    monkeypatch.setattr(uc.sys, "executable", "/usr/bin/python3")
    monkeypatch.delenv("PIPX_HOME", raising=False)

    import vezir
    monkeypatch.setattr(
        vezir, "__file__",
        "/usr/lib/python3.12/site-packages/vezir/__init__.py",
    )
    assert uc.upgrade_command() == "pip install --upgrade vezir"


# ── cross-launch cache gate ─────────────────────────────────────────────────


def test_cache_gate_open_when_never_checked(monkeypatch):
    monkeypatch.setattr(uc, "load_client_prefs", lambda: {})
    assert uc._cache_gate_open(now=1_000_000.0) is True


def test_cache_gate_closed_within_interval(monkeypatch):
    now = 1_000_000.0
    recent = now - 60  # 1 min ago, well within 6h
    monkeypatch.setattr(uc, "load_client_prefs",
                        lambda: {"last_update_check": recent})
    assert uc._cache_gate_open(now) is False


def test_cache_gate_open_after_interval(monkeypatch):
    now = 1_000_000.0
    old = now - (uc.UPDATE_POLL_INTERVAL + 1)
    monkeypatch.setattr(uc, "load_client_prefs",
                        lambda: {"last_update_check": old})
    assert uc._cache_gate_open(now) is True


def test_record_check_persists(monkeypatch):
    saved = {}
    monkeypatch.setattr(uc, "load_client_prefs", lambda: dict(saved))
    monkeypatch.setattr(uc, "save_client_prefs", lambda d: saved.update(d))

    uc._record_check(now=1234567.0, latest="0.12.9")
    assert saved["last_update_check"] == 1234567
    assert saved["last_seen_latest"] == "0.12.9"
