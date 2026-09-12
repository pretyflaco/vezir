"""Summary provenance + attestation (v0.20.0).

``jobs.summary_provenance`` records "<backend>/<model>" for *every* summary,
not just fallbacks, so a client can state whether a summary was produced
inside a hardware-attested TEE.  ``summary_fallback`` cannot answer that:
it is only populated when a fallback fired, so on the normal path vezir had
no idea which backend ran.

Rendering rule: only the *exception* is badged.  Every summary produced
since millet 0.19.0 is TEE-attested, so a positive badge on every row would
stop being read; an unattested summary is what deserves attention.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from vezir.client.api import Session
from vezir.server import migrations, queue, worker


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def team(tmp_data):
    """enqueue() has required a team_id since v0.6.0."""
    queue.create_team("blink", "Blink (test)")
    return queue.get_team("blink")["id"]


def _write_meta(sd: Path, sid: str, meta: dict, *, artifact: str = "summary") -> None:
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"{sid}.{artifact}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


# ── _summary_provenance: unconditional, unlike the fallback reader ──


def test_provenance_recorded_without_any_fallback(tmp_path):
    """The whole point: a normal, successful TEE run must be recorded.
    _summary_fallback_provenance returns None here."""
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {
        "backend": "tinfoil", "model": "glm-5-3-flash (TEE)", "fallback_used": False,
    })
    assert worker._summary_provenance(sd) == "tinfoil/glm-5-3-flash (TEE)"
    assert worker._summary_fallback_provenance(sd) is None


def test_provenance_records_non_tee_backend(tmp_path):
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {"backend": "claudemax", "model": "claude-sonnet-4-6"})
    assert worker._summary_provenance(sd) == "claudemax/claude-sonnet-4-6"


def test_provenance_reads_templated_sidecar(tmp_path):
    """A templated run writes <base>.<template>.meta.json and no
    .summary.meta.json at all -- video/iteration-plan sessions would
    otherwise have no provenance."""
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {
        "backend": "tinfoil", "model": "glm-5-3-flash", "template": "iteration-plan",
    }, artifact="iteration-plan")
    assert worker._summary_provenance(sd) == "tinfoil/glm-5-3-flash"


def test_provenance_ignores_upload_staging_sidecar(tmp_path):
    """<id>.meta.json is the upload staging file (summary_preset etc.),
    not summary provenance -- it must not be mistaken for one."""
    sd = tmp_path / "01SID"
    sd.mkdir(parents=True)
    (sd / "01SID.meta.json").write_text(
        json.dumps({"summary_preset": "confidential"}), encoding="utf-8"
    )
    assert worker._summary_provenance(sd) is None


def test_provenance_none_when_no_sidecar(tmp_path):
    assert worker._summary_provenance(tmp_path / "nope") is None


def test_provenance_none_on_malformed_json(tmp_path):
    sd = tmp_path / "01SID"
    sd.mkdir(parents=True)
    (sd / "01SID.summary.meta.json").write_text("{not json", encoding="utf-8")
    assert worker._summary_provenance(sd) is None


def test_provenance_none_when_backend_absent(tmp_path):
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {"model": "something"})
    assert worker._summary_provenance(sd) is None


def test_provenance_missing_model_falls_back_to_unknown(tmp_path):
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {"backend": "tinfoil"})
    assert worker._summary_provenance(sd) == "tinfoil/unknown"


# ── queue column round-trip ──


def test_update_status_summary_provenance_roundtrip(tmp_data, team):
    queue.enqueue("01JOB", github="alice", team_id=team)
    queue.update_status("01JOB", "done", summary_provenance="tinfoil/glm-5-3-flash")
    assert queue.get("01JOB")["summary_provenance"] == "tinfoil/glm-5-3-flash"
    # Sentinel: omitting it must not clear the stored value.
    queue.update_status("01JOB", "done")
    assert queue.get("01JOB")["summary_provenance"] == "tinfoil/glm-5-3-flash"


# ── migration + backfill ──


def test_migrate_0_20_0_idempotent(tmp_data):
    first = migrations.migrate_0_20_0()
    assert first["version"] == "0.20.0-summary-provenance"
    assert migrations.migrate_0_20_0() == {"already_applied": True}


def test_migration_backfills_from_sidecars(tmp_data, team):
    """The 500+ historical sessions must get accurate provenance, or the
    'unattested' signal never fires for the rows where it matters."""
    sessions_dir = tmp_data / "sessions"
    for sid, backend, model in (
        ("01OLD", "claudemax", "claude-sonnet-4-6"),
        ("01NEW", "tinfoil", "glm-5-3-flash (TEE)"),
    ):
        queue.enqueue(sid, github="alice", team_id=team)
        _write_meta(sessions_dir / sid, sid, {"backend": backend, "model": model})
    # A row with no sidecar at all must survive untouched.
    queue.enqueue("01BARE", github="alice", team_id=team)

    result = migrations.migrate_0_20_0()

    assert result["backfilled"] == 2
    assert queue.get("01OLD")["summary_provenance"] == "claudemax/claude-sonnet-4-6"
    assert queue.get("01NEW")["summary_provenance"] == "tinfoil/glm-5-3-flash (TEE)"
    assert queue.get("01BARE")["summary_provenance"] is None


def test_backfill_never_overwrites_existing_value(tmp_data, team):
    queue.enqueue("01JOB", github="alice", team_id=team)
    queue.update_status("01JOB", "done", summary_provenance="tinfoil/already-set")
    _write_meta(
        tmp_data / "sessions" / "01JOB", "01JOB",
        {"backend": "claudemax", "model": "would-clobber"},
    )
    migrations.migrate_0_20_0()
    assert queue.get("01JOB")["summary_provenance"] == "tinfoil/already-set"


def test_backfill_survives_a_corrupt_sidecar(tmp_data, team):
    """A migration that can be stopped by one bad file would block startup."""
    queue.enqueue("01BAD", github="alice", team_id=team)
    bad = tmp_data / "sessions" / "01BAD"
    bad.mkdir(parents=True)
    (bad / "01BAD.summary.meta.json").write_text("{{{", encoding="utf-8")
    queue.enqueue("01GOOD", github="alice", team_id=team)
    _write_meta(
        tmp_data / "sessions" / "01GOOD", "01GOOD",
        {"backend": "tinfoil", "model": "glm-5-3-flash"},
    )

    result = migrations.migrate_0_20_0()

    assert result["backfilled"] == 1
    assert queue.get("01BAD")["summary_provenance"] is None
    assert queue.get("01GOOD")["summary_provenance"] == "tinfoil/glm-5-3-flash"


def test_migration_registered_last(tmp_data):
    assert migrations.ALL_MIGRATIONS[-1] is migrations.migrate_0_20_0


# ── client-side attestation semantics ──


@pytest.mark.parametrize("prov", ["tinfoil/glm-5-3-flash", "tinfoil-tee/x"])
def test_session_is_attested_for_tee_backends(prov):
    s = Session(id="x", status="done", summary_provenance=prov)
    assert s.is_attested is True
    assert s.is_unattested is False


@pytest.mark.parametrize("prov", [
    "claudemax/claude-sonnet-4-6", "ollama/qwen3.8:27b", "openrouter/kimi",
])
def test_session_is_unattested_for_other_backends(prov):
    s = Session(id="x", status="done", summary_provenance=prov)
    assert s.is_attested is False
    assert s.is_unattested is True


def test_unknown_provenance_is_neither_attested_nor_flagged():
    """Absence of evidence isn't evidence of absence: badging every
    pre-0.20.0 session would make the badge meaningless."""
    s = Session(id="x", status="done", summary_provenance=None)
    assert s.is_attested is False
    assert s.is_unattested is False


def test_session_from_dict_carries_provenance():
    s = Session.from_dict({
        "id": "x", "status": "done", "summary_provenance": "tinfoil/glm-5-3-flash",
    })
    assert s.summary_provenance == "tinfoil/glm-5-3-flash"
    assert s.is_attested is True


# ── list badge: only the exception is shown ──


def _status_cell(**kw) -> str:
    from vezir.client.tui.sessions_screen import _status_cell

    return _status_cell(Session(id="x", status="done", **kw))


def test_attested_session_gets_no_badge():
    assert "unattested" not in _status_cell(summary_provenance="tinfoil/glm-5-3-flash")


def test_unattested_session_is_badged():
    assert "unattested" in _status_cell(summary_provenance="claudemax/sonnet")


def test_unknown_provenance_gets_no_badge():
    assert "unattested" not in _status_cell(summary_provenance=None)


# ── schema presence ──


def test_column_present_in_fresh_schema(tmp_data, team):
    queue.enqueue("01JOB", github="alice", team_id=team)
    row = queue.get("01JOB")
    assert "summary_provenance" in row
