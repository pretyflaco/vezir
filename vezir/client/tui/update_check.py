"""Background poll for a newer vezir release on PyPI.

Some users didn't realise a newer vezir was available -- an update that
would have fixed the problem they were hitting.  This module polls PyPI
for the latest published version, compares it against the running
``vezir.__version__``, and -- when a newer release exists -- nudges the
user with the exact upgrade command for how their copy was installed.

Design mirrors ``notify.py`` (the labeling poll): a Textual
``set_interval`` timer drives a ``@work(thread=True)`` worker so the
network call never blocks the UI, a small state dataclass avoids
re-nagging within a launch, and both a desktop notification and an
in-app toast are fired.

There is deliberately NO in-app self-update: vezir is a pip/pipx package
and the safe, honest thing is to show the command the user should run.
The command is chosen from how the current process was installed
(editable checkout vs. pipx-managed venv vs. plain pip).

The 6h cadence is cached across launches in ``client.json``
(``last_update_check`` timestamp + ``last_seen_latest`` version) so a
user who restarts the TUI often doesn't hammer PyPI.  Skipped entirely
under ``VEZIR_TUI_DISABLE_UPDATE_CHECK=1`` (set in tests).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

from textual import work

from ..audio import notify_desktop
from ..config import load_client_prefs, save_client_prefs

log = logging.getLogger("vezir.client.tui.update_check")


PYPI_URL = "https://pypi.org/pypi/vezir/json"

# Check at most every 6h.  The interval timer also fires roughly this
# often, but the cache in client.json is the real gate across launches.
UPDATE_POLL_INTERVAL = 6 * 60 * 60.0

# First check runs shortly after mount (not immediately) so startup
# stays snappy and the user isn't toasted before the UI settles.
FIRST_CHECK_DELAY = 5.0


def _parse_version(s: str) -> tuple[int, ...]:
    """Parse a version string into a comparable numeric tuple.

    Deliberately dependency-free (``packaging`` isn't guaranteed at
    runtime).  Splits on ``.``, takes the leading integer of each
    component, and stops at the first non-numeric component (so pre-
    release/local suffixes like ``1.2.0rc1`` compare as ``(1, 2, 0)``).
    Unparseable input yields ``()`` which sorts lowest.
    """
    parts: list[int] = []
    for chunk in s.strip().split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """True if ``latest`` is a strictly newer release than ``current``."""
    lt = _parse_version(latest)
    ct = _parse_version(current)
    if not lt:
        return False
    return lt > ct


def _current_version() -> str:
    try:
        from vezir import __version__
        return __version__
    except Exception:
        return "0"


def fetch_latest_pypi_version() -> str | None:
    """Return the latest published vezir version on PyPI, or None.

    Best-effort: any network/DNS/parse error returns None silently so
    the poll never disrupts the UI.  Uses default TLS verification
    (public PyPI, certifi) -- NOT the internal-CA trust path.
    """
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx is a hard dep
        return None
    try:
        resp = httpx.get(PYPI_URL, timeout=5.0, verify=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        # DNS/connectivity/HTTP/JSON errors are all non-fatal here.
        log.debug("pypi version fetch failed: %s", exc)
        return None
    version = (data.get("info") or {}).get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def upgrade_command() -> str:
    """Return the upgrade command appropriate to how vezir was installed.

    * editable/source checkout -> ``git pull && pip install -e .``
    * pipx-managed venv         -> ``pipx upgrade vezir``
    * plain pip install         -> ``pip install --upgrade vezir``
    """
    # pipx installs live under a pipx venvs dir; the running interpreter
    # path is the most reliable signal.
    exe = (sys.executable or "").replace("\\", "/")
    if "/pipx/venvs/vezir/" in exe or os.environ.get("PIPX_HOME"):
        if "/pipx/venvs/vezir/" in exe:
            return "pipx upgrade vezir"

    # Editable install: the package imports from a source tree that is
    # not inside site-packages/dist-packages.
    try:
        import vezir
        pkg_path = os.path.dirname(os.path.abspath(vezir.__file__ or ""))
    except Exception:
        pkg_path = ""
    if pkg_path and "site-packages" not in pkg_path and "dist-packages" not in pkg_path:
        return "git pull && pip install -e ."

    return "pip install --upgrade vezir"


@dataclass
class UpdateState:
    """Per-app state for the update poll.

    ``notified_for`` records the version string we last toasted so we
    don't re-nag on every tick within a single launch.  Reset on
    restart by design (re-notifying after a restart is fine -- the user
    may have missed the first nudge).
    """

    notified_for: str | None = None

    @classmethod
    def new(cls) -> UpdateState:
        return cls(notified_for=None)


def _cache_gate_open(now: float) -> bool:
    """True if enough time has passed since the last successful check."""
    try:
        prefs = load_client_prefs()
        last = float(prefs.get("last_update_check", 0) or 0)
    except Exception:
        return True
    return (now - last) >= UPDATE_POLL_INTERVAL


def _record_check(now: float, latest: str | None) -> None:
    """Persist the check timestamp (+ latest seen) via load->mutate->save."""
    try:
        prefs = load_client_prefs()
        prefs["last_update_check"] = int(now)
        if latest:
            prefs["last_seen_latest"] = latest
        save_client_prefs(prefs)
    except Exception as exc:
        log.debug("failed to persist update-check state: %s", exc)


# ─── Textual integration ─────────────────────────────────────────────────────


def install_update_poll(
    screen,
    *,
    interval: float = UPDATE_POLL_INTERVAL,
    first_delay: float = FIRST_CHECK_DELAY,
) -> UpdateState:
    """Wire the update poll into a screen's lifecycle.

    Call from ``MainScreen.on_mount``.  Returns the ``UpdateState`` so
    the caller can attach it for testing.  Runs one prompt check shortly
    after mount, then a recurring check every ``interval`` seconds; both
    respect the cross-launch cache in ``client.json``.
    """
    state = UpdateState.new()

    def on_tick() -> None:
        _update_poll_worker(screen, state)

    screen.set_timer(first_delay, on_tick, name="update-check-first")
    screen.set_interval(interval, on_tick, name="update-check")
    return state


@work(thread=True, exclusive=True, group="update-check")
def _update_poll_worker(screen, state: UpdateState) -> None:
    """Worker body: (cache-gated) fetch PyPI, compare, notify once."""
    now = time.time()
    if not _cache_gate_open(now):
        return
    latest = fetch_latest_pypi_version()
    # Record the attempt time regardless so a transient failure still
    # backs off the next tick (avoids tight retry loops on PyPI errors).
    _record_check(now, latest)
    if not latest:
        return

    current = _current_version()
    if not is_newer(latest, current):
        return
    if state.notified_for == latest:
        return
    state.notified_for = latest

    # Stash the latest version on the app so other surfaces (e.g. the
    # Record screen version line) can annotate "update available".
    try:
        screen.app.latest_available_version = latest
    except Exception:
        pass

    cmd = upgrade_command()
    title = f"vezir {latest} available"
    body = f"You're on {current}. Update with:\n{cmd}"
    notify_desktop(title, body)
    try:
        screen.app.call_from_thread(
            screen.app.notify,
            f"vezir {latest} available — run: {cmd}",
            title=title,
            severity="information",
            timeout=15,
        )
    except Exception as exc:
        log.debug("in-app update notify failed: %s", exc)
