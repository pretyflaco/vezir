"""Tests for v0.6.3 ergonomic token-revocation surface.

Cover both the helper layer (``auth.revoke_by_filter``,
``auth.list_tokens``) and the CLI surface (``vezir token revoke``,
``vezir token list --team``).  The CLI tests use ``click.testing`` so
we don't need a full subprocess shell.
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
    """Seed a small fixture: 3 handles across 2 teams with varied labels.

    Returns a dict mapping a friendly key to the plaintext token so each
    test can grab the one it cares about.
    """
    from vezir.server import auth, queue as _queue

    # Need teams to exist before we can issue.
    _queue.create_team("blink", "Blink")
    _queue.create_team("twentyone", "Twentyone")

    raw = auth._issue_raw if hasattr(auth, "_issue_raw") else auth.issue
    out: dict[str, str] = {}
    out["alice_phone"] = raw(
        "alice", team_id="blink", label="android-phone",
    )
    out["alice_laptop"] = raw(
        "alice", team_id="blink", label="linux-laptop",
    )
    out["bob_phone"] = raw(
        "bob", team_id="twentyone", label="android-phone",
    )
    out["carol_unlabeled"] = raw(
        "carol", team_id="blink",  # no label
    )
    return out


# ── auth.list_tokens ────────────────────────────────────────────────────────


def test_list_tokens_returns_all_by_default(tmp_data):
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


def test_list_tokens_filters_by_team(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    blink = auth.list_tokens(team_id="blink")
    twentyone = auth.list_tokens(team_id="twentyone")
    assert len(blink) == 3
    assert len(twentyone) == 1
    assert {r["github"] for r in blink} == {"alice", "carol"}
    assert {r["github"] for r in twentyone} == {"bob"}


def test_list_tokens_unknown_team_returns_empty(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    assert auth.list_tokens(team_id="nonesuch") == []


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


def test_revoke_by_filter_team_filter(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    removed = auth.revoke_by_filter(team_id="twentyone")
    assert len(removed) == 1
    assert removed[0]["github"] == "bob"
    # Blink rows untouched.
    assert len(auth.list_tokens(team_id="blink")) == 3


def test_revoke_by_filter_token_id_prefix_matches_single_row(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    rows = auth.list_tokens()
    # Pick the first row's id prefix.
    target_id = rows[0]["token_id"]
    target_github = rows[0]["github"]
    removed = auth.revoke_by_filter(token_id_prefix=target_id)
    assert len(removed) == 1
    # And it was actually the one we asked for.
    assert removed[0]["github"] == target_github
    # token_hashes are 64-char sha256 hex; collisions on the first 12
    # chars are astronomical, so a 12-char prefix should match exactly
    # one row.


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
    # Confirm side effect.
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
    # Alice still has her laptop.
    alice_left = [r for r in auth.list_tokens() if r["github"] == "alice"]
    assert len(alice_left) == 1
    assert alice_left[0]["label"] == "linux-laptop"


def test_cli_revoke_abort_on_no_confirm(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    # Click confirm reads from stdin; "n\n" declines.
    result = _invoke(
        ["token", "revoke", "--github", "alice"],
        input_text="n\n",
    )
    assert result.exit_code == 1
    assert "Aborted" in result.output
    # No mutation.
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


def test_cli_revoke_team_filter(tmp_data):
    from vezir.server import auth

    _seed_three_handles(tmp_data)
    result = _invoke(["token", "revoke", "--team", "twentyone", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Removed 1 token(s)" in result.output
    assert auth.list_tokens(team_id="twentyone") == []
    assert len(auth.list_tokens(team_id="blink")) == 3


# ── CLI: vezir token list ───────────────────────────────────────────────────


def test_cli_list_includes_team_column_by_default(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "list"])
    assert result.exit_code == 0, result.output
    # Header includes 'team'.
    assert "team" in result.output.splitlines()[0]
    # All four rows present.
    assert "alice" in result.output
    assert "bob" in result.output
    assert "carol" in result.output
    assert "blink" in result.output
    assert "twentyone" in result.output


def test_cli_list_team_filter(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "list", "--team", "twentyone"])
    assert result.exit_code == 0, result.output
    # Only bob's row shows.
    assert "bob" in result.output
    assert "alice" not in result.output
    assert "carol" not in result.output


def test_cli_list_unknown_team_clean_message(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "list", "--team", "ghost"])
    assert result.exit_code == 0
    assert "no tokens scoped to team 'ghost'" in result.output


def test_cli_list_show_id_adds_column(tmp_data):
    _seed_three_handles(tmp_data)
    result = _invoke(["token", "list", "--show-id"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0]
    assert " id " in header or header.rstrip().endswith("id")
    # The id values are 12 hex chars; check at least one such pattern.
    import re
    assert re.search(r"\b[0-9a-f]{12}\b", result.output)
