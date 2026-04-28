"""Multipart upload to the vezir service with retry."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

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
    from meet_record.audio import compress_audio

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


def upload(
    server_url: str,
    token: str,
    audio_path: Path,
    title: str | None = None,
    timeout: float = 600.0,
    retries: int = 3,
    progress: ProgressCallback | None = None,
    on_retry: RetryCallback | None = None,
) -> dict:
    """POST audio to <server_url>/upload. Returns the JSON response.

    Retries on connection errors and 5xx responses with exponential backoff.
    Raises httpx.HTTPError on permanent failure.
    """
    url = server_url.rstrip("/") + "/upload"
    headers = {"Authorization": f"Bearer {token}"}

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
                with httpx.Client(timeout=timeout) as client:
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
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
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
            time.sleep(2 ** attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"upload failed after {retries} attempts")
