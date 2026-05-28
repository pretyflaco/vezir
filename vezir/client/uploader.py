"""Multipart upload to the vezir service with retry."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

log = logging.getLogger("vezir.client.uploader")

ACCEPTED_AUDIO_EXTS = {".wav", ".ogg"}
CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}

ProgressCallback = Callable[[int, int, float], None]
RetryCallback = Callable[[int, int, Exception], None]


def validate_audio_path(audio_path: Path) -> Path:
    """Validate a user-selected upload path and return it as a Path."""
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"audio file not found: {audio_path}")
    if not audio_path.is_file():
        raise ValueError(f"audio path is not a file: {audio_path}")
    ext = audio_path.suffix.lower()
    if ext not in ACCEPTED_AUDIO_EXTS:
        allowed = ", ".join(sorted(ACCEPTED_AUDIO_EXTS))
        raise ValueError(f"unsupported audio type {ext or '(none)'}; expected {allowed}")
    return audio_path


def compress_wav_for_upload(
    audio_path: Path,
    *,
    keep_wav: bool = True,
    bitrate: str = "48k",
) -> Path:
    """Compress a WAV to OGG/Opus for upload, preserving stereo channels."""
    audio_path = validate_audio_path(audio_path)
    if audio_path.suffix.lower() != ".wav":
        return audio_path
    from millet_record.audio import compress_audio

    return compress_audio(audio_path, keep_wav=keep_wav, bitrate=bitrate)


class _ProgressReader:
    """File-like wrapper that reports upload progress as httpx reads."""

    def __init__(
        self,
        fileobj,
        *,
        total: int,
        callback: ProgressCallback | None = None,
    ):
        self._file = fileobj
        self._total = total
        self._callback = callback
        self._sent = 0
        self._started = time.monotonic()
        self._last_report = 0.0

    def read(self, size: int = -1) -> bytes:
        chunk = self._file.read(size)
        if chunk:
            self._sent += len(chunk)
            self._report(force=self._sent >= self._total)
        return chunk

    def _report(self, *, force: bool = False) -> None:
        if self._callback is None:
            return
        now = time.monotonic()
        if force or now - self._last_report >= 0.5:
            self._last_report = now
            self._callback(self._sent, self._total, now - self._started)

    def tell(self):
        return self._file.tell()

    def seek(self, offset: int, whence: int = 0):
        pos = self._file.seek(offset, whence)
        self._sent = self._file.tell()
        return pos

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._file.close()

    def __getattr__(self, name: str):
        return getattr(self._file, name)


def _auth_headers(token: str, team_id: str | None) -> dict:
    """Bearer + (when known) X-Team-Id, matching the server's v0.7.0 contract."""
    headers = {"Authorization": f"Bearer {token}"}
    if team_id:
        headers["X-Team-Id"] = team_id
    return headers


