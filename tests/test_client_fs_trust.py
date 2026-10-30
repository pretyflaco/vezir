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


def _tui_save_screen(monkeypatch, tmp_path, name):
    from vezir.client.tui.artifact_screen import ArtifactScreen

    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    screen = ArtifactScreen("01S", name)
    notifications = []
    monkeypatch.setattr(
        screen, "notify", lambda *args, **kwargs: notifications.append((args, kwargs))
    )
    return screen, downloads, notifications


def test_tui_save_rejects_absolute_and_traversal_names(monkeypatch, tmp_path):
    (tmp_path / "home" / "Downloads").mkdir(parents=True)
    before = set(tmp_path.rglob("*"))
    for name in ("../escaped.txt", "nested/escaped.txt", str(tmp_path / "absolute.txt")):
        screen, _, notifications = _tui_save_screen(monkeypatch, tmp_path, name)
        screen._body = "secret"
        screen.action_save_to_disk()
        assert notifications[0][1]["severity"] == "error"
    assert set(tmp_path.rglob("*")) == before


def test_tui_save_never_follows_destination_symlink(monkeypatch, tmp_path):
    screen, downloads, notifications = _tui_save_screen(monkeypatch, tmp_path, "summary.md")
    screen._body = "new"
    target = tmp_path / "victim.txt"
    target.write_text("original")
    link = downloads / "summary.md"
    link.symlink_to(target)

    screen.action_save_to_disk()

    assert notifications[0][1]["severity"] == "error"
    assert target.read_text() == "original"
    assert link.is_symlink()


@pytest.mark.parametrize(("name", "binary"), [("summary.md", False), ("report.pdf", True)])
def test_tui_save_text_and_binary_are_private(monkeypatch, tmp_path, name, binary):
    screen, downloads, notifications = _tui_save_screen(monkeypatch, tmp_path, name)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"\x00PDF secret")
    if binary:
        screen._tmp_path = source
    else:
        screen._body = "text secret"
    old = os.umask(0o022)
    try:
        screen.action_save_to_disk()
    finally:
        os.umask(old)

    dest = downloads / name
    assert dest.read_bytes() == (source.read_bytes() if binary else b"text secret")
    assert _mode(dest) == 0o600
    assert notifications[0][1]["severity"] == "information"


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


# ── L-18 / L-19: credential bookkeeping ─────────────────────────────────────


@pytest.fixture
def tmp_home(monkeypatch):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HOME", d)
        monkeypatch.delenv("VEZIR_URL", raising=False)
        monkeypatch.delenv("VEZIR_TOKEN", raising=False)
        yield Path(d)


def test_adding_bearer_over_nostr_login_clears_session_identity(tmp_home):
    """L-18: a vzr_ bearer replacing a login must not leave the entry
    looking like a nostr identity, or fan-out would swap the bearer for a
    JWT and _try_refresh would POST the dead refresh token."""
    from vezir.client import config as ccfg

    ccfg.add_team_credentials("blink", "https://s.example", "eyJ.jwt.x")
    cfg = ccfg.load_teams_config()
    entry = next(t for t in cfg["teams"] if t["id"] == "blink")
    entry.update(auth="nostr", npub="ab" * 32, refresh_token="vzrt_old",
                 expires_at=1.0, refresh_expires_at=2.0)
    ccfg.save_teams_config(cfg)

    ccfg.add_team_credentials("blink", "https://s.example", "vzr_machine")
    entry = next(t for t in ccfg.load_teams_config()["teams"] if t["id"] == "blink")
    assert entry["token"] == "vzr_machine"
    for k in ("auth", "npub", "refresh_token", "expires_at", "refresh_expires_at"):
        assert k not in entry
    assert ccfg.identity_key(entry) is None
    assert ccfg.team_refresh_token("blink") is None


def test_refresh_token_never_leaves_its_origin(tmp_home, monkeypatch):
    """L-19: --server X with --team Y must not POST Y's refresh token to X."""
    import httpx

    import vezir.client.api as api_mod
    from vezir.client import config as ccfg

    ccfg.add_team_credentials("blink", "https://issuer.example", "eyJ.jwt.x")
    cfg = ccfg.load_teams_config()
    entry = next(t for t in cfg["teams"] if t["id"] == "blink")
    entry.update(auth="nostr", npub="ab" * 32, refresh_token="vzrt_secret")
    ccfg.save_teams_config(cfg)

    posted: list[tuple[str, bytes]] = []

    def handler(request):
        posted.append((str(request.url), request.content))
        return httpx.Response(200, json={"access_jwt": "new", "refresh_token": "vzrt_new"})

    orig = api_mod.httpx.Client

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return orig(*a, **k)

    monkeypatch.setattr(api_mod.httpx, "Client", factory)

    # Different origin: nothing is sent.
    assert api_mod.refresh_session("https://attacker.example", team="blink") is None
    assert posted == []
    # Same origin (trailing slash / case differences are fine): refresh runs.
    assert api_mod.refresh_session("https://ISSUER.example/", team="blink") == "new"
    assert posted and b"vzrt_secret" in posted[0][1]
    assert posted[0][0].lower().startswith("https://issuer.example/")


@pytest.mark.parametrize("a, b, same", [
    ("https://x.example", "https://x.example/", True),
    ("https://x.example:443", "https://x.example", True),
    ("https://x.example", "http://x.example", False),
    ("https://x.example", "https://x.example:8443", False),
    ("https://x.example", "https://y.example", False),
    ("https://x.example", "https://x.example.evil", False),
])
def test_same_origin(a, b, same):
    from vezir.client.config import same_origin

    assert same_origin(a, b) is same


def test_tui_log_is_private(tmp_home, monkeypatch):
    import logging

    from vezir.client import tui as tui_pkg

    old = os.umask(0o022)
    try:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_home / "state"))
        log_path = tui_pkg._install_crash_log()
        assert log_path == tmp_home / "state" / "vezir" / "tui.log"
        assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
        logger = logging.getLogger("vezir")
        handler = next(
            h for h in logger.handlers if getattr(h, "baseFilename", "") == str(log_path)
        )
        handler.maxBytes = 256
        logger.info("before rollover")
        logger.info("x" * 512)
        handler.flush()
        rotated = log_path.with_name("tui.log.1")
        assert rotated.exists()
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
        for h in list(logging.getLogger("vezir").handlers):
            if getattr(h, "baseFilename", "") == str(log_path):
                logging.getLogger("vezir").removeHandler(h)
                h.close()
    finally:
        os.umask(old)
