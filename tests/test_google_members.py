"""Tests for the Google email allowlist store (vezir/server/google_members.py)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def test_add_and_lookup(tmp_data):
    from vezir.server import google_members as gm
    gm.add("Alice@blinkbtc.com", "alice", is_admin=True, label="work")
    assert gm.lookup_email("alice@blinkbtc.com") == ("alice", True)


def test_lookup_is_case_insensitive(tmp_data):
    from vezir.server import google_members as gm
    gm.add("Bob@Blinkbtc.com", "bob")
    assert gm.lookup_email("BOB@blinkbtc.com") == ("bob", False)


def test_lookup_unknown_returns_none(tmp_data):
    from vezir.server import google_members as gm
    assert gm.lookup_email("nobody@blinkbtc.com") is None


def test_upsert_updates_github_and_admin(tmp_data):
    from vezir.server import google_members as gm
    gm.add("a@blinkbtc.com", "alice", is_admin=False)
    gm.add("a@blinkbtc.com", "alice2", is_admin=True)
    assert gm.lookup_email("a@blinkbtc.com") == ("alice2", True)
    assert len(gm.list_members()) == 1


def test_remove(tmp_data):
    from vezir.server import google_members as gm
    gm.add("a@blinkbtc.com", "alice")
    assert gm.remove("a@blinkbtc.com") == 1
    assert gm.remove("a@blinkbtc.com") == 0
    assert gm.lookup_email("a@blinkbtc.com") is None


def test_invalid_email_rejected(tmp_data):
    from vezir.server import google_members as gm
    for bad in ("not-an-email", "@blinkbtc.com", "a@", "a b@blinkbtc.com"):
        with pytest.raises(ValueError):
            gm.add(bad, "x")
    # lookup of a malformed email is a miss, not an error.
    assert gm.lookup_email("not-an-email") is None
