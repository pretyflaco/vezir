"""Streaming multipart uploads (security audit 2026-10-30, H-4 / H-4b).

FastAPI's ``File(...)`` spooled the whole multipart body to ``$TMPDIR`` before
the handler's size check ran, and a mid-upload disconnect leaked that spool
(Starlette closes it only on ``MultiPartException``; FastAPI registers
``body.close`` only after ``form()`` returns).  The upload endpoints now parse
``request.stream()`` themselves and write file bytes straight to their
bounded destination.  These tests pin:

* the declared ``Content-Length`` is rejected *before* any body is read;
* an undeclared (chunked) oversize body is cut at the cap, with nothing
  spooled to the temp dir and nothing left in the session dir;
* a client that disconnects mid-upload leaves no session dir, no temp
  file and no open fd behind;
* fields may come after the file part (wire order must not matter);
* malformed multipart is a 400, not a 500.
"""
from __future__ import annotations

import io
import os
import socket
import tempfile
import threading
import time
import wave
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        monkeypatch.delenv("VEZIR_MAX_UPLOAD_BYTES", raising=False)
        yield Path(d)


@pytest.fixture
def private_tmp(monkeypatch):
    """A fresh TMPDIR so any spool file the server might create is visible."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("TMPDIR", d)
        tempfile.tempdir = None  # force re-resolution
        yield Path(d)
        tempfile.tempdir = None


def _wav_bytes(seconds: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * seconds)
    return buf.getvalue()


@pytest.fixture
def server(tmp_data, private_tmp, monkeypatch):
    """Real uvicorn on loopback: TestClient can't exercise disconnects or
    chunked streaming, and the leak under test lives in the ASGI body path."""
    import uvicorn

    from vezir.server import auth
    from vezir.server.app import create_app

    monkeypatch.setenv("VEZIR_MAX_UPLOAD_BYTES", str(256 * 1024))
    token = auth.issue("alice")
    app = create_app()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline and th.is_alive()
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", {"Authorization": f"Bearer {token}", "X-Team-Id": "blink"}
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def _session_dirs(tmp_data: Path) -> list[Path]:
    d = tmp_data / "sessions"
    return sorted(p for p in d.iterdir()) if d.exists() else []


def _open_tmp_fds(private_tmp: Path) -> list[str]:
    """Open fds under the private TMPDIR (pytest's own capture files live in
    the default /tmp and must not count)."""
    out = []
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith(str(private_tmp)):
            out.append(target)
    return out


def _post_until_response(base: str, headers: dict, path: str, chunks, *, chunked=True,
                         content_length: int | None = None, max_send: int = 64 << 20):
    """Send a request body chunk by chunk over a raw socket and stop as soon
    as the server answers.  Returns (status, bytes_sent) — so a server that
    rejects early is distinguishable from one that swallowed everything."""
    import select

    host, port = base.replace("http://", "").split(":")
    hdr = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
           f"Authorization: {headers['Authorization']}\r\nX-Team-Id: blink\r\n"
           "Content-Type: multipart/form-data; boundary=zz\r\n")
    if chunked:
        hdr += "Transfer-Encoding: chunked\r\n"
    else:
        hdr += f"Content-Length: {content_length}\r\n"
    hdr += "\r\n"
    s = socket.create_connection((host, int(port)), timeout=10)
    s.sendall(hdr.encode())
    sent = 0
    status = None
    buf = b""
    exhausted = False
    try:
        for c in chunks:
            r, _w, _x = select.select([s], [], [], 0)
            if r:
                buf += s.recv(65536)
                break
            try:
                s.sendall((f"{len(c):x}\r\n".encode() + c + b"\r\n") if chunked else c)
            except (BrokenPipeError, ConnectionResetError):
                break
            sent += len(c)
            if sent > max_send:
                break
        else:
            exhausted = True
            if chunked:
                try:
                    s.sendall(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
        s.settimeout(10)
        while b"\r\n\r\n" not in buf:
            try:
                more = s.recv(65536)
            except (TimeoutError, ConnectionResetError):
                break
            if not more:
                break
            buf += more
        if buf.startswith(b"HTTP/1.1 "):
            status = int(buf[9:12])
    finally:
        s.close()
    # ``sent`` includes whatever the loopback kernel buffers absorbed (a few
    # MB); ``exhausted`` is the reliable early-rejection signal.
    return status, (sent if exhausted else -sent)


# ── Content-Length pre-check ─────────────────────────────────────────────────


def test_declared_oversize_body_is_rejected_before_reading(server, tmp_data, private_tmp):
    base, headers = server
    status, sent = _post_until_response(
        base, headers, "/upload", (b"\0" * 65536 for _ in range(160)),
        chunked=False, content_length=10 * 1024 * 1024,
    )
    assert status == 413
    # Negative = the server answered while we still had 10 MiB to offer.
    assert sent < 0, sent
    assert _session_dirs(tmp_data) == []
    assert list(private_tmp.iterdir()) == []


# ── chunked oversize: cut at the cap, nothing spooled ────────────────────────


def _multipart(fields: dict[str, str], file_bytes_iter, boundary="zz", file_field="audio"):
    for k, v in fields.items():
        yield (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
        ).encode()
    yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
           f"filename=\"big.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
    yield from file_bytes_iter
    yield f"\r\n--{boundary}--\r\n".encode()


def _oversize_body(wav: bytes, total: int = 8 * 1024 * 1024):
    yield wav
    n = len(wav)
    while n < total:
        n += 65536
        yield b"\0" * 65536


def test_chunked_oversize_is_cut_at_cap_without_spooling(server, tmp_data, private_tmp):
    base, headers = server
    status, sent = _post_until_response(
        base, headers, "/upload", _multipart({"title": "t"}, _oversize_body(_wav_bytes())),
    )
    assert status == 413
    # Cap is 256 KiB; the server stopped us before the 8 MiB on offer ran out.
    assert sent < 0, sent
    assert _session_dirs(tmp_data) == []
    assert list(private_tmp.iterdir()) == []


# ── disconnect mid-upload ────────────────────────────────────────────────────


def test_disconnect_mid_upload_leaves_nothing_behind(server, tmp_data, private_tmp):
    base, headers = server
    host, port = base.replace("http://", "").split(":")
    wav = _wav_bytes()
    head = (
        f"POST /upload HTTP/1.1\r\nHost: {host}\r\n"
        f"Authorization: {headers['Authorization']}\r\nX-Team-Id: blink\r\n"
        "Content-Type: multipart/form-data; boundary=zz\r\n"
        "Transfer-Encoding: chunked\r\n\r\n"
    ).encode()
    part = (b"--zz\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"x.wav\"\r\n"
            b"Content-Type: audio/wav\r\n\r\n" + wav + b"\0" * 100_000)

    s = socket.create_connection((host, int(port)), timeout=5)
    s.sendall(head)
    s.sendall(f"{len(part):x}\r\n".encode() + part + b"\r\n")
    time.sleep(0.3)  # let the server consume the first chunk
    s.close()  # drop without terminating the chunked body

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _session_dirs(tmp_data):
        time.sleep(0.1)
    assert _session_dirs(tmp_data) == [], "partial session dir must be cleaned up"
    assert list(private_tmp.iterdir()) == [], "nothing may be spooled to TMPDIR"
    # And the server process (this process) holds no fd under TMPDIR.
    assert _open_tmp_fds(private_tmp) == []


# ── field order and malformed bodies ─────────────────────────────────────────


def test_fields_after_file_part_are_honoured(server, tmp_data):
    base, headers = server
    wav = _wav_bytes()
    body = b"".join(_multipart({}, iter([wav])))
    # Append fields AFTER the file part.
    body = body.replace(
        b"\r\n--zz--\r\n",
        b"\r\n--zz\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nLate Title\r\n"
        b"--zz\r\nContent-Disposition: form-data; name=\"personal\"\r\n\r\ntrue\r\n--zz--\r\n",
    )
    r = httpx.post(
        f"{base}/upload",
        headers={**headers, "Content-Type": "multipart/form-data; boundary=zz"},
        content=body, timeout=10,
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    from vezir.server import queue

    row = queue.get(sid)
    assert row["title"] == "Late Title"
    assert row["personal"] == 1


def test_malformed_multipart_is_400(server, tmp_data):
    base, headers = server
    r = httpx.post(
        f"{base}/upload",
        headers={**headers, "Content-Type": "multipart/form-data; boundary=zz"},
        content=b"this is not multipart at all", timeout=10,
    )
    assert r.status_code in (400, 422)
    assert _session_dirs(tmp_data) == []


def test_missing_boundary_is_400(server):
    base, headers = server
    r = httpx.post(
        f"{base}/upload",
        headers={**headers, "Content-Type": "multipart/form-data"},
        content=b"--x\r\n", timeout=10,
    )
    assert r.status_code == 400


def test_wrong_file_field_name_is_400_and_clean(server, tmp_data):
    base, headers = server
    body = b"".join(_multipart({}, iter([_wav_bytes()]), file_field="not_audio"))
    r = httpx.post(
        f"{base}/upload",
        headers={**headers, "Content-Type": "multipart/form-data; boundary=zz"},
        content=body, timeout=10,
    )
    assert r.status_code == 400
    assert _session_dirs(tmp_data) == []


# ── the same guarantees on /upload/multi, /api/sessions/import, attachments ──


@pytest.mark.parametrize(
    "path, field",
    [("/upload/multi", "audio"), ("/api/sessions/import", "files")],
)
def test_other_endpoints_cut_oversize_without_spooling(server, tmp_data, private_tmp, path, field):
    base, headers = server
    status, sent = _post_until_response(
        base, headers, path, _multipart({}, _oversize_body(_wav_bytes()), file_field=field),
    )
    assert status == 413
    assert sent < 0, sent
    assert _session_dirs(tmp_data) == []
    assert list(private_tmp.iterdir()) == []


def test_attachments_cut_oversize_without_spooling(server, tmp_data, private_tmp, monkeypatch):
    base, headers = server
    # A real session to attach to.
    r = httpx.post(
        f"{base}/upload", headers=headers,
        files={"audio": ("a.wav", _wav_bytes(), "audio/wav")}, timeout=10,
    )
    sid = r.json()["session_id"]
    status, sent = _post_until_response(
        base, headers, f"/api/sessions/{sid}/attachments",
        _multipart({}, (b"\0" * 65536 for _ in range(128)), file_field="files"),
    )
    assert status == 413
    assert sent < 0, sent
    assert not (tmp_data / "sessions" / sid / "attachments").exists()
    assert list(private_tmp.iterdir()) == []
