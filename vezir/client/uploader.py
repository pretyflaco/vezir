"""Multipart upload to the vezir service with retry."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger("vezir.client.uploader")


def upload(
    server_url: str,
    token: str,
    audio_path: Path,
    title: str | None = None,
    timeout: float = 600.0,
    retries: int = 3,
) -> dict:
    """POST audio to <server_url>/upload. Returns the JSON response.

    Retries on connection errors and 5xx responses with exponential backoff.
    Raises httpx.HTTPError on permanent failure.
    """
    url = server_url.rstrip("/") + "/upload"
    headers = {"Authorization": f"Bearer {token}"}

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with audio_path.open("rb") as f:
                files = {"audio": (audio_path.name, f, "audio/wav")}
                data = {}
                if title:
                    data["title"] = title
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, headers=headers, files=files, data=data)
            if resp.status_code == 200:
                return resp.json()
            if 500 <= resp.status_code < 600:
                log.warning(
                    "upload attempt %d/%d: server %d %s",
                    attempt, retries, resp.status_code, resp.text[:200],
                )
            else:
                resp.raise_for_status()
                return resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            log.warning(
                "upload attempt %d/%d failed: %s", attempt, retries, exc
            )
            last_exc = exc
        if attempt < retries:
            time.sleep(2 ** attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"upload failed after {retries} attempts")
