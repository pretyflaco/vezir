"""Client-side upload path validation (incl. video exts, v0.18.0)."""
from __future__ import annotations

import pytest

from vezir.client import uploader


def test_validate_accepts_audio_exts(tmp_path):
    for ext in (".wav", ".ogg", ".mp3"):
        p = tmp_path / f"a{ext}"
        p.write_bytes(b"x")
        assert uploader.validate_audio_path(p) == p


def test_validate_accepts_video_exts(tmp_path):
    for ext in (".mp4", ".mov"):
        p = tmp_path / f"demo{ext}"
        p.write_bytes(b"x")
        assert uploader.validate_audio_path(p) == p


def test_validate_accepts_uppercase_video_ext(tmp_path):
    p = tmp_path / "DEMO.MP4"
    p.write_bytes(b"x")
    assert uploader.validate_audio_path(p) == p


def test_validate_rejects_unknown_ext(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"x")
    with pytest.raises(ValueError, match="unsupported audio type .txt"):
        uploader.validate_audio_path(p)


def test_validate_rejection_lists_video_exts(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"x")
    with pytest.raises(ValueError, match=r"\.mp4"):
        uploader.validate_audio_path(p)


def test_validate_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        uploader.validate_audio_path(tmp_path / "nope.mp4")


def test_content_types_cover_video():
    assert uploader.CONTENT_TYPES[".mp4"] == "video/mp4"
    assert uploader.CONTENT_TYPES[".mov"] == "video/quicktime"
    # Every accepted extension has a content type.
    assert uploader.ACCEPTED_EXTS == set(uploader.CONTENT_TYPES)
