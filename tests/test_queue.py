"""Basic unit tests for the sqlite queue."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def test_enqueue_and_claim(tmp_data):
    from vezir.server import queue
    queue.enqueue("01HZ000000000000000000ABCD", github="alice", title="t1")
    queue.enqueue("01HZ000000000000000000ABCE", github="bob", title="t2")

    job = queue.claim_next()
    assert job is not None
    assert job["id"] == "01HZ000000000000000000ABCD"
    assert job["status"] == "transcribing"

    # Second claim returns the next queued job (since the first is now transcribing).
    job2 = queue.claim_next()
    assert job2 is not None
    assert job2["id"] == "01HZ000000000000000000ABCE"


def test_status_transitions(tmp_data):
    from vezir.server import queue
    queue.enqueue("01HZ0000000000000000000XYZ", github="alice")
    queue.update_status("01HZ0000000000000000000XYZ", "needs_labeling",
                        artifacts={"pdf": "x.pdf"})
    row = queue.get("01HZ0000000000000000000XYZ")
    assert row["status"] == "needs_labeling"
    assert "pdf" in row["artifacts"]


def test_invalid_status(tmp_data):
    from vezir.server import queue
    queue.enqueue("01HZ000000000000000000NOPE", github="alice")
    with pytest.raises(ValueError):
        queue.update_status("01HZ000000000000000000NOPE", "bogus")
