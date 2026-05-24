from __future__ import annotations

import stat
import tempfile
from pathlib import Path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_runtime_dirs_and_tokens_are_private(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)

        from vezir import config
        from vezir.server import auth, queue, voiceprints

        config.ensure_dirs()
        # The auth.issue shim in conftest auto-creates the 'blink' team
        # if it doesn't exist, so this also guarantees the team row is
        # in place for the per-team voiceprint DB below.
        auth.issue("alice")
        queue.enqueue("01TEST", github="alice", title="test", team_id="blink")
        # v0.6.2+: voiceprint DBs are per-team.
        voiceprints.ensure_db_exists("blink")

        assert _mode(Path(d)) == 0o700
        assert _mode(Path(d) / "sessions") == 0o700
        assert _mode(Path(d) / "jobs") == 0o700
        assert _mode(Path(d) / "logs") == 0o700
        assert _mode(Path(d) / "tokens.json") == 0o600
        assert _mode(Path(d) / "vezir.sqlite") == 0o600
        # v0.6.2+: per-team voiceprint DB.
        assert _mode(Path(d) / "teams" / "blink" / "speaker_profiles.json") == 0o600


def test_secure_write_text_creates_private_parent_and_file(tmp_path):
    from vezir import config

    target = tmp_path / "nested" / "secret.json"
    config.secure_write_text(target, "{}")

    assert _mode(target.parent) == 0o700
    assert _mode(target) == 0o600
