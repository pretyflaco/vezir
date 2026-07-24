"""Shared pytest configuration.

Rate-limiting is enforced in production but would make existing test
suites flaky (e.g. test loops that POST /api/sessions 50 times in a
row).  We disable it globally and add focused tests that re-enable it
to verify the limiter itself in ``test_token_hardening.py``.

v0.7.0: tokens no longer carry team scope.  Team context is supplied
per-request via the ``X-Team-Id`` header and validated against the
``memberships`` table.  To keep the dozens of existing test call
sites readable, we wrap ``auth.issue`` so that callers passing the
legacy ``team_id=...`` keyword are silently accepted -- the team_id
becomes a membership row instead of being baked into the token.
"""
from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    """Run before any test imports the app."""
    os.environ.setdefault("VEZIR_DISABLE_RATELIMIT", "1")
    # Prevent host production env vars from leaking into tests.
    # The Google vars must be stripped too: an operator's host that has Google
    # sign-in configured would otherwise make the "unconfigured" auth tests
    # (test_google_auth.py) fail spuriously.
    for var in (
        "VEZIR_COOKIE_SECURE",
        "VEZIR_CADDY_ROOT_CERT_PATH",
        "VEZIR_PUBLIC_URL",
        "VEZIR_GOOGLE_CLIENT_ID",
        "VEZIR_GOOGLE_CLIENT_SECRET_FILE",
        "VEZIR_GOOGLE_CLIENT_SECRET",
        "VEZIR_GOOGLE_ALLOWED_DOMAIN",
    ):
        os.environ.pop(var, None)


def _install_auth_issue_shim() -> None:
    """Wrap ``auth.issue`` to translate legacy ``team_id=`` into a membership.

    Pre-v0.7.0 tests routinely call ``auth.issue("alice")`` or
    ``auth.issue("alice", team_id="blink")``.  In v0.7.0 the second
    form is gone, but rewriting every test site is mechanical noise.
    This shim:

      * accepts the legacy ``team_id=`` keyword,
      * auto-creates the team if missing (defaults to ``'blink'``),
      * adds a membership row for the issuing handle so subsequent
        ``X-Team-Id`` requests validate.

    Tests that need to bypass the shim can import ``auth._issue_raw``.
    """
    from vezir.server import auth as _auth
    from vezir.server import queue as _queue

    if hasattr(_auth, "_issue_raw"):
        return  # already installed

    _auth._issue_raw = _auth.issue  # save the real one

    def _shimmed_issue(github, team_id=None, **kwargs):  # type: ignore[no-untyped-def]
        # v0.7.0 issue() signature dropped team_id; capture it here
        # for the membership shim and DROP it before calling through.
        team = team_id or "blink"
        if _queue.get_team(team) is None:
            _queue.create_team(team, f"{team.capitalize()} (test default)")
        role = "admin" if kwargs.get("is_admin") else "scribe"
        _queue.add_membership(github, team, role=role, added_by="test-shim")
        return _auth._issue_raw(github, **kwargs)

    _auth.issue = _shimmed_issue  # type: ignore[assignment]


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Install the auth.issue shim once tests have been collected."""
    try:
        _install_auth_issue_shim()
    except Exception:  # pragma: no cover - defensive
        pass


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_nip98_replay_store():
    """Clear the in-memory NIP-98 consumed-id store between tests so the
    0.8.2 replay guard can't leak state across unrelated test cases."""
    try:
        from vezir.server import nip98
        nip98._consumed_ids.clear()
    except Exception:  # pragma: no cover - module may be unimportable in some envs
        pass
    yield


@pytest.fixture(autouse=True)
def _reset_v0_11_process_state():
    """Reset v0.11.0 in-process caches between tests.

    * the revoked-sid cache (sessions_auth) — otherwise the loaded-flag
      carries sids from a previous test's tmp DB;
    * the worker follow-up task queue + dedupe set — otherwise an
      enqueued-but-never-drained task from one test suppresses (dedupes)
      an identical enqueue in the next;
    * the schema-bring-up marker is left alone (keyed per DB path, so
      fresh tmp DBs re-run their DDL naturally).
    """
    try:
        from vezir.server import sessions_auth
        sessions_auth._reset_revoked_cache_for_tests()
        sessions_auth._reset_grace_cache_for_tests()
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        from vezir.server import uploads
        uploads._reset_idempotency_for_tests()
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        from vezir.server import worker
        with worker._TASKS_LOCK:
            worker._ACTIVE_TASKS.clear()
        while not worker._TASKS.empty():
            try:
                worker._TASKS.get_nowait()
            except Exception:
                break
    except Exception:  # pragma: no cover - defensive
        pass
    yield
