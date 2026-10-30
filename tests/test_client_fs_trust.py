"""Client-side filesystem trust (security audit 2026-10-30, H-1 / M-12 / L-14).

The server validates names on its side, but a compromised server — or a MITM
against a rogue-but-trusted CA — can return anything.  The client writes to
the user's laptop and must treat every name on the wire as hostile:

* H-1  artifact names in ``session.artifacts`` are used to build the local
       path; ``artifact_friendly_name`` returns unknown names verbatim.
* M-12 the team slug from ``/api/me`` / a move response becomes a directory
       under the recordings root.
* L-14 pulled meeting content must be private (0600 / 0700) regardless of
       the process umask, and never half-written.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from vezir import config
from vezir.client import artifacts as art_mod
from vezir.client.api import Session

# ── validators ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "../../.ssh/authorized_keys",
        "..",
        ".",
        "",
        "/etc/passwd",
        "a/b.txt",
        "a\\b.txt",
        "x\x00y",
        "x\ny",
        "a" * 256,
        None,
        42,
    ],
)
def test_safe_path_component_rejects(bad):
    assert config.safe_path_component(bad) is None


@pytest.mark.parametrize(
    "good",
    [
        "01ULID.summary.de.md",
        "slides v2 (final).pdf",
        ".hidden",
        "ünïcödé — name.txt",
        "a" * 255,
    ],
)
def test_safe_path_component_accepts(good):
    assert config.safe_path_component(good) == good


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", ".", "", "/abs", "x y", "x\n", None])
def test_safe_team_dir_rejects(bad):
    assert config.safe_team_dir(bad) is None


@pytest.mark.parametrize("good", ["blink", "team-01", "default", "__vezir_probe__", "a.b"])
def test_safe_team_dir_accepts(good):
    assert config.safe_team_dir(good) == good


def test_assert_within(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config.assert_within(root / "a" / "b", root)
    config.assert_within(root, root)
    with pytest.raises(ValueError):
        config.assert_within(root / ".." / "x", root)
    with pytest.raises(ValueError):
        config.assert_within(tmp_path / "root-sibling", root)
    # A symlink inside root pointing outside resolves outside -> rejected.
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        config.assert_within(root / "link" / "x", root)


# ── M-12: recordings_dir ─────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../../tmp/x", "a/b", "..", "/abs", ""])
def test_recordings_dir_rejects_unsafe_team(monkeypatch, tmp_path, bad):
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        config.recordings_dir(bad)


def test_recordings_dir_accepts_valid_slug(monkeypatch, tmp_path):
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))
    assert config.recordings_dir("blink") == tmp_path / "blink"
    # Internal probe value used by recovery/local keeps working.
    assert config.recordings_dir("__vezir_probe__").parent == tmp_path


def test_move_session_dir_refuses_server_supplied_traversal(monkeypatch, tmp_path):
    from vezir.client import recovery

    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path / "rec"))
    src = tmp_path / "rec" / "blink" / "meeting-20260101-000000"
    src.mkdir(parents=True)
    (src / "x.wav").write_bytes(b"RIFF")
    with pytest.raises(ValueError):
        recovery.move_session_dir(src, "../../../escaped")
    assert src.exists()
    assert not (tmp_path / "escaped").exists()


# ── H-1: artifact download ───────────────────────────────────────────────────


@pytest.fixture
def make_client(monkeypatch):
    import vezir.client.api as api_mod

    def _make(handler):
        transport = httpx.MockTransport(handler)
        orig = api_mod.httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return orig(*args, **kwargs)

        monkeypatch.setattr(api_mod.httpx, "Client", factory)
        return api_mod.VezirClient("https://vezir.example", "vzr_token", team_id="t")

    return _make


def _session(artifacts: dict) -> Session:
    return Session.from_dict({
        "id": "01S",
        "title": "Weekly",
        "status": "done",
        "github": "alice",
        "created_at": "2026-07-06T10:00:00Z",
        "artifacts": artifacts,
    })


EVIL_NAMES = [
    "../../pwned.txt",
    "../pwned.md",
    "/tmp/pwned-abs.txt",
    "sub/dir.txt",
    "..\\pwned.txt",
    "..",
    "evil\nname.txt",
]


@pytest.mark.parametrize("evil", EVIL_NAMES)
def test_malicious_artifact_name_never_writes_outside_dest(make_client, tmp_path, evil):
    """A hostile server names an artifact with a path; nothing may land
    outside the chosen directory and no request is even made for it."""
    requested: list[str] = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, content=b"owned")

    api = make_client(handler)
    dest = tmp_path / "meeting"
    outside_before = {p for p in tmp_path.rglob("*")}

    saved = art_mod.download_session_artifacts(
        api, _session({"summary": "01S.summary.md", "evil": evil}), dest,
    )

    # Only the legitimate artifact was fetched (plus the attachments listing).
    artifact_reqs = [p for p in requested if p.startswith("/artifact/")]
    assert artifact_reqs == ["/artifact/01S/01S.summary.md"]
    assert [p.name for p in saved if p.suffix == ".md"] == ["20260706_weekly.md"]
    new = {p for p in tmp_path.rglob("*")} - outside_before
    assert all(dest in p.parents or p == dest for p in new), new
    assert not (tmp_path / "pwned.txt").exists()
    assert not Path("/tmp/pwned-abs.txt").exists()


def test_unknown_but_safe_artifact_names_still_download(make_client, tmp_path):
    """Language summaries fall through artifact_friendly_name verbatim —
    that branch must keep working."""
    api = make_client(lambda r: httpx.Response(200, content=b"# Zusammenfassung"))
    dest = tmp_path / "meeting"
    saved = art_mod.download_session_artifacts(
        api, _session({"summary_de": "01S.summary.de.md"}), dest,
    )
    assert (dest / "01S.summary.de.md").read_bytes() == b"# Zusammenfassung"
    assert dest / "01S.summary.de.md" in saved


def test_malicious_attachment_name_is_skipped(make_client, tmp_path):
    def handler(request):
        if request.url.path.endswith("/attachments"):
            return httpx.Response(200, json={"attachments": [
                {"name": "../../pwned.pdf", "size": 1},
                {"name": "ok.pdf", "size": 1},
            ]})
        return httpx.Response(200, content=b"%PDF")

    api = make_client(handler)
    dest = tmp_path / "meeting"
    art_mod.download_session_artifacts(api, _session({}), dest)
    assert (dest / "attachments" / "ok.pdf").exists()
    assert not (tmp_path / "pwned.pdf").exists()


def test_save_artifact_replaces_symlink_instead_of_following(make_client, tmp_path):
    """A pre-existing symlink at the destination must not redirect the write."""
    api = make_client(lambda r: httpx.Response(200, content=b"new"))
    target = tmp_path / "victim.txt"
    target.write_text("original")
    dest_dir = tmp_path / "meeting"
    dest_dir.mkdir()
    link = dest_dir / "20260706_weekly.txt"
    link.symlink_to(target)

    api.save_artifact("01S", "01S.txt", link)

    assert target.read_text() == "original"
    assert not link.is_symlink()
    assert link.read_bytes() == b"new"


# ── L-14: private, atomic client writes ──────────────────────────────────────


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_saved_artifacts_are_private_under_permissive_umask(make_client, tmp_path):
    old = os.umask(0o022)
    try:
        api = make_client(lambda r: httpx.Response(200, content=b"secret transcript"))
        dest = tmp_path / "meeting"
        art_mod.download_session_artifacts(api, _session({"txt": "01S.txt"}), dest)
        f = dest / "20260706_weekly.txt"
        assert _mode(f) == 0o600
        assert _mode(dest) == 0o700
        meta = dest / "session.json"
        assert meta.exists() and _mode(meta) == 0o600
        assert json.loads(meta.read_text())["session_id"] == "01S"
    finally:
        os.umask(old)


def test_secure_write_bytes_leaves_no_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "d" / "f.bin"
    config.secure_write_bytes(path, b"v1")

    real_replace = Path.replace

    def boom(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        config.secure_write_bytes(path, b"v2")
    monkeypatch.setattr(Path, "replace", real_replace)

    assert path.read_bytes() == b"v1"
    assert [p.name for p in path.parent.iterdir()] == ["f.bin"]


def test_upload_journal_is_private(tmp_path):
    from vezir.client import upload_journal

    old = os.umask(0o022)
    try:
        sdir = tmp_path / "meeting"
        sdir.mkdir()
        upload_journal.mark_recording(sdir, team_id="blink")
        j = sdir / ".upload.json"
        assert j.exists() and _mode(j) == 0o600
    finally:
        os.umask(old)
