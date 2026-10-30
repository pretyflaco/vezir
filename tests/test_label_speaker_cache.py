from __future__ import annotations

import os
from unittest.mock import patch

from millet.label import SpeakerInfo


def _speaker() -> SpeakerInfo:
    return SpeakerInfo(
        id="REMOTE",
        channel="system",
        sample_text="hello",
        sample_start=0.0,
        sample_end=1.0,
        segment_count=1,
    )


def test_speaker_lookup_cache_hits_and_invalidates_with_sources(tmp_path, monkeypatch):
    from vezir.server import labels

    monkeypatch.setenv("VEZIR_DATA", str(tmp_path))
    session_id = "01CACHE"
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)
    transcript = session_dir / f"{session_id}.json"
    audio = session_dir / f"{session_id}.ogg"
    transcript.write_text("{}")
    audio.write_bytes(b"one")

    with patch("millet.label.get_speakers", return_value=[_speaker()]) as get_speakers:
        assert labels._get_speakers(session_id)[0].id == "REMOTE"
        assert labels._get_speakers(session_id)[0].channel == "system"
        assert get_speakers.call_count == 1

        cache = session_dir / "clips" / ".speakers.json"
        assert cache.is_file()
        assert os.stat(cache).st_mode & 0o777 == 0o600

        transcript.write_text("{ }")
        labels._get_speakers(session_id)
        assert get_speakers.call_count == 2

        audio.write_bytes(b"two")
        labels._get_speakers(session_id)
        assert get_speakers.call_count == 3