def upload(
    server_url: str,
    token: str,
    audio_path: Path,
    title: str | None = None,
    summary_preset: str | None = None,
    auto_label: bool = True,
    sync: bool = True,
    personal: bool = False,
    timeout: float = 600.0,
    retries: int = 5,
    progress: ProgressCallback | None = None,
    on_retry: RetryCallback | None = None,
    verify: bool | str | None = None,
    team_id: str | None = None,
) -> dict:
    """POST audio to <server_url>/upload. Returns the JSON response.

    ``personal=True`` flags the session as private to the uploader; the
    server forces ``sync_enabled=False`` for personal sessions regardless
    of the ``sync`` argument (see vezir/server/queue.py::enqueue), so
    callers should also pass ``sync=False`` to avoid a misleading log
    line on the client side.  Matches vezir-android 0.2.0+ semantics.

    Retries on connection errors (including timeouts) and 5xx responses
    with exponential backoff capped at 30 s. Default 5 attempts give the
    VPN tunnel ~60 s to recover from a transient drop.
    """
    url = server_url.rstrip("/") + "/upload"
    headers = _auth_headers(token, team_id)

    audio_path = validate_audio_path(audio_path)

    # Pick a content-type matching the file extension.
    ext = audio_path.suffix.lower()
    content_type = CONTENT_TYPES[ext]
    expected_bytes = audio_path.stat().st_size

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with audio_path.open("rb") as f:
                reader = _ProgressReader(f, total=expected_bytes, callback=progress)
                files = {"audio": (audio_path.name, reader, content_type)}
                data = {"audio_bytes": str(expected_bytes)}
                if title:
                    data["title"] = title
                if summary_preset:
                    data["summary_preset"] = summary_preset
                # Privacy toggles sent as strings so FastAPI's Form
                # parsing is identical across clients.  Always send so
                # the user's explicit choice is recorded; server treats
                # absent fields as True (back-compat with older clients).
                data["auto_label"] = "true" if auto_label else "false"
                data["sync"] = "true" if sync else "false"
                # Personal flag: when true, the server forces sync_enabled
                # to false (see vezir/server/uploads.py).  Only send when
                # set so the wire stays clean for the common case.
                if personal:
                    data["personal"] = "true"
                # Reuse VezirClient's CA discovery so internal-CA
                # setups (Caddy) work without per-call wiring.  Lazy
                # import avoids a circular dependency at module load.
                from .api import VezirClient
                resolved_verify = VezirClient._resolve_verify(verify)
                with httpx.Client(
                    timeout=timeout, verify=resolved_verify,
                ) as client:
                    resp = client.post(url, headers=headers, files=files, data=data)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("bytes") != expected_bytes:
                    raise RuntimeError(
                        f"upload byte mismatch: server received {result.get('bytes')} "
                        f"but local file is {expected_bytes} bytes"
                    )
                return result
            if 500 <= resp.status_code < 600:
                log.warning(
                    "upload attempt %d/%d: server %d %s",
                    attempt, retries, resp.status_code, resp.text[:200],
                )
            else:
                resp.raise_for_status()
                result = resp.json()
                if result.get("bytes") != expected_bytes:
                    raise RuntimeError(
                        f"upload byte mismatch: server received {result.get('bytes')} "
                        f"but local file is {expected_bytes} bytes"
                    )
                return result
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
        ) as exc:
            will_retry = attempt < retries
            log.warning(
                "upload attempt %d/%d failed%s: %s",
                attempt,
                retries,
                "; retrying from byte 0" if will_retry else "",
                exc,
            )
            if will_retry and on_retry is not None:
                on_retry(attempt, retries, exc)
            last_exc = exc
        if attempt < retries:
            time.sleep(min(2 ** attempt, 30))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"upload failed after {retries} attempts")


# ─── Resumable upload client (tus subset, v0.7.3+) ───────────────────────────

_RESUMABLE_NET_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)


def server_supports_resumable(
    server_url: str, token: str, *, verify: bool | str | None = None,
    team_id: str | None = None, timeout: float = 10.0,
) -> bool:
    """Probe whether the server exposes the resumable endpoints.

    A 0-length create with a bogus length returns 400 (endpoint exists);
    a 404/405 means the server predates resumable support.  We use a
    cheap OPTIONS-style probe: POST with Upload-Length=0 → 400 if present.
    """
    from .api import VezirClient
    url = server_url.rstrip("/") + "/upload/resumable"
    headers = {**_auth_headers(token, team_id), "Upload-Length": "0"}
    try:
        with httpx.Client(
            timeout=timeout, verify=VezirClient._resolve_verify(verify),
        ) as client:
            resp = client.post(url, headers=headers)
        # 400 (invalid length) or 201 means the route exists; 404/405 means no.
        return resp.status_code not in (404, 405)
    except httpx.HTTPError:
        return False


