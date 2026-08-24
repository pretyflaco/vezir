"""Tests for dated artifact filenames (v0.14.1).

Downloaded artifacts are named ``YYYYMMDD_<title_slug>.<ext>`` instead of
``transcript.pdf``-style names.  Covers the shared helpers in
``vezir.config``, the client-side rename in ``vezir.client.artifacts``,
and the server's friendly ``Content-Disposition`` filename.
"""
from __future__ import annotations

import json

import pytest

# ── config.artifact_title_slug ──────────────────────────────────────────────


def test_title_slug_lowercases_and_underscores():
    from vezir import config

    assert config.artifact_title_slug("Brainstorm Phoenix") == "brainstorm_phoenix"
    assert config.artifact_title_slug("  Weekly Sync / @blink!  ") == "weekly_sync_blink"


def test_title_slug_collapses_underscore_runs():
    from vezir import config

    assert config.artifact_title_slug("a --- b") == "a_b"


def test_title_slug_caps_at_60():
    from vezir import config

    slug = config.artifact_title_slug("x" * 100)
    assert len(slug) == 60


def test_title_slug_empty():
    from vezir import config

    assert config.artifact_title_slug("   ") == ""


# ── config.artifact_stem / artifact_friendly_name ────────────────────────────


def test_stem_with_title():
    from vezir import config

    assert config.artifact_stem("20260824", "Brainstorm Phoenix") == (
        "20260824_brainstorm_phoenix"
    )


def test_stem_without_title_falls_back_to_recording():
    from vezir import config

    assert config.artifact_stem("20260824", "") == "20260824_recording"
    assert config.artifact_stem("20260824", None) == "20260824_recording"


@pytest.mark.parametrize(
    ("server_name", "ext"),
    [
        ("01ABC.summary.md", ".md"),
        ("01ABC.frontmatter.json", ".frontmatter.json"),
        ("01ABC.srt", ".srt"),
        ("01ABC.txt", ".txt"),
        ("01ABC.pdf", ".pdf"),
        # .json must not shadow .frontmatter.json
        ("01ABC.json", ".json"),
    ],
)
def test_friendly_name_all_suffixes(server_name, ext):
    from vezir import config

    assert config.artifact_friendly_name(server_name, "20260824", "My Talk") == (
        f"20260824_my_talk{ext}"
    )


def test_friendly_name_unknown_type_keeps_original():
    from vezir import config

    assert config.artifact_friendly_name("slides.pdf.bak", "20260824", "t") == (
        "slides.pdf.bak"
    )


# ── client artifacts._friendly_name (Session-aware) ──────────────────────────


class _S:
    """Minimal Session stand-in."""

    def __init__(self, title="Brainstorm Phoenix",
                 created_at="2026-08-23T22:30:00Z"):
        self.id = "01TEST"
        self.title = title
        self.created_at = created_at
        self.status = "done"
        self.github = None
        self.team_id = None
        self.artifacts = {}


def test_client_friendly_name_uses_local_date_and_title():
    from datetime import datetime, timezone

    from vezir.client.artifacts import _friendly_name

    session = _S(created_at="2026-08-23T22:30:00Z")
    name = _friendly_name(session, "01TEST.pdf")
    assert name.startswith("20")
    assert "brainstorm_phoenix.pdf" in name
    # The local-tz date must parse back as YYYYMMDD.
    date_part = name.split("_")[0]
    datetime.strptime(date_part, "%Y%m%d").replace(tzinfo=timezone.utc)


def test_client_friendly_name_bad_created_at_falls_back_to_today():
    from datetime import datetime

    from vezir.client.artifacts import _friendly_name

    session = _S(title="T", created_at="not-a-date")
    name = _friendly_name(session, "01TEST.txt")
    assert name.endswith("_t.txt")
    assert name.split("_")[0] == datetime.now().strftime("%Y%m%d")


def test_client_friendly_name_no_title_falls_back_to_recording():
    from vezir.client.artifacts import _friendly_name

    name = _friendly_name(_S(title=None), "01TEST.summary.md")
    assert name.split("_", 1)[1] == "recording.md"


# ── download_session_artifacts saves under dated names ───────────────────────


class _FakeResult:
    def __init__(self, ok=None):
        self.ok = ok

    def is_ok(self):
        return True

    def error_message(self):
        return ""


