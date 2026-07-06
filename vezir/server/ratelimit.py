"""Lightweight in-memory token-bucket rate limiter.

Why not slowapi/limits
----------------------
This package follows a "thin dependency tree" policy: 4 base deps + 5
server extras, nothing else. Slowapi pulls in ``limits`` plus a redis
optional and a moderate-sized config surface. For a single-process,
single-worker dogfood deployment, a 100-line bucket suffices and stays
in our test surface.

Scope
-----
* Per-token (or per-IP for unauthenticated paths) token bucket.
* Limits picked for dogfood: /upload 10/min, /login 20/min, /api/* 60/min.
* No persistence: bucket state lives in process memory. Resets on
  ``vezir serve`` restart. Acceptable because the bucket is defence-in-
  depth on top of bearer auth, not the primary access control.

Bypass
------
Set ``VEZIR_DISABLE_RATELIMIT=1`` to disable entirely (useful for tests
that hammer the API and for CI). On by default in production.

Headers
-------
On 429 we set ``Retry-After: <seconds>`` so well-behaved clients (e.g.
the Android uploader already implementing exponential backoff) can pause
without operator intervention.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

log = logging.getLogger("vezir.ratelimit")


@dataclass
class Bucket:
    """One token bucket. ``tokens`` is a float so refill can be fractional."""
    tokens: float
    last_refill: float


class _Limiter:
    """In-memory limiter keyed by an arbitrary string.

    ``capacity`` tokens, refilled linearly at ``capacity / window_sec`` per
    second. A burst up to ``capacity`` is allowed; sustained rate cannot
    exceed ``capacity`` per ``window_sec``.
    """

    def __init__(self, capacity: int, window_sec: float):
        self.capacity = float(capacity)
        self.window_sec = float(window_sec)
        self._buckets: dict[str, Bucket] = {}
        self._lock = threading.Lock()

    def _refill(self, b: Bucket, now: float) -> None:
        elapsed = max(0.0, now - b.last_refill)
        b.tokens = min(self.capacity, b.tokens + elapsed * (self.capacity / self.window_sec))
        b.last_refill = now

    def check(self, key: str) -> tuple[bool, float]:
        """Try to consume 1 token. Returns (allowed, retry_after_seconds).

        ``retry_after`` is 0.0 when allowed, else the time the caller must
        wait before another single-token request would succeed.
        """
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = Bucket(tokens=self.capacity, last_refill=now)
                self._buckets[key] = b
            self._refill(b, now)
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0.0
            deficit = 1.0 - b.tokens
            retry_after = deficit * (self.window_sec / self.capacity)
            return False, retry_after

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._buckets.clear()


# Concrete buckets per protected route family. Tuned for dogfood: large
# enough that no human-driven workflow ever trips them, small enough that
# a runaway client loop is visibly slowed.
_LIMITERS = {
    "upload": _Limiter(capacity=10, window_sec=60.0),
    "login": _Limiter(capacity=20, window_sec=60.0),
    "api": _Limiter(capacity=60, window_sec=60.0),
}


def _disabled() -> bool:
    return os.environ.get("VEZIR_DISABLE_RATELIMIT", "").lower() in ("1", "true", "yes")


def warn_if_disabled(log: logging.Logger | None = None) -> None:
    """Emit a loud warning at startup if rate limiting is disabled.

    ``VEZIR_DISABLE_RATELIMIT=1`` removes ALL auth rate limiting (it exists
    for tests).  If it ever leaks from a CI/test env into a real deployment,
    brute-force protection vanishes silently — so make it noisy.
    """
    if _disabled():
        (log or logging.getLogger("vezir.ratelimit")).warning(
            "VEZIR_DISABLE_RATELIMIT is set: ALL auth/API rate limiting is "
            "OFF. This is for tests only — never set it in production."
        )


def _client_key(request: Request, name_prefix: str, *, ip_only: bool = False) -> str:
    """Build a stable key for this request.

    Prefers the bearer token (so per-user fairness survives multi-IP
    clients on shared NAT); falls back to client IP.

    ``ip_only=True`` is REQUIRED for unauthenticated route families
    (login, refresh): there the bearer value is attacker-controlled and
    unvalidated, so keying on it would hand out a fresh bucket per random
    header — a total bypass of the brute-force limit.
    """
    if not ip_only:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            parts = auth_header.split(None, 1)
            bearer = parts[1].strip() if len(parts) > 1 else ""
            if bearer:
                # Hash the bearer to avoid keeping the plaintext as a dict key.
                import hashlib
                return f"{name_prefix}:tok:{hashlib.sha256(bearer.encode()).hexdigest()[:16]}"
    # Unauthenticated family, or empty/malformed bearer → per-IP keying.
    # (uvicorn runs with proxy_headers=True + forwarded_allow_ips=127.0.0.1
    # in `vezir serve`, so request.client.host is the real client IP behind
    # the loopback Caddy proxy, not the proxy's own address.)
    ip = request.client.host if request.client else "?"
    return f"{name_prefix}:ip:{ip}"


def _enforce(name: str, request: Request, *, ip_only: bool = False) -> None:
    if _disabled():
        return
    limiter = _LIMITERS[name]
    key = _client_key(request, name, ip_only=ip_only)
    allowed, retry_after = limiter.check(key)
    if allowed:
        return
    retry_int = max(1, int(retry_after + 0.999))
    log.info(
        "ratelimit %s: blocked key=%s retry_after=%ss",
        name, key, retry_int,
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"rate limit exceeded for {name}; retry after {retry_int}s",
        headers={"Retry-After": str(retry_int)},
    )


# ── FastAPI dependencies ────────────────────────────────────────────────────


def limit_upload(request: Request) -> None:
    """Dependency: enforce the upload bucket. Apply to POST /upload."""
    _enforce("upload", request)


def limit_login(request: Request) -> None:
    """Dependency: enforce the login bucket. Apply to /login GET+POST
    and every other unauthenticated auth route (refresh, device poll).

    Login is unauthenticated, so this is the per-IP defence against
    code/token spraying.  Always keyed by IP: a presented bearer here is
    unvalidated attacker input and must never select the bucket.
    """
    _enforce("login", request, ip_only=True)


def limit_api(request: Request) -> None:
    """Dependency: enforce the api bucket. Apply to /api/* routes."""
    _enforce("api", request)


def _reset_for_tests() -> None:
    """Clear all bucket state. Tests only."""
    for lim in _LIMITERS.values():
        lim._reset_for_tests()
