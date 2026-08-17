"""Multipart upload to the vezir service with retry."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

log = logging.getLogger("vezir.client.uploader")

ACCEPTED_AUDIO_EXTS = {".wav", ".ogg", ".mp3"}
CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
}

ProgressCallback = Callable[[int, int, float], None]
RetryCallback = Callable[[int, int, Exception], None]
# Called on a 401; returns a fresh access token (rotated + persisted) or
# None when refresh isn't possible (no refresh token / server rejected).
RefreshCallback = Callable[[], "str | None"]


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


def _user_agent() -> str:
    """``vezir-cli/<version>`` for the User-Agent header (server records it)."""
    try:
        from vezir import __version__
        return f"vezir-cli/{__version__}"
    except Exception:
        return "vezir-cli/?"


def _auth_headers(token: str, team_id: str | None) -> dict:
    """Bearer + (when known) X-Team-Id, matching the server's v0.7.0 contract.

    Also sends ``User-Agent: vezir-cli/<version>`` so the server can record
    which client (and version) produced each upload.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": _user_agent(),
    }
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
    refresh_cb: RefreshCallback | None = None,
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
    tok = {"v": token}

    audio_path = validate_audio_path(audio_path)

    # Pick a content-type matching the file extension.
    ext = audio_path.suffix.lower()
    content_type = CONTENT_TYPES[ext]
    expected_bytes = audio_path.stat().st_size

    # Idempotency key: stable for the whole call so a retry after a
    # lost/late response can be deduped server-side instead of creating a
    # SECOND session (double GPU work + duplicate meeting in the team repo).
    # A retry re-POSTs the whole file, but the server keys on this and
    # returns the already-committed session (M2).
    import uuid as _uuid
    idempotency_key = _uuid.uuid4().hex

    last_exc: Exception | None = None
    refreshed_once = False
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
                    headers = _auth_headers(tok["v"], team_id)
                    headers["Idempotency-Key"] = idempotency_key
                    resp = client.post(
                        url, headers=headers,
                        files=files, data=data,
                    )
            # Silent refresh-and-retry once on a 401 (0.10.1).
            if resp.status_code == 401 and not refreshed_once \
                    and refresh_cb is not None:
                new = refresh_cb()
                if new:
                    tok["v"] = new
                    refreshed_once = True
                    continue
            if resp.status_code == 200:
                result = resp.json()
                # An idempotent replay (server already committed this upload
                # on a prior, response-lost attempt) carries no byte count —
                # the session already exists, so skip the size assertion.
                if not result.get("idempotent") \
                        and result.get("bytes") != expected_bytes:
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


