"""Tests for the nostr npub allowlist store (vezir/server/nostr_members.py)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

PK_A = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
PK_B = "e3b1372cfa163f9e530f907127e40018ec1f08b97d119fa553cbc110c565dc75"


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def test_add_and_lookup(tmp_data):
    from vezir.server import nostr_members as nm
    nm.add(PK_A, "alice", is_admin=True, label="laptop")
    assert nm.lookup_npub(PK_A) == ("alice", True)


def test_lookup_unknown_returns_none(tmp_data):
    from vezir.server import nostr_members as nm
    assert nm.lookup_npub(PK_B) is None


def test_lookup_is_case_insensitive(tmp_data):
    from vezir.server import nostr_members as nm
    nm.add(PK_A.upper(), "alice")
    assert nm.lookup_npub(PK_A.lower()) == ("alice", False)


def test_upsert_updates_github_and_admin(tmp_data):
    from vezir.server import nostr_members as nm
    nm.add(PK_A, "alice", is_admin=False)
    nm.add(PK_A, "alice2", is_admin=True)
    assert nm.lookup_npub(PK_A) == ("alice2", True)
    # Still a single row.
    assert len(nm.list_members()) == 1


def test_remove(tmp_data):
    from vezir.server import nostr_members as nm
    nm.add(PK_A, "alice")
    assert nm.remove(PK_A) == 1
    assert nm.lookup_npub(PK_A) is None
    # Idempotent.
    assert nm.remove(PK_A) == 0


def test_list_members(tmp_data):
    from vezir.server import nostr_members as nm
    nm.add(PK_A, "alice", label="laptop")
    nm.add(PK_B, "bob", is_admin=True)
    members = nm.list_members()
    by_npub = {m["npub"]: m for m in members}
    assert by_npub[PK_A]["github"] == "alice"
    assert by_npub[PK_A]["is_admin"] is False
    assert by_npub[PK_A]["label"] == "laptop"
    assert by_npub[PK_B]["is_admin"] is True


def test_invalid_pubkey_rejected(tmp_data):
    from vezir.server import nostr_members as nm
    with pytest.raises(ValueError, match="64 hex"):
        nm.add("tooshort", "alice")
    with pytest.raises(ValueError, match="valid hex"):
        nm.add("z" * 64, "alice")


def test_lookup_invalid_returns_none(tmp_data):
    from vezir.server import nostr_members as nm
    # lookup must never raise on malformed input -- it's on the auth path.
    assert nm.lookup_npub("nonsense") is None
