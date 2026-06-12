"""Vezir HTTP API client for native thin clients (TUI + scribe widget).

This module is a Python port of vezir-android's ``net/SessionApi.kt``,
``net/LabelApi.kt`` and ``net/VezirApi.kt``.  It exposes a small,
synchronous-but-cancellable-via-httpx surface that the TUI screens
(and any future native shell) can consume without re-implementing
HTTP plumbing.

Design choices:

* **httpx (already a dependency)** — no new transport.  ``httpx.Client``
  is used in a context manager per call to keep connection handling
  trivial; sessions in the TUI are short-lived enough that pooling is
  not worth the lifecycle hassle.
* **Synchronous** — Textual's worker mechanism (``@work(thread=True)``)
  is the right place to push blocking I/O off the event loop.  Keeping
  the API client itself synchronous avoids dragging asyncio semantics
  through every call site and makes the same client trivially usable
  from the CLI and the Tkinter widget.
* **Result objects** — explicit ``ApiResult`` discriminated by
  ``ok``/``http_error``/``network_error`` keeps error handling at the
  call site honest.  Mirrors the ``Result.Ok / Result.HttpError /
  Result.NetworkError`` sealed class in SessionApi.kt so the Android
  code reads as a clear reference implementation.
* **No CA pinning yet** — current Android v2 QR enrollment carries a
  Caddy CA cert and the client uses it explicitly.  The Linux/macOS
  joiners install the CA system-wide (per the wiki onboarding doc),
  so httpx's default ``verify=True`` against the system trust store
  works the same way.  When CA pinning lands here, a ``ca_pem``
  kwarg on ``VezirClient.__init__`` will mirror the kotlin signature.

Endpoint coverage (server: vezir 0.1.17):

* GET  /api/sessions
* GET  /api/sessions/{id}
* POST /api/sessions/{id}/share
* POST /api/sessions/{id}/retry-summary    (optional preset override)
* POST /session/{id}/sync                  (legacy URL kept for parity)
* GET  /api/label/{id}
* POST /api/label/{id}                     ({"labels": {...}} JSON body)
* GET  /api/team
* GET  /label/{id}/clip/{speaker_id}       (binary audio/wav)
* GET  /artifact/{id}/{name}               (binary artifact bytes)
* GET  /api/me                             (identity + memberships)
* GET  /health
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("vezir.client.api")


# ─── Result type ─────────────────────────────────────────────────────────────


@dataclass
class ApiResult:
    """Tagged result for every API call.

    Exactly one of ``ok``, ``http_error``, ``network_error`` carries a
    non-None payload.  Call ``.unwrap()`` to raise on failure or
    ``bool(result)`` to short-circuit success/failure.
    """

    ok: Any | None = None
    http_error: tuple[int, str] | None = None
    network_error: Exception | None = None

    @classmethod
    def success(cls, data: Any) -> ApiResult:
        return cls(ok=data)

    @classmethod
    def http(cls, code: int, message: str) -> ApiResult:
        return cls(http_error=(code, message))

    @classmethod
    def network(cls, exc: Exception) -> ApiResult:
        return cls(network_error=exc)

    def __bool__(self) -> bool:  # truthiness == success
        return self.ok is not None or (
            self.http_error is None and self.network_error is None
        )

    def is_ok(self) -> bool:
        return self.http_error is None and self.network_error is None

    def unwrap(self) -> Any:
        if self.http_error is not None:
            code, msg = self.http_error
            raise RuntimeError(f"HTTP {code}: {msg}")
        if self.network_error is not None:
            raise self.network_error
        return self.ok

    def error_message(self) -> str:
        """Human-readable error suitable for display in the TUI status bar."""
        if self.http_error is not None:
            code, msg = self.http_error
            base = f"server returned {code}: {msg}"
            if code == 401:
                base += "; " + _reauth_hint()
            return base
        if self.network_error is not None:
            return f"network error: {self.network_error}"
        return ""

    def is_auth_error(self) -> bool:
        """True iff this is a 401 (expired/invalid credential)."""
        return self.http_error is not None and self.http_error[0] == 401


def _reauth_hint() -> str:
    """Suggest the right re-auth action based on the active credential type.

    A nostr session (teams.json ``auth="nostr"``) expires after ~24h and
    is renewed with ``vezir login``; a bearer token is renewed by the
    operator.  Best-effort: any error reading config falls back to the
    generic hint.
    """
    try:
        from .config import load_teams_config

        cfg = load_teams_config()
        active = cfg.get("active")
        for t in cfg.get("teams", []):
            if t["id"] == active and t.get("auth") == "nostr":
                return "your nostr session may have expired — run `vezir login`"
    except Exception:
        pass
    return "credential invalid or expired — check $VEZIR_TOKEN or run `vezir login`"


# ─── Session record ──────────────────────────────────────────────────────────


@dataclass
class Session:
    """A row from ``/api/sessions`` or ``/api/sessions/{id}``.

    Mirrors ``SessionApi.kt::Session``; field names match the JSON
    response verbatim so the TUI can pass dict and dataclass forms
    interchangeably during refactors.
    """

    id: str
    status: str
    github: str | None = None
    title: str | None = None
    summary_preset: str | None = None
    auto_label_enabled: int | None = None
    sync_enabled: int | None = None
    personal: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    error: str | None = None
    summary_error: str | None = None
    sync_error: str | None = None
    team_id: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> Session:
        # The server returns artifacts as a JSON string in /api/sessions
        # (legacy compatibility with the HTML dashboard) and as a dict in
        # /api/sessions/{id} since 0.1.12.  Normalize both.
        artifacts = d.get("artifacts") or {}
        if isinstance(artifacts, str):
            import json
            try:
                artifacts = json.loads(artifacts) if artifacts else {}
            except Exception:
                artifacts = {}
        if not isinstance(artifacts, dict):
            artifacts = {}
        # Strip server-only fields we don't model.
        known = {
            "id", "status", "github", "title", "summary_preset",
            "auto_label_enabled", "sync_enabled", "personal",
            "created_at", "updated_at",
            "error", "summary_error", "sync_error",
            "team_id",
        }
        kwargs = {k: d.get(k) for k in known if k in d}
        return cls(artifacts=artifacts, **kwargs)

    @property
    def is_personal(self) -> bool:
        return bool(self.personal)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "error", "sync_failed")

    @property
    def is_active(self) -> bool:
        return self.status in (
            "queued", "transcribing", "summarizing", "syncing",
        )


@dataclass
class LabelInfo:
    """Response payload for ``GET /api/label/{id}``."""

    session_id: str
    status: str
    speakers: list[dict]
    team: list[str]
    audio_available: bool

    @classmethod
    def from_dict(cls, d: dict) -> LabelInfo:
        return cls(
            session_id=d.get("session_id", ""),
            status=d.get("status", ""),
            speakers=list(d.get("speakers") or []),
            team=list(d.get("team") or []),
            audio_available=bool(d.get("audio_available", False)),
        )


# ─── Client ──────────────────────────────────────────────────────────────────


_DEFAULT_TIMEOUT = httpx.Timeout(
    connect=15.0, read=30.0, write=30.0, pool=5.0,
)

# Larger timeout for artifact / clip downloads, which can be 10 MB+ over
# a flaky VPN tunnel.
_DOWNLOAD_TIMEOUT = httpx.Timeout(
    connect=15.0, read=120.0, write=30.0, pool=5.0,
)


class VezirClient:
    """Thin synchronous HTTP client over the vezir server API.

    Construct once per logical "session of use" — e.g. one instance for
    the lifetime of a TUI app, or one per `vezir scribe` invocation.
    Methods are thread-safe in the sense that each call opens its own
    httpx.Client; there is no shared connection pool that would force
    serialization.
    """

    def __init__(
        self,
        server_url: str,
        token: str,
        *,
        team_id: str | None = None,
        timeout: httpx.Timeout | None = None,
        verify: bool | str | None = None,
    ):
        self.base_url = server_url.rstrip("/")
        self.token = token
        # v0.7.0: every team-scoped request carries an X-Team-Id header.
        # Routes that don't need a team scope (/health, /api/me) ignore
        # it.  ``team_id`` may be None for clients that only call
        # /api/me to discover memberships before picking one.
        self.team_id = team_id
        self._timeout = timeout or _DEFAULT_TIMEOUT
        # httpx's default verify=True uses certifi.where() -- the bundled
        # public CA list -- which does NOT include our internal Caddy CA.
        # If the caller didn't specify, honor the standard OpenSSL
        # SSL_CERT_FILE env var (and the vezir-specific
        # VEZIR_CADDY_ROOT_CERT_PATH set by Caddy-deployed servers) so
        # internal-CA setups work out of the box.  Falls back to True
        # (certifi) when no env hint is present, which is correct for
        # public-CA servers.
        self._verify = self._resolve_verify(verify)

    @staticmethod
    def _resolve_verify(explicit: bool | str | None) -> bool | str:
        """Pick the right ``verify`` value for httpx.Client.

        Resolution order:
          1. explicit kwarg (caller wins)
          2. ``SSL_CERT_FILE`` env var (OpenSSL convention; the vezir
             wiki onboarding doc instructs joiners to set this)
          3. ``VEZIR_CADDY_ROOT_CERT_PATH`` (vezir-specific, set on
             muscle and any Caddy-deployed server box for use by the
             server itself; client may also pick it up if joined to
             the same shell environment)
          4. ``True`` (httpx default -> certifi public CA bundle)
        """
        if explicit is not None:
            return explicit
        import os
        for var in ("SSL_CERT_FILE", "VEZIR_CADDY_ROOT_CERT_PATH"):
            path = os.environ.get(var)
            if path and os.path.isfile(path):
                log.debug("VezirClient.verify resolved from %s=%s", var, path)
                return path
        return True

    # ── plumbing ──

    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.token}"}
        if self.team_id:
            h["X-Team-Id"] = self.team_id
        return h

    def _get(self, path: str, *, timeout: httpx.Timeout | None = None) -> ApiResult:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(
                timeout=timeout or self._timeout, verify=self._verify,
            ) as c:
                r = c.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            return ApiResult.network(exc)
        if r.status_code != 200:
            return ApiResult.http(r.status_code, r.text[:200])
        try:
            return ApiResult.success(r.json())
        except Exception as exc:  # json decode error
            return ApiResult.network(exc)

    def _get_bytes(self, path: str) -> ApiResult:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(
                timeout=_DOWNLOAD_TIMEOUT, verify=self._verify,
            ) as c:
                r = c.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            return ApiResult.network(exc)
        if r.status_code != 200:
            return ApiResult.http(r.status_code, r.text[:200])
        return ApiResult.success(r.content)

    def _post(
        self,
        path: str,
        *,
        json: dict | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> ApiResult:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(
                timeout=timeout or self._timeout, verify=self._verify,
            ) as c:
                r = c.post(
                    url,
                    headers={
                        **self._headers(),
                        "Content-Type": "application/json",
                    },
                    json=json if json is not None else {},
                )
        except httpx.HTTPError as exc:
            return ApiResult.network(exc)
        if not (200 <= r.status_code < 300):
            return ApiResult.http(r.status_code, r.text[:200])
        # Many POSTs return empty / non-JSON; try JSON, fall back to True.
        try:
            return ApiResult.success(r.json())
        except Exception:
            return ApiResult.success(True)

    # ── sessions ──

    def get_sessions(
        self,
        limit: int = 50,
        since: str | None = None,
    ) -> ApiResult:
        """List sessions visible to the current bearer.

        Returns ApiResult whose .ok is ``list[Session]`` on success.

        v0.7.0: *since* (ISO 8601 date/datetime) filters to sessions
        created at or after that timestamp.
        """
        path = f"/api/sessions?limit={int(limit)}"
        if since is not None:
            path += f"&since={quote(since, safe='')}"
        result = self._get(path)
        if not result.is_ok():
            return result
        raw = result.ok
        items = raw.get("sessions") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return ApiResult.network(
                ValueError(f"unexpected /api/sessions payload: {type(raw)}"),
            )
        return ApiResult.success([Session.from_dict(s) for s in items])

    def get_session(self, session_id: str) -> ApiResult:
        result = self._get(f"/api/sessions/{quote(session_id, safe='')}")
        if not result.is_ok():
            return result
        return ApiResult.success(Session.from_dict(result.ok))

    def share_with_team(self, session_id: str) -> ApiResult:
        return self._post(
            f"/api/sessions/{quote(session_id, safe='')}/share",
        )

    def retry_summary(
        self,
        session_id: str,
        *,
        preset: str | None = None,
        language: str | None = None,
    ) -> ApiResult:
        body: dict = {}
        if preset:
            body["preset"] = preset
        # "auto" (or None) means use the transcript's detected language; only
        # send an explicit override so the server can preserve the primary.
        if language and language != "auto":
            body["language"] = language
        return self._post(
            f"/api/sessions/{quote(session_id, safe='')}/retry-summary",
            json=body,
        )

    def sync_now(
        self, session_id: str, meeting_type: str | None = None
    ) -> ApiResult:
        # Legacy URL (no /api prefix); the Android client uses the same.
        # ``meeting_type`` is the optional "sync as" folder override.
        body = {"meeting_type": meeting_type} if meeting_type else None
        return self._post(
            f"/session/{quote(session_id, safe='')}/sync", json=body
        )

    # ── artifacts ──

    def download_artifact(self, session_id: str, name: str) -> ApiResult:
        """Return the raw bytes of an artifact (transcript, summary, etc.)."""
        path = f"/artifact/{quote(session_id, safe='')}/{quote(name, safe='')}"
        return self._get_bytes(path)

    def save_artifact(
        self,
        session_id: str,
        name: str,
        dest: Path,
    ) -> ApiResult:
        """Download an artifact and write it to ``dest``.

        ``dest``'s parent is created with mkdir(parents=True, exist_ok=True).
        Returns ApiResult whose .ok is the dest Path on success.
        """
        result = self.download_artifact(session_id, name)
        if not result.is_ok():
            return result
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(result.ok)
        return ApiResult.success(dest)

    # ── labeling ──

    def get_label_info(self, session_id: str) -> ApiResult:
        result = self._get(f"/api/label/{quote(session_id, safe='')}")
        if not result.is_ok():
            return result
        return ApiResult.success(LabelInfo.from_dict(result.ok))

    # Label submission timeout: longer than the default 30s read timeout
    # because apply_labels() relabels the transcript synchronously before
    # returning.  The expensive voiceprint update runs in a background
    # thread (v0.6.9+), but the transcript rewrite can still take a few
    # seconds for long sessions.
    _LABEL_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=5.0)

    def submit_labels(
        self,
        session_id: str,
        labels: dict[str, str],
    ) -> ApiResult:
        return self._post(
            f"/api/label/{quote(session_id, safe='')}",
            json={"labels": dict(labels)},
            timeout=self._LABEL_TIMEOUT,
        )

    def get_team(self) -> ApiResult:
        """Return the team handles list (for autocomplete)."""
        result = self._get("/api/team")
        if not result.is_ok():
            return result
        team = result.ok.get("team") if isinstance(result.ok, dict) else None
        if not isinstance(team, list):
            return ApiResult.network(
                ValueError(f"unexpected /api/team payload: {type(result.ok)}"),
            )
        return ApiResult.success([str(t) for t in team])

    def download_clip(self, session_id: str, speaker_id: str) -> ApiResult:
        """Return raw WAV bytes of a speaker clip for in-TUI playback."""
        path = (
            f"/label/{quote(session_id, safe='')}"
            f"/clip/{quote(speaker_id, safe='')}"
        )
        return self._get_bytes(path)

    def save_clip(
        self,
        session_id: str,
        speaker_id: str,
        dest: Path,
    ) -> ApiResult:
        result = self.download_clip(session_id, speaker_id)
        if not result.is_ok():
            return result
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(result.ok)
        return ApiResult.success(dest)

    # ── health ──

    def health(self) -> ApiResult:
        """Cheap GET /health for connectivity smoke tests."""
        return self._get("/health")

    def get_me(self) -> ApiResult:
        """GET /api/me -- returns identity + every team membership.

        v0.7.0 shape:

            {
              "github": "alice",
              "is_admin": false,
              "memberships": [
                {"team_id": "blink", "team_name": "Blink", "role": "scribe"},
                ...
              ],
              "alternate_urls": [...]
            }

        Used by ``vezir doctor`` to validate the active token end-to-end
        and by the TUI to populate the team-picker.
        """
        return self._get("/api/me")
