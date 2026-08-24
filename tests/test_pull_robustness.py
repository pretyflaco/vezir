"""Tests for pull robustness (v0.17.0): download retry + partial-pull self-heal.

Covers the two failure modes that made the startups bot's wrapper carry
hand-rolled retry/repair hacks:
  * ``vezir pull`` gave up on the first network timeout -> downloads now
    retry transient transport errors with backoff.
  * A partially-pulled session (manifest entry + some artifacts on disk)
    was pinned forever ("0 sessions" on re-pull) -> pull now tops up
    missing artifacts before skipping.
"""
from __future__ import annotations

import json

import httpx

# ── download retry ──────────────────────────────────────────────────────────


def test_download_retries_transient_errors(monkeypatch):
    """ConnectError twice, then success -> the call succeeds."""
    from vezir.client import api as api_mod
    from vezir.client.api import VezirClient

    monkeypatch.setattr("time.sleep", lambda _s: None)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, content=b"pdf-bytes")

    transport = httpx.MockTransport(handler)
    orig = api_mod.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(api_mod.httpx, "Client", factory)
    client = VezirClient("https://test", "vzr_tok", team_id="blink")
    result = client.download_artifact("01X", "x.pdf")
    assert result.is_ok()
    assert result.ok == b"pdf-bytes"
    assert calls["n"] == 3


def test_download_gives_up_after_retries(monkeypatch):
    from vezir.client import api as api_mod
    from vezir.client.api import VezirClient

    monkeypatch.setattr("time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("always down")

    transport = httpx.MockTransport(handler)
    orig = api_mod.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(api_mod.httpx, "Client", factory)
    client = VezirClient("https://test", "vzr_tok", team_id="blink")
    result = client.download_artifact("01X", "x.pdf")
    assert not result.is_ok()
    # 1 initial + 3 backoff retries.
    assert calls["n"] == 1 + len(VezirClient._RETRY_BACKOFF)


def test_http_errors_are_not_retried(monkeypatch):
    """A deterministic 404 must not burn through the retry budget."""
    from vezir.client import api as api_mod
    from vezir.client.api import VezirClient

    monkeypatch.setattr("time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    transport = httpx.MockTransport(handler)
    orig = api_mod.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(api_mod.httpx, "Client", factory)
    client = VezirClient("https://test", "vzr_tok", team_id="blink")
    result = client.download_artifact("01X", "x.pdf")
    assert not result.is_ok()
    assert calls["n"] == 1


# ── partial-pull self-heal ──────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, ok=None):
        self.ok = ok

    def is_ok(self):
        return True

    def error_message(self):
        return "boom"


def test_pull_tops_up_missing_artifacts(tmp_path, monkeypatch):
    """A manifest-tracked session missing the pdf gets it on re-pull."""
    from vezir import config
    from vezir.client import pull as pull_mod
    from vezir.client.artifacts import _friendly_name

    team_dir = tmp_path / "blink"
    rec = team_dir / "meeting-20260605-123757_WEEKLY"
    rec.mkdir(parents=True)

    class _S:
        id = "01PARTIAL"
        status = "done"
        title = "Weekly"
        github = "alice"
        created_at = "2026-06-05T12:37:57Z"
        team_id = "blink"
        artifacts = {
            "txt": "01PARTIAL.txt",
            "pdf": "01PARTIAL.pdf",
        }

    session = _S()
    # Simulate the partial pull: only the txt survived.
    (rec / _friendly_name(session, "01PARTIAL.txt")).write_text("transcript")
    (rec / "session.json").write_text(json.dumps({"session_id": "01PARTIAL"}))
    (team_dir / ".pull-manifest.json").write_text(
        json.dumps({"01PARTIAL": rec.name}),
    )

    downloaded = []

    class _Api:
        def get_sessions(self, limit=50, since=None):
            return _FakeResult(ok=[session])

        def save_artifact(self, sid, name, dest):
            from pathlib import Path as _P

            _P(dest).write_text(f"content of {name}")
            downloaded.append(name)
            return _FakeResult()

        def list_attachments(self, sid):
            return _FakeResult(ok=[])

    monkeypatch.setattr(config, "recordings_dir", lambda team_id=None: team_dir)

    n = pull_mod.pull_team_sessions(_Api())
    assert n == 1
    # Only the missing pdf was fetched; the existing txt was skipped.
    assert downloaded == ["01PARTIAL.pdf"]
    assert (rec / _friendly_name(session, "01PARTIAL.pdf")).exists()


def test_pull_still_skips_complete_sessions(tmp_path, monkeypatch):
    """A fully-pulled session must NOT re-download anything."""
    from vezir import config
    from vezir.client import pull as pull_mod
    from vezir.client.artifacts import _friendly_name

    team_dir = tmp_path / "blink"
    rec = team_dir / "meeting-20260605-123757_WEEKLY"
    rec.mkdir(parents=True)

    class _S:
        id = "01FULL"
        status = "done"
        title = "Weekly"
        github = "alice"
        created_at = "2026-06-05T12:37:57Z"
        team_id = "blink"
        artifacts = {"txt": "01FULL.txt", "pdf": "01FULL.pdf"}

    session = _S()
    (rec / _friendly_name(session, "01FULL.txt")).write_text("t")
    (rec / _friendly_name(session, "01FULL.pdf")).write_text("p")
    (rec / "session.json").write_text(json.dumps({"session_id": "01FULL"}))
    (team_dir / ".pull-manifest.json").write_text(
        json.dumps({"01FULL": rec.name}),
    )

    downloaded = []

    class _Api:
        def get_sessions(self, limit=50, since=None):
            return _FakeResult(ok=[session])

        def save_artifact(self, sid, name, dest):
            downloaded.append(name)
            return _FakeResult()

        def list_attachments(self, sid):
            return _FakeResult(ok=[])

    monkeypatch.setattr(config, "recordings_dir", lambda team_id=None: team_dir)

    n = pull_mod.pull_team_sessions(_Api())
    assert n == 0
    assert downloaded == []