def upload_multi(
    server_url: str,
    token: str,
    audio_paths: list[Path],
    title: str | None = None,
    summary_preset: str | None = None,
    auto_label: bool = True,
    sync: bool = True,
    personal: bool = False,
    timeout: float = 600.0,
    retries: int = 5,
    on_retry: RetryCallback | None = None,
    verify: bool | str | None = None,
    team_id: str | None = None,
    refresh_cb: RefreshCallback | None = None,
) -> dict:
    """POST multiple audio files as one meeting to <server_url>/upload/multi.

    The files are sent in the order given; the server preserves that order
    and the worker concatenates them (filename order on disk) before
    transcribe.  Returns the JSON response: {session_id, bytes, parts}.

    Whole-batch retry on connection errors / 5xx (one bucket hit per
    meeting).  A 404/405 means the server predates v0.9.0; the caller
    should surface a clear "server too old" message.
    """
    if not audio_paths:
        raise ValueError("upload_multi requires at least one audio file")

    url = server_url.rstrip("/") + "/upload/multi"
    tok = {"v": token}

    paths = [validate_audio_path(p) for p in audio_paths]
    exts = {p.suffix.lower() for p in paths}
    if len(exts) > 1:
        raise ValueError(
            f"all parts must share one audio type; got {sorted(exts)}"
        )
    expected_bytes = sum(p.stat().st_size for p in paths)

    from .api import VezirClient
    resolved_verify = VezirClient._resolve_verify(verify)

    last_exc: Exception | None = None
    refreshed_once = False
    for attempt in range(1, retries + 1):
        opened: list = []
        try:
            files = []
            for p in paths:
                f = p.open("rb")
                opened.append(f)
                files.append(
                    ("audio", (p.name, f, CONTENT_TYPES[p.suffix.lower()]))
                )
            data = {"audio_bytes": str(expected_bytes)}
            if title:
                data["title"] = title
            if summary_preset:
                data["summary_preset"] = summary_preset
            data["auto_label"] = "true" if auto_label else "false"
            data["sync"] = "true" if sync else "false"
            if personal:
                data["personal"] = "true"
            with httpx.Client(timeout=timeout, verify=resolved_verify) as client:
                resp = client.post(
                    url, headers=_auth_headers(tok["v"], team_id),
                    files=files, data=data,
                )
            # Silent refresh-and-retry once on a 401 (0.10.1).
            if resp.status_code == 401 and not refreshed_once \
                    and refresh_cb is not None:
                new = refresh_cb()
                if new:
                    tok["v"] = new
                    refreshed_once = True
                    continue
            if resp.status_code == 200:
                result = resp.json()
                if result.get("bytes") != expected_bytes:
                    raise RuntimeError(
                        f"upload byte mismatch: server received "
                        f"{result.get('bytes')} but local parts total "
                        f"{expected_bytes} bytes"
                    )
                return result
            if resp.status_code in (404, 405):
                raise RuntimeError(
                    "server does not support multi-audio uploads "
                    "(requires vezir >= 0.9.0)"
                )
            if 500 <= resp.status_code < 600:
                log.warning(
                    "multi upload attempt %d/%d: server %d %s",
                    attempt, retries, resp.status_code, resp.text[:200],
                )
            else:
                resp.raise_for_status()
                return resp.json()
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
        ) as exc:
            will_retry = attempt < retries
            log.warning(
                "multi upload attempt %d/%d failed%s: %s",
                attempt, retries,
                "; retrying" if will_retry else "", exc,
            )
            if will_retry and on_retry is not None:
                on_retry(attempt, retries, exc)
            last_exc = exc
        finally:
            for f in opened:
                f.close()
        if attempt < retries:
            time.sleep(min(2 ** attempt, 30))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"multi upload failed after {retries} attempts")


# ─── Resumable upload client (tus subset, v0.7.3+) ───────────────────────────

_RESUMABLE_NET_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)


