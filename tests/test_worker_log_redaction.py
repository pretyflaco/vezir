"""Redaction of credentials in worker error/sync_error log tails (v0.12.1).

`millet sync` failures embed the last ~2 KiB of the millet log into the
job row's ``sync_error``, which is returned to EVERY team member via
``GET /api/sessions``.  Git errors routinely echo the remote URL, and the
common PAT-in-URL pattern (``https://ghp_xxx@github.com/org/repo.git``)
would otherwise hand the team's git token to any scribe.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_log():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "millet.log"


def test_pat_in_url_is_redacted(tmp_log):
    from vezir.server import worker

    tmp_log.write_text(
        "cloning...\n"
        "fatal: unable to access "
        "'https://ghp_SECRETTOKEN1234@github.com/blink/team.git': "
        "The requested URL returned error: 403\n"
    )
    tail = worker._last_log_lines(tmp_log)
    assert "ghp_SECRETTOKEN1234" not in tail
    assert "***@github.com" in tail


def test_userpass_in_url_is_redacted(tmp_log):
    from vezir.server import worker

    tmp_log.write_text(
        "fatal: could not read Password for "
        "'https://alice:hunter2@gitlab.example.com/x.git'\n"
    )
    tail = worker._last_log_lines(tmp_log)
    assert "hunter2" not in tail
    assert "alice" not in tail
    assert "***@gitlab.example.com" in tail


def test_plain_url_without_credentials_is_untouched(tmp_log):
    from vezir.server import worker

    tmp_log.write_text(
        "fatal: unable to access 'https://github.com/blink/team.git'\n"
    )
    tail = worker._last_log_lines(tmp_log)
    assert "https://github.com/blink/team.git" in tail
    assert "***" not in tail