def upload_resumable(
    server_url: str,
    token: str,
    audio_path: Path,
    title: str | None = None,
    summary_preset: str | None = None,
    auto_label: bool = True,
    sync: bool = True,
    personal: bool = False,
    timeout: float = 600.0,
    retries: int = 5,
    chunk_bytes: int = 4 * 1024 * 1024,
    progress: ProgressCallback | None = None,
    on_retry: RetryCallback | None = None,
    verify: bool | str | None = None,
    team_id: str | None = None,
) -> dict:
    """Upload via the tus-subset resumable protocol with offset-resume.

    Creates an upload session (POST /upload/resumable), then PATCHes the
    file in ``chunk_bytes`` pieces.  On a network error it HEADs the
    session to learn the server's current offset and resumes from there
    instead of restarting at byte 0.  Returns the final JSON
    ``{"session_id", "bytes"}``.

    Falls back to nothing here — callers that want a legacy fallback
    should catch and call :func:`upload`.
    """
    from .api import VezirClient
    audio_path = validate_audio_path(audio_path)
    ext = audio_path.suffix.lower()
    content_type = CONTENT_TYPES[ext]
    total = audio_path.stat().st_size
    resolved_verify = VezirClient._resolve_verify(verify)
    base = server_url.rstrip("/")

    # 1. Create the session.
    create_headers = _auth_headers(token, team_id)
    create_headers.update({
        "Upload-Length": str(total),
        "Upload-Filename": audio_path.name,
        "Upload-Content-Type": content_type,
    })
    data: dict[str, str] = {}
    if title:
        data["title"] = title
    if summary_preset:
        data["summary_preset"] = summary_preset
    data["auto_label"] = "true" if auto_label else "false"
    data["sync"] = "true" if sync else "false"
    if personal:
        data["personal"] = "true"

    with httpx.Client(timeout=timeout, verify=resolved_verify) as client:
        resp = client.post(base + "/upload/resumable",
                           headers=create_headers, data=data)
        resp.raise_for_status()
        upload_id = resp.json()["upload_id"]
    location = f"{base}/upload/resumable/{upload_id}"

    started = time.monotonic()

    def _report(sent: int) -> None:
        if progress is not None:
            progress(sent, total, time.monotonic() - started)

    offset = 0
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout, verify=resolved_verify) as client:
                # On a retry, re-sync the offset from the server (HEAD).
                if attempt > 1:
                    h = client.head(location, headers=_auth_headers(token, team_id))
                    if h.status_code == 200:
                        offset = int(h.headers.get("Upload-Offset", offset))

                with audio_path.open("rb") as f:
                    f.seek(offset)
                    while offset < total:
                        chunk = f.read(chunk_bytes)
                        if not chunk:
                            break
                        patch_headers = _auth_headers(token, team_id)
                        patch_headers.update({
                            "Upload-Offset": str(offset),
                            "Content-Type": "application/offset+octet-stream",
                        })
                        r = client.patch(location, headers=patch_headers,
                                         content=chunk)
                        if r.status_code == 409:
                            # Offset drift — re-sync and retry the loop.
                            offset = int(r.headers.get("Upload-Offset", offset))
                            f.seek(offset)
                            continue
                        if 500 <= r.status_code < 600:
                            raise httpx.RemoteProtocolError(
                                f"server {r.status_code}", request=r.request)
                        r.raise_for_status()
                        offset = int(r.headers.get("Upload-Offset", offset + len(chunk)))
                        _report(offset)
                        if r.status_code == 200:
                            # Completed — final body carries session_id.
                            return r.json()
            # Loop exhausted without a 200 (shouldn't happen) — re-HEAD.
        except _RESUMABLE_NET_ERRORS as exc:
            will_retry = attempt < retries
            log.warning(
                "resumable upload attempt %d/%d failed%s: %s",
                attempt, retries,
                f"; resuming from offset {offset}" if will_retry else "",
                exc,
            )
            if will_retry and on_retry is not None:
                on_retry(attempt, retries, exc)
            last_exc = exc
        if attempt < retries:
            time.sleep(min(2 ** attempt, 30))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"resumable upload failed after {retries} attempts")