def upload_attachments(
    server_url: str,
    token: str,
    session_id: str,
    paths: list[Path],
    *,
    team_id: str | None = None,
    timeout: float = 600.0,
    retries: int = 3,
    verify: bool | str | None = None,
) -> list[dict]:
    """POST supporting documents to an existing session.

    Sent after the audio, once the session id is known — the audio upload
    itself is unchanged (it has three implementations: one-shot, multi and
    resumable).  Returns the server's stored-attachment descriptors
    (``{name, size, content_type}``); the stored name can differ from the
    local one when the server sanitizes or de-duplicates it.

    Retries only on connection errors: the request is not idempotent
    server-side, so a retry after a 5xx could store a second copy.
    """
    from .api import VezirClient

    if not paths:
        return []
    url = f"{server_url.rstrip('/')}/api/sessions/{session_id}/attachments"
    resolved_verify = VezirClient._resolve_verify(verify)

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        handles = []
        try:
            files = []
            for p in paths:
                fh = p.open("rb")
                handles.append(fh)
                files.append(("files", (p.name, fh, "application/octet-stream")))
            with httpx.Client(timeout=timeout, verify=resolved_verify) as client:
                resp = client.post(
                    url, headers=_auth_headers(token, team_id), files=files,
                )
            if resp.status_code == 404:
                raise RuntimeError(
                    "server does not know this session (or it belongs to "
                    "another team); attachments not stored"
                )
            resp.raise_for_status()
            return resp.json().get("attachments", [])
        except _RESUMABLE_NET_ERRORS as exc:
            log.warning(
                "attachment upload attempt %d/%d failed: %s", attempt, retries, exc,
            )
            last_exc = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 30))
        finally:
            for fh in handles:
                fh.close()
    raise last_exc or RuntimeError("attachment upload failed")


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
    refresh_cb: RefreshCallback | None = None,
) -> dict:
    """Upload via the tus-subset resumable protocol with offset-resume.

    Creates an upload session (POST /upload/resumable), then PATCHes the
    file in ``chunk_bytes`` pieces.  On a network error it HEADs the
    session to learn the server's current offset and resumes from there
    instead of restarting at byte 0.  Returns the final JSON
    ``{"session_id", "bytes"}``.

    ``refresh_cb``: called on a ``401`` to silently rotate the session
    token (0.10.1).  When it returns a new token the request is retried
    with it; only after a refresh also 401s (or no callback) does the
    upload fail with the 401 so the caller can prompt a re-login.  This is
    what makes an upload after a lapsed 60-min access token "just work".

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

    # Mutable token box so a mid-upload refresh updates every subsequent
    # request (create / PATCH / HEAD) within this call.
    tok = {"v": token}

    def _refresh() -> bool:
        if refresh_cb is None:
            return False
        new = refresh_cb()
        if not new:
            return False
        tok["v"] = new
        return True

    data: dict[str, str] = {}
    if title:
        data["title"] = title
    if summary_preset:
        data["summary_preset"] = summary_preset
    data["auto_label"] = "true" if auto_label else "false"
    data["sync"] = "true" if sync else "false"
    if personal:
        data["personal"] = "true"

    def _create_headers() -> dict:
        h = _auth_headers(tok["v"], team_id)
        h.update({
            "Upload-Length": str(total),
            "Upload-Filename": audio_path.name,
            "Upload-Content-Type": content_type,
        })
        return h

    # 1. Create the session (refresh-and-retry once on 401).
    with httpx.Client(timeout=timeout, verify=resolved_verify) as client:
        resp = client.post(base + "/upload/resumable",
                           headers=_create_headers(), data=data)
        if resp.status_code == 401 and _refresh():
            resp = client.post(base + "/upload/resumable",
                               headers=_create_headers(), data=data)
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
                    h = client.head(location, headers=_auth_headers(tok["v"], team_id))
                    if h.status_code == 401 and _refresh():
                        h = client.head(
                            location, headers=_auth_headers(tok["v"], team_id))
                    if h.status_code == 200:
                        offset = int(h.headers.get("Upload-Offset", offset))

                # Bound the in-loop retry paths so a server that keeps
                # answering 401/409/429 without progress can't spin forever
                # inside a single attempt — after the budget we break out to
                # the outer per-attempt backoff/deadline machinery.
                refreshes_left = 2
                stalls_left = 8  # combined 409 re-sync + 429 pause budget
                with audio_path.open("rb") as f:
                    f.seek(offset)
                    while offset < total:
                        chunk = f.read(chunk_bytes)
                        if not chunk:
                            break
                        patch_headers = _auth_headers(tok["v"], team_id)
                        patch_headers.update({
                            "Upload-Offset": str(offset),
                            "Content-Type": "application/offset+octet-stream",
                        })
                        r = client.patch(location, headers=patch_headers,
                                         content=chunk)
                        if r.status_code == 401:
                            # Token rotated mid-upload; re-send same chunk
                            # (offset unchanged) with the fresh token.  Bound
                            # the refreshes so a persistent 401 (e.g. revoked
                            # membership) can't loop indefinitely.
                            if refreshes_left > 0 and _refresh():
                                refreshes_left -= 1
                                f.seek(offset)
                                continue
                            r.raise_for_status()
                        if r.status_code == 409:
                            # Offset drift — re-sync and retry the loop.
                            if stalls_left <= 0:
                                raise httpx.RemoteProtocolError(
                                    "repeated 409 offset drift with no progress",
                                    request=r.request)
                            stalls_left -= 1
                            offset = int(r.headers.get("Upload-Offset", offset))
                            f.seek(offset)
                            continue
                        if r.status_code == 429:
                            # Rate limited (e.g. an old server that still
                            # buckets PATCH chunks).  Honor Retry-After and
                            # re-send the SAME chunk (offset unchanged), so
                            # the upload pauses instead of hard-failing.
                            if stalls_left <= 0:
                                raise httpx.RemoteProtocolError(
                                    "persistent 429 with no progress",
                                    request=r.request)
                            stalls_left -= 1
                            try:
                                wait = float(r.headers.get("Retry-After", "5"))
                            except ValueError:
                                wait = 5.0
                            wait = min(max(wait, 1.0), 60.0)
                            log.warning(
                                "resumable upload rate-limited at offset "
                                "%d; waiting %.0fs (Retry-After)",
                                offset, wait,
                            )
                            time.sleep(wait)
                            f.seek(offset)
                            continue
                        if 500 <= r.status_code < 600:
                            raise httpx.RemoteProtocolError(
                                f"server {r.status_code}", request=r.request)
                        r.raise_for_status()
                        new_offset = int(
                            r.headers.get("Upload-Offset", offset + len(chunk)))
                        # Reset the stall budget whenever we actually advance.
                        if new_offset > offset:
                            stalls_left = 8
                        offset = new_offset
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