class _FakeApi:
    def save_artifact(self, sid, name, dest):
        from pathlib import Path

        Path(dest).write_text("x")
        return _FakeResult()

    def list_attachments(self, sid):
        return _FakeResult(ok=[])


def test_download_saves_dated_names(tmp_path):
    from vezir.client.artifacts import download_session_artifacts

    session = _S()
    session.artifacts = {
        "pdf": "01X.pdf",
        "summary": "01X.summary.md",
    }
    saved = download_session_artifacts(_FakeApi(), session, tmp_path)
    names = {p.name for p in saved}
    pdf = [n for n in names if n.endswith(".pdf")]
    md = [n for n in names if n.endswith(".md")]
    assert len(pdf) == 1 and pdf[0] != "transcript.pdf"
    assert "brainstorm_phoenix" in pdf[0]
    assert len(md) == 1 and "brainstorm_phoenix" in md[0]


def test_download_is_idempotent_with_new_names(tmp_path):
    from vezir.client.artifacts import _friendly_name, download_session_artifacts

    session = _S()
    session.artifacts = {"txt": "01X.txt"}
    first = download_session_artifacts(_FakeApi(), session, tmp_path)
    expected = _friendly_name(session, "01X.txt")
    assert (tmp_path / expected).exists() is True
    assert any(p.name == expected for p in first)
    # Second run skips (file exists under the new name).
    second = download_session_artifacts(_FakeApi(), session, tmp_path)
    assert any(p.name == expected for p in second)


# ── server Content-Disposition ───────────────────────────────────────────────


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path))
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    def _make():
        return TestClient(create_app(), follow_redirects=False)

    return _make


def _bearer(token: str, team: str = "blink") -> dict:
    return {"Authorization": f"Bearer {token}", "X-Team-Id": team}


def test_artifact_endpoint_content_disposition_friendly(client_factory):

    from vezir import config
    from vezir.server import auth, queue

    client = client_factory()
    tok = auth.issue("alice")
    queue.enqueue(
        "01CD", github="alice", title="Brainstorm Phoenix",
        team_id="blink",
    )
    sdir = config.sessions_dir() / "01CD"
    sdir.mkdir(parents=True)
    (sdir / "01CD.pdf").write_bytes(b"%PDF-1.4 fake")

    resp = client.get("/artifact/01CD/01CD.pdf", headers=_bearer(tok))
    assert resp.status_code == 200
    disp = resp.headers["content-disposition"]
    assert "brainstorm_phoenix.pdf" in disp
    assert "01CD.pdf" not in disp.replace("filename*=UTF-8''", "")


def test_artifact_endpoint_no_title_falls_back_to_recording(client_factory):

    from vezir import config
    from vezir.server import auth, queue

    client = client_factory()
    tok = auth.issue("alice")
    queue.enqueue("01NT", github="alice", title=None, team_id="blink")
    sdir = config.sessions_dir() / "01NT"
    sdir.mkdir(parents=True)
    (sdir / "01NT.txt").write_text("hello")

    resp = client.get("/artifact/01NT/01NT.txt", headers=_bearer(tok))
    assert resp.status_code == 200
    assert "_recording.txt" in resp.headers["content-disposition"]


def test_missing_server_artifacts_uses_dated_names(tmp_path):
    from vezir.client.pull import missing_server_artifacts

    session = _S()
    session.artifacts = {"pdf": "01X.pdf"}
    missing = missing_server_artifacts(session, tmp_path)
    assert len(missing) == 1
    assert missing[0].endswith(".pdf")
    assert missing[0] != "transcript.pdf"

    # After the file exists under its friendly name, nothing is missing.
    (tmp_path / missing[0]).write_text("x")
    assert missing_server_artifacts(session, tmp_path) == []


def test_dir_has_artifacts_detects_dated_names(tmp_path):
    from vezir.client.pull import _dir_has_artifacts

    assert _dir_has_artifacts(tmp_path) is False
    (tmp_path / "session.json").write_text(json.dumps({"session_id": "01"}))
    assert _dir_has_artifacts(tmp_path) is False  # stub only
    (tmp_path / "20260824_brainstorm.pdf").write_text("x")
    assert _dir_has_artifacts(tmp_path) is True
