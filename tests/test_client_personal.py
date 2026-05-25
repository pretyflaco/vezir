"""Tests for client-side --personal flag propagation.

The server-side enforcement lives in tests/test_personal.py.  Here we
verify the client surface:

  * vezir/client/uploader.py upload(personal=True) sends the right form
    field on the wire.
  * vezir/client/scribe.py run_scribe(personal=True) coerces sync=False
    so log lines and stored prefs stay honest with the server's
    behavior (server forces sync_enabled=0 for personal sessions).
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import httpx


def _tiny_wav_path(tmp_path: Path) -> Path:
    p = tmp_path / "tiny.wav"
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    p.write_bytes(buf.getvalue())
    return p


def test_uploader_omits_personal_when_false(tmp_path, monkeypatch):
    """Default behavior: don't send the field, keep wire clean."""
    from vezir.client import uploader

    captured: dict = {}

    def fake_post(self, url, **kwargs):
        captured["data"] = kwargs.get("data", {})
        return httpx.Response(
            200,
            json={
                "session_id": "01TEST",
                "bytes": _tiny_wav_path(tmp_path).stat().st_size,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    uploader.upload(
        "http://x",
        "vzr_t",
        _tiny_wav_path(tmp_path),
        personal=False,
    )
    assert "personal" not in captured["data"]


def test_uploader_sends_personal_true(tmp_path, monkeypatch):
    """personal=True must put 'true' on the wire as a form field."""
    from vezir.client import uploader

    captured: dict = {}

    def fake_post(self, url, **kwargs):
        captured["data"] = kwargs.get("data", {})
        return httpx.Response(
            200,
            json={
                "session_id": "01TEST",
                "bytes": _tiny_wav_path(tmp_path).stat().st_size,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    uploader.upload(
        "http://x",
        "vzr_t",
        _tiny_wav_path(tmp_path),
        personal=True,
    )
    assert captured["data"].get("personal") == "true"


def test_run_scribe_personal_overrides_sync_true(monkeypatch, tmp_path):
    """run_scribe(personal=True, sync=True) must coerce sync to False.

    The server enforces this anyway, but we want the client's own
    side-effects (logs, persisted prefs) to reflect what actually
    happened on the server.
    """
    from vezir.client import scribe as scribe_mod

    # Stub the recording layer entirely -- this test cares about
    # personal/sync flag plumbing into uploader.upload(), nothing else.
    audio_file = tmp_path / "meeting-fake.wav"
    audio_file.write_bytes(b"RIFFsomewav")
    monkeypatch.setattr(
        scribe_mod, "_record_via_library", lambda *a, **k: audio_file,
    )

    seen: dict = {}

    def fake_upload(*args, **kwargs):
        seen.update(kwargs)
        return {
            "session_id": "01TEST",
            "dashboard_url": "http://x/s/01TEST",
        }

    monkeypatch.setattr(scribe_mod.uploader, "upload", fake_upload)
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_" + "x" * 43)

    scribe_mod.run_scribe(
        server_url="http://x",
        token="vzr_" + "x" * 43,
        compress=False,
        wait=False,
        personal=True,
        sync=True,  # explicit conflicting value
    )
    assert seen.get("personal") is True
    # The interesting assertion: sync was coerced to False before reaching uploader.
    assert seen.get("sync") is False


def test_run_scribe_personal_default_no_change(monkeypatch, tmp_path):
    """personal=False (default) keeps sync as the caller provided."""
    from vezir.client import scribe as scribe_mod

    audio_file = tmp_path / "meeting-fake.wav"
    audio_file.write_bytes(b"RIFFsomewav")
    monkeypatch.setattr(
        scribe_mod, "_record_via_library", lambda *a, **k: audio_file,
    )

    seen: dict = {}

    def fake_upload(*args, **kwargs):
        seen.update(kwargs)
        return {"session_id": "01TEST", "dashboard_url": "http://x"}

    monkeypatch.setattr(scribe_mod.uploader, "upload", fake_upload)

    scribe_mod.run_scribe(
        server_url="http://x",
        token="vzr_" + "x" * 43,
        compress=False,
        wait=False,
        sync=True,
        # personal omitted → defaults to False
    )
    assert seen.get("personal") is False
    assert seen.get("sync") is True
