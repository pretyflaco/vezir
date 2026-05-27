"""Tests for the ergonomic token-revocation surface.

Covers both the helper layer (``auth.revoke_by_filter``,
``auth.list_tokens``) and the CLI surface (``vezir token revoke``,
``vezir token list``).

v0.7.0: tokens are no longer team-scoped, so the ``--team`` filter
on ``token revoke`` / ``token list`` was removed.  The remaining
filters (github, label, token-id) still apply.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _seed_three_handles(tmp_data: Path) -> dict:
    """Seed a small fixture: 4 tokens across 3 handles with varied labels.

    Returns a dict mapping a friendly key to the plaintext token so each
    test can grab the one it cares about.

    v0.7.0: team_id is no longer a token field.  We still create the
    teams for backward compat with other tests that may run before this
    one and to give the conftest shim something to point to.
    """
    from vezir.server import auth
    from vezir.server import queue as _queue

    if _queue.get_team("blink") is None:
        _queue.create_team("blink", "Blink")
    if _queue.get_team("twentyone") is None:
        _queue.create_team("twentyone", "Twentyone")

    raw = auth._issue_raw if hasattr(auth, "_issue_raw") else auth.issue
    out: dict[str, str] = {}
    out["alice_phone"] = raw("alice", label="android-phone")
    out["alice_laptop"] = raw("alice", label="linux-laptop")
    out["bob_phone"] = raw("bob", label="android-phone")
    out["carol_unlabeled"] = raw("carol")  # no label
    return out


# ── auth.list_tokens ────────────────────────────────────────────────────────


def test_list_tokens_returns_all(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    rows = auth.list_tokens()
    assert len(rows) == 4
    # Plaintext never leaks
    for r in rows:
        assert "token_hash" not in r
        assert "plaintext" not in r
    # The truncated token_id is present and 12 chars.
    for r in rows:
        assert "token_id" in r
        assert len(r["token_id"]) == 12
    # v0.7.0: team_id no longer in the listed shape.
    for r in rows:
        assert "team_id" not in r


# ── auth.revoke_by_filter ───────────────────────────────────────────────────


def test_revoke_by_filter_refuses_no_filter(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    with pytest.raises(ValueError, match="at least one"):
        auth.revoke_by_filter()
    # Nothing was deleted.
    assert len(auth.list_tokens()) == 4


def test_revoke_by_filter_refuses_short_token_id_prefix(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    with pytest.raises(ValueError, match="at least 4"):
        auth.revoke_by_filter(token_id_prefix="ab")


def test_revoke_by_filter_github_only_removes_all_for_handle(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    removed = auth.revoke_by_filter(github="alice")
    assert len(removed) == 2
    remaining = {r["github"] for r in auth.list_tokens()}
    assert remaining == {"bob", "carol"}


def test_revoke_by_filter_github_plus_label_removes_only_one(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    removed = auth.revoke_by_filter(github="alice", label="android-phone")
    assert len(removed) == 1
    assert removed[0]["label"] == "android-phone"
    # Alice still has her laptop token.
    alice_left = [r for r in auth.list_tokens() if r["github"] == "alice"]
    assert len(alice_left) == 1
    assert alice_left[0]["label"] == "linux-laptop"


def test_revoke_by_filter_label_alone_matches_across_handles(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    removed = auth.revoke_by_filter(label="android-phone")
    # Both alice's phone AND bob's phone match.
    assert len(removed) == 2
    assert {r["github"] for r in removed} == {"alice", "bob"}


def test_revoke_by_filter_label_dash_matches_label_less_rows(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    removed = auth.revoke_by_filter(label="-")
    # Only carol's unlabeled token matches.
    assert len(removed) == 1
    assert removed[0]["github"] == "carol"


def test_revoke_by_filter_token_id_prefix_matches_single_row(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    rows = auth.list_tokens()
    target_id = rows[0]["token_id"]
    target_github = rows[0]["github"]
    removed = auth.revoke_by_filter(token_id_prefix=target_id)
    assert len(removed) == 1
    assert removed[0]["github"] == target_github


def test_revoke_by_filter_no_match_returns_empty_and_writes_nothing(tmp_data):
    from vezir.server import auth

    seeded = _seed_three_handles(tmp_data)
    removed = auth.revoke_by_filter(github="dave")
    assert removed == []
    # All four rows still present + tokens still resolve.
    assert len(auth.list_tokens()) == 4
    assert auth.lookup(seeded["alice_phone"]) == "alice"


# ── CLI: vezir token revoke ─────────────────────────────────────────────────


def _invoke(args, input_text=None):
    """Run the vezir click app with args.  Returns the Result object."""
    from vezir.cli import main

    runner = CliRunner()
    return runner.invoke(main, args, input=input_text, catch_exceptions=False)


def test_cli_revoke_refuses_with_no_filters(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "revoke"])
    assert result.exit_code == 2
    assert "at least one filter" in result.output


def test_cli_revoke_github_with_yes_skips_prompt(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    result = _invoke(["token", "revoke", "--github", "alice", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Would revoke 2 token(s)" in result.output
    assert "Removed 2 token(s)" in result.output
    assert {r["github"] for r in auth.list_tokens()} == {"bob", "carol"}


def test_cli_revoke_github_plus_label_only_one(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    result = _invoke(
        ["token", "revoke", "--github", "alice",
         "--label", "android-phone", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "Would revoke 1 token(s)" in result.output
    alice_left = [r for r in auth.list_tokens() if r["github"] == "alice"]
    assert len(alice_left) == 1
    assert alice_left[0]["label"] == "linux-laptop"


def test_cli_revoke_abort_on_no_confirm(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    result = _invoke(
        ["token", "revoke", "--github", "alice"],
        input_text="n\n",
    )
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert len(auth.list_tokens()) == 4


def test_cli_revoke_no_match_clean_exit(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "revoke", "--github", "dave", "--yes"])
    assert result.exit_code == 0
    assert "no tokens matched" in result.output


def test_cli_revoke_short_token_id_rejected(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "revoke", "--token-id", "ab", "--yes"])
    assert result.exit_code == 2
    assert "at least 4" in result.output


# ── CLI: vezir token list ───────────────────────────────────────────────────


def test_cli_list_includes_github_label_role(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "list"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0]
    assert "github" in header
    assert "role" in header
    assert "label" in header
    # v0.7.0: no team column.
    assert "team" not in header
    # All three handles present.
    assert "alice" in result.output
    assert "bob" in result.output
    assert "carol" in result.output


def test_cli_list_show_id_adds_column(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "list", "--show-id"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0]
    assert " id " in header or header.rstrip().endswith("id")
    import re
    assert re.search(r"\b[0-9a-f]{12}\b", result.output)
