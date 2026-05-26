"""Shared pytest configuration.

Rate-limiting is enforced in production but would make existing test
suites flaky (e.g. test loops that POST /login 50 times in a row). We
disable it globally and add focused tests that re-enable it to verify
the limiter itself in ``test_token_hardening.py``.

v0.6.0: ``auth.issue`` requires a ``team_id``.  To keep the dozens of
existing test call sites readable, we monkey-patch ``auth.issue`` at
import-time so that test code calling ``auth.issue("alice")`` gets a
default team auto-created and applied.  Tests that explicitly want a
specific team pass ``team_id="..."`` and the wrapper passes through.
Migration tests and the new team-isolation tests bypass this shim by
importing ``auth._issue_raw`` (the original) directly.
"""
from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    """Run before any test imports the app."""
    os.environ.setdefault("VEZIR_DISABLE_RATELIMIT", "1")
    # Prevent host production env vars from leaking into tests.
    # VEZIR_COOKIE_SECURE=1 causes Secure cookies that httpx won't send
    # over http://testserver; VEZIR_CADDY_ROOT_CERT_PATH causes enroll
    # payloads to upgrade to v2 with a real CA cert.
    for var in ("VEZIR_COOKIE_SECURE", "VEZIR_CADDY_ROOT_CERT_PATH"):
        os.environ.pop(var, None)


def _install_auth_issue_shim() -> None:
    """Monkey-patch ``vezir.server.auth.issue`` to auto-create + use a default team.

    Run lazily (the first time the auth module is imported by a test)
    so we don't pay the cost when running, say, a client-side test that
    never touches the server.
    """
    from vezir.server import auth as _auth
    from vezir.server import queue as _queue

    if hasattr(_auth, "_issue_raw"):
        return  # already installed

    _auth._issue_raw = _auth.issue  # save the real one for migration tests

    def _shimmed_issue(github, team_id=None, **kwargs):  # type: ignore[no-untyped-def]
        if team_id is None:
            # Use 'blink' (one of the migration-seeded teams) so we
            # don't drift the team roster.  Create it on demand if a
            # test fixture skipped the migration step.
            team_id = "blink"
            if _queue.get_team(team_id) is None:
                _queue.create_team(team_id, "Blink (test default)")
        return _auth._issue_raw(
            github, team_id=team_id, **kwargs,
        )

    _auth.issue = _shimmed_issue  # type: ignore[assignment]


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Install the auth.issue shim once tests have been collected.

    By this point ``vezir.server.auth`` has been imported by the test
    modules' imports, so the patch sticks for the rest of the run.
    """
    try:
        _install_auth_issue_shim()
    except Exception:  # pragma: no cover - defensive
        pass
