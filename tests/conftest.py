"""Shared pytest configuration.

Rate-limiting is enforced in production but would make existing test
suites flaky (e.g. test loops that POST /login 50 times in a row). We
disable it globally and add focused tests that re-enable it to verify
the limiter itself in ``test_token_hardening.py``.
"""
from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    """Run before any test imports the app."""
    os.environ.setdefault("VEZIR_DISABLE_RATELIMIT", "1")
