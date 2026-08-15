"""Meeting-attachment endpoints (issue #16).

Covers the security-relevant surface of ``vezir/server/attachments.py``:

* upload / list / download round trip
* storage lands in the exact directory ``millet sync`` pushes from
* cross-team and personal-session access return 404 (existence-hiding)
* hostile filenames (traversal, control characters, Windows paths, over-long)
* collision handling
* per-file, count and total-byte caps, and their all-or-nothing cleanup
"""
from __future__ import annotations

import io
import tempfile
import wave
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def client_factory(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    def _make():
        return TestClient(create_app(), follow_redirects=False)

    return _make


def _headers(token: str, team: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Team-Id": team}


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


def _issue_for(github: str, team: str) -> str:
    from vezir.server import auth
    return auth.issue(github, team_id=team)


def _upload_session(client, token: str, team: str, personal: bool = False) -> str:
    data = {"personal": "true"} if personal else None
    resp = client.post(
        "/upload",
        headers=_headers(token, team),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
        data=data,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _attach(client, token, team, sid, files):
    return client.post(
        f"/api/sessions/{sid}/attachments",
        headers=_headers(token, team),
        files=files,
    )


# ── round trip ──────────────────────────────────────────────────────────────


def test_upload_list_download_round_trip(client_factory, tmp_data):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")

    resp = _attach(client, tok, "blink", sid, [
        ("files", ("slides.pdf", b"%PDF-1.4 deck", "application/pdf")),
        ("files", ("board.png", b"\x89PNG shot", "image/png")),
    ])
    assert resp.status_code == 200, resp.text
    names = [a["name"] for a in resp.json()["attachments"]]
    assert names == ["slides.pdf", "board.png"]

    listed = client.get(
        f"/api/sessions/{sid}/attachments", headers=_headers(tok, "blink")
    )
    assert listed.status_code == 200
    by_name = {a["name"]: a for a in listed.json()["attachments"]}
    assert by_name["slides.pdf"]["size"] == len(b"%PDF-1.4 deck")
    assert by_name["slides.pdf"]["content_type"] == "application/pdf"
    assert by_name["board.png"]["content_type"] == "image/png"

    got = client.get(
        f"/api/sessions/{sid}/attachments/slides.pdf",
        headers=_headers(tok, "blink"),
    )
    assert got.status_code == 200
    assert got.content == b"%PDF-1.4 deck"


def test_stored_in_the_directory_millet_sync_pushes_from(client_factory, tmp_data):
    """The whole design rests on this path: it is the session dir handed to
    ``millet sync``, whose 0.15.0 attachment passthrough copies the subdir
    into the team repo verbatim."""
    from millet import sync as millet_sync

    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")
    _attach(client, tok, "blink", sid, [
        ("files", ("agenda.md", b"# agenda", "text/markdown")),
    ])

    stored = tmp_data / "sessions" / sid / "attachments" / "agenda.md"
    assert stored.read_bytes() == b"# agenda"
    # Same subdirectory name on both sides of the boundary.
    assert millet_sync.ATTACHMENTS_SUBDIR == "attachments"
    pairs = dict((d, s) for s, d in millet_sync._collect_files(stored.parent.parent))
    assert "attachments/agenda.md" in pairs


def test_stored_attachment_is_private(client_factory, tmp_data):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")
    _attach(client, tok, "blink", sid, [("files", ("a.txt", b"x", "text/plain"))])

    stored = tmp_data / "sessions" / sid / "attachments" / "a.txt"
    assert stored.stat().st_mode & 0o077 == 0


def test_list_is_empty_for_session_without_attachments(client_factory):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")

    resp = client.get(
        f"/api/sessions/{sid}/attachments", headers=_headers(tok, "blink")
    )
    assert resp.status_code == 200
    assert resp.json()["attachments"] == []


# ── visibility ──────────────────────────────────────────────────────────────


def test_cross_team_upload_and_read_are_404(client_factory):
    client = client_factory()
    alice = _issue_for("alice", "blink")
    sid = _upload_session(client, alice, "blink")
    _attach(client, alice, "blink", sid, [("files", ("a.txt", b"x", "text/plain"))])

    mallory = _issue_for("mallory", "twentyone")
    hdr = _headers(mallory, "twentyone")
    assert _attach(
        client, mallory, "twentyone", sid, [("files", ("b.txt", b"y", "text/plain"))]
    ).status_code == 404
    assert client.get(f"/api/sessions/{sid}/attachments", headers=hdr).status_code == 404
    assert client.get(
        f"/api/sessions/{sid}/attachments/a.txt", headers=hdr
    ).status_code == 404


def test_personal_session_hidden_from_same_team_member(client_factory):
    client = client_factory()
    alice = _issue_for("alice", "blink")
    sid = _upload_session(client, alice, "blink", personal=True)
    _attach(client, alice, "blink", sid, [("files", ("a.txt", b"x", "text/plain"))])

    bob = _issue_for("bob", "blink")
    hdr = _headers(bob, "blink")
    assert client.get(f"/api/sessions/{sid}/attachments", headers=hdr).status_code == 404
    assert client.get(
        f"/api/sessions/{sid}/attachments/a.txt", headers=hdr
    ).status_code == 404


def test_unknown_session_is_404(client_factory):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    resp = client.get(
        "/api/sessions/01NOPENOPENOPENOPENOPENOPE/attachments",
        headers=_headers(tok, "blink"),
    )
    assert resp.status_code == 404


def test_requires_auth(client_factory):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")
    assert client.get(f"/api/sessions/{sid}/attachments").status_code in (401, 403)


# ── hostile filenames ───────────────────────────────────────────────────────


def test_traversal_and_control_chars_are_flattened(client_factory, tmp_data):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")

    resp = _attach(client, tok, "blink", sid, [
        ("files", ("../../../etc/passwd", b"root:x", "text/plain")),
        ("files", ("C:\\Users\\bob\\deck.pptx", b"deck", None)),
    ])
    assert resp.status_code == 200, resp.text
    names = [a["name"] for a in resp.json()["attachments"]]
    assert names == ["passwd", "deck.pptx"]

    sdir = tmp_data / "sessions" / sid
    assert sorted(p.name for p in (sdir / "attachments").iterdir()) == [
        "deck.pptx", "passwd",
    ]
    # Nothing escaped the attachments directory.
    assert not (tmp_data / "sessions" / "etc").exists()


def test_control_characters_are_stripped_from_names():
    """HTTP clients tend to percent-encode these, but a hand-rolled client
    (or a future transport) can put raw control bytes in the filename."""
    from vezir.server import attachments

    assert attachments.safe_attachment_name("we\x00ird\x1fname.txt") == "weirdname.txt"
    assert attachments.safe_attachment_name("\x7fdel.png") == "del.png"
    assert attachments.safe_attachment_name("  spaced.pdf  ") == "spaced.pdf"


def test_dot_names_get_a_fallback(client_factory):
    from vezir.server import attachments

    assert attachments.safe_attachment_name("..") == "attachment"
    assert attachments.safe_attachment_name(".") == "attachment"
    assert attachments.safe_attachment_name("") == "attachment"
    assert attachments.safe_attachment_name(None) == "attachment"
    assert attachments.safe_attachment_name("   ") == "attachment"


def test_long_name_is_capped_but_keeps_extension(client_factory):
    from vezir.server import attachments

    name = attachments.safe_attachment_name("a" * 400 + ".pdf")
    assert len(name) <= 128
    assert name.endswith(".pdf")


def test_download_rejects_traversal_name(client_factory):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")
    _attach(client, tok, "blink", sid, [("files", ("a.txt", b"x", "text/plain"))])

    resp = client.get(
        f"/api/sessions/{sid}/attachments/..%2F..%2Fteam.json",
        headers=_headers(tok, "blink"),
    )
    assert resp.status_code in (400, 404)


def test_download_does_not_follow_symlink(client_factory, tmp_data):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")
    _attach(client, tok, "blink", sid, [("files", ("a.txt", b"x", "text/plain"))])

    secret = tmp_data / "secret.txt"
    secret.write_text("token")
    (tmp_data / "sessions" / sid / "attachments" / "link.txt").symlink_to(secret)

    resp = client.get(
        f"/api/sessions/{sid}/attachments/link.txt", headers=_headers(tok, "blink")
    )
    assert resp.status_code == 404
    # …and it is not listed either.
    listed = client.get(
        f"/api/sessions/{sid}/attachments", headers=_headers(tok, "blink")
    )
    assert [a["name"] for a in listed.json()["attachments"]] == ["a.txt"]


def test_same_name_twice_does_not_clobber(client_factory):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")

    _attach(client, tok, "blink", sid, [("files", ("shot.png", b"first", "image/png"))])
    resp = _attach(
        client, tok, "blink", sid, [("files", ("shot.png", b"second", "image/png"))]
    )
    assert resp.json()["attachments"][0]["name"] == "shot_2.png"

    first = client.get(
        f"/api/sessions/{sid}/attachments/shot.png", headers=_headers(tok, "blink")
    )
    second = client.get(
        f"/api/sessions/{sid}/attachments/shot_2.png", headers=_headers(tok, "blink")
    )
    assert first.content == b"first"
    assert second.content == b"second"


# ── caps ────────────────────────────────────────────────────────────────────


def test_count_cap_rejects_and_stores_nothing(client_factory, monkeypatch, tmp_data):
    monkeypatch.setenv("VEZIR_MAX_ATTACHMENTS", "2")
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")

    resp = _attach(client, tok, "blink", sid, [
        ("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(3)
    ])
    assert resp.status_code == 413
    assert not (tmp_data / "sessions" / sid / "attachments").exists()


def test_count_cap_counts_already_stored_files(client_factory, monkeypatch):
    monkeypatch.setenv("VEZIR_MAX_ATTACHMENTS", "2")
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")

    assert _attach(client, tok, "blink", sid, [
        ("files", ("a.txt", b"x", "text/plain")),
        ("files", ("b.txt", b"x", "text/plain")),
    ]).status_code == 200
    assert _attach(
        client, tok, "blink", sid, [("files", ("c.txt", b"x", "text/plain"))]
    ).status_code == 413


def test_total_byte_cap_cleans_up_partial_writes(client_factory, monkeypatch, tmp_data):
    monkeypatch.setenv("VEZIR_MAX_ATTACHMENT_BYTES", "64")
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")

    resp = _attach(client, tok, "blink", sid, [
        ("files", ("small.txt", b"x" * 32, "text/plain")),
        ("files", ("big.txt", b"x" * 128, "text/plain")),
    ])
    assert resp.status_code == 413
    # All-or-nothing: the first file must not survive a failed request.
    adir = tmp_data / "sessions" / sid / "attachments"
    assert not adir.exists() or list(adir.iterdir()) == []


def test_per_file_cap_is_max_upload_bytes(client_factory, monkeypatch, tmp_data):
    client = client_factory()
    tok = _issue_for("alice", "blink")
    sid = _upload_session(client, tok, "blink")
    # Set the cap only after the audio upload, which it would otherwise reject.
    monkeypatch.setenv("VEZIR_MAX_UPLOAD_BYTES", "16")

    resp = _attach(
        client, tok, "blink", sid, [("files", ("big.txt", b"x" * 64, "text/plain"))]
    )
    assert resp.status_code == 413
    adir = tmp_data / "sessions" / sid / "attachments"
    assert not adir.exists() or list(adir.iterdir()) == []
