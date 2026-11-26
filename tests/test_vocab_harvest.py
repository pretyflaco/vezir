"""Tests for per-team vocabulary harvesting API, worker isolation, rejection memory, and CLI."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from vezir import config
from vezir.cli import main
from vezir.server import auth, meet_runner, queue, vocabulary, worker
from vezir.server.app import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(worker, "start_background_worker", lambda: None)
    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _headers(token: str, team_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Team-Id": team_id}


def test_harvest_trigger_returns_501_when_millet_lacks_vocab_harvest(
    client: TestClient, monkeypatch
):
    token = auth.issue("alice", team_id="team-a")
    monkeypatch.setattr(config, "meet_supports_vocab_harvest", lambda: False)

    resp = client.post("/api/vocabulary/harvest", headers=_headers(token, "team-a"))
    assert resp.status_code == 501
    assert "millet vocab harvest" in resp.text


def test_harvest_worker_isolates_team_and_excludes_personal_sessions(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path / "data"))
    queue.create_team("team-a", "Team A")
    queue.create_team("team-b", "Team B")
    team_a = queue.resolve_team_uuid("team-a")
    team_b = queue.resolve_team_uuid("team-b")
    assert team_a and team_b

    # Seed: Team A shared done, Team A personal done, Team A queued, Team B shared done.
    def _seed(sid: str, team: str, status: str, *, personal: bool = False) -> Path:
        queue.enqueue(sid, "alice", team_id=team, personal=personal)
        queue.update_status(sid, status)
        sd = config.sessions_dir() / sid
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{sid}.json").write_text('{"segments": []}', encoding="utf-8")
        return sd

    sd_a_shared = _seed("01JHARV000000000000000A1", team_a, "done", personal=False)
    _seed("01JHARV000000000000000A2", team_a, "done", personal=True)
    _seed("01JHARV000000000000000A3", team_a, "queued", personal=False)
    _seed("01JHARV000000000000000B1", team_b, "done", personal=False)

    seen_dirs: list[Path] = []

    def fake_harvest(team_id, session_dirs, *, output_path, **_kw):
        assert team_id == team_a
        seen_dirs.extend(session_dirs)
        report = {
            "schema_version": 1,
            "generated_at": "2026-11-26T12:00:00Z",
            "backend": "tinfoil/test",
            "sessions_scanned": len(session_dirs),
            "candidates": [
                {
                    "canonical": "Vezir",
                    "observed": "vzr",
                    "suggested_layer": "alias",
                    "total_occurrences": 4,
                    "distinct_sessions": 2,
                    "holdout_occurrences": 1,
                    "examples": [],
                }
            ],
        }
        config.secure_write_text(output_path, json.dumps(report))
        return 0

    monkeypatch.setattr(meet_runner, "harvest_vocabulary", fake_harvest)

    worker.harvest_vocabulary_for_team(team_a, max_sessions=50)

    # Strictly Team A's non-personal done session only!
    assert seen_dirs == [sd_a_shared]

    hpath = config.team_vocabulary_harvest_path(team_a)
    assert hpath.is_file()
    assert stat.S_IMODE(hpath.stat().st_mode) == 0o600

    state_a = vocabulary.load_team_harvest_state(team_a)
    assert state_a["status"] == "ready"
    assert len(state_a["report"]["candidates"]) == 1

    # Team B sees nothing from Team A.
    state_b = vocabulary.load_team_harvest_state(team_b)
    assert state_b["status"] == "idle"
    assert state_b["report"] is None


def test_harvest_decide_atomic_approvals_rejections_and_chain_rollback(
    client: TestClient,
):
    token = auth.issue("alice", team_id="team-a")
    team_uuid = queue.resolve_team_uuid("team-a")
    assert team_uuid is not None
    hdrs = _headers(token, "team-a")

    # Seed a ready harvest report with 4 candidates.
    vocabulary.save_team_harvest_state(
        team_uuid,
        {
            "status": "ready",
            "error": None,
            "updated_at": "2026-11-26T12:00:00Z",
            "report": {
                "schema_version": 1,
                "sessions_scanned": 5,
                "candidates": [
                    {"canonical": "Vezir", "observed": "vzr", "suggested_layer": "alias"},
                    {
                        "canonical": "WhisperX",
                        "observed": "whisper",
                        "suggested_layer": "term_and_glossary",
                    },
                    {
                        "canonical": "Amber",
                        "observed": "amber",
                        "suggested_layer": "term_and_glossary",
                    },
                    {"canonical": "Vezir", "observed": "easier", "suggested_layer": "alias"},
                ],
            },
        },
    )

    r_get = client.get("/api/vocabulary/harvest", headers=hdrs)
    assert r_get.status_code == 200
    assert len(r_get.json()["pending_candidates"]) == 4

    # Batch approve: alias + term + glossary, plus 1 rejection.
    r_dec = client.post(
        "/api/vocabulary/harvest/decide",
        headers=hdrs,
        json={
            "approve": [
                {"canonical": "Vezir", "observed": "vzr", "layer": "alias"},
                {"canonical": "WhisperX", "observed": "whisper", "layer": "term"},
                {
                    "canonical": "Amber",
                    "observed": "amber",
                    "layer": "glossary",
                    "description": "Android NIP-55 Nostr signer",
                },
            ],
            "reject": [{"canonical": "Vezir", "observed": "easier"}],
        },
    )
    assert r_dec.status_code == 200, r_dec.text
    body = r_dec.json()
    assert body["approved_count"] == 3
    assert body["rejected_count"] == 1
    assert body["pending_count"] == 0
    assert body["vocabulary"]["aliases"] == {"Vezir": ["vzr"]}
    assert set(body["vocabulary"]["terms"]) == {"Vezir", "WhisperX", "Amber"}
    assert body["vocabulary"]["glossary"] == {"Amber": "Android NIP-55 Nostr signer"}

    # Permissions on both files must be 0600.
    assert stat.S_IMODE(config.team_vocabulary_path(team_uuid).stat().st_mode) == 0o600
    assert stat.S_IMODE(config.team_vocabulary_rejected_path(team_uuid).stat().st_mode) == 0o600

    # Pending candidates must now be empty.
    r_after = client.get("/api/vocabulary/harvest", headers=hdrs)
    assert r_after.json()["pending_candidates"] == []

    # Invalid batch (alias chain `vzr` -> `Vezir` + `Vezir` -> `NewName`) must
    # fail with 400 and NOT write the rejection in the same request!
    r_bad = client.post(
        "/api/vocabulary/harvest/decide",
        headers=hdrs,
        json={
            "approve": [{"canonical": "NewName", "observed": "Vezir", "layer": "alias"}],
            "reject": [{"canonical": "Other", "observed": "should_not_persist"}],
        },
    )
    assert r_bad.status_code == 400
    rej_list = client.get("/api/vocabulary/rejected", headers=hdrs).json()["rejected"]
    assert len(rej_list) == 1
    assert rej_list[0]["observed"] == "easier"

    # Un-reject single pair via DELETE /api/vocabulary/rejected
    r_del = client.delete(
        "/api/vocabulary/rejected",
        headers=hdrs,
        params={"canonical": "Vezir", "observed": "easier"},
    )
    assert r_del.status_code == 200
    assert r_del.json()["removed"] == 1
    assert r_del.json()["rejected"] == []


def test_meet_runner_harvest_vocabulary_builds_private_files_and_cleans_up(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(config, "meet_supports_vocab_harvest", lambda: True)

    seen_args: list[list[str]] = []
    cleaned: list[str] = []

    def fake_run_meet(args, *, job_id, team_id, log_path=None):
        seen_args.append(list(args))
        out_path = Path(args[args.index("--output") + 1])
        sessions_path = Path(args[args.index("--sessions-file") + 1])
        assert json.loads(sessions_path.read_text(encoding="utf-8")) == [str(tmp_path / "s1")]
        out_path.write_text('{"schema_version": 1, "candidates": []}', encoding="utf-8")
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)
    monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda jid: cleaned.append(jid))

    dest = tmp_path / "final_report.json"
    rc = meet_runner.harvest_vocabulary(
        "team-a",
        [tmp_path / "s1"],
        output_path=dest,
        anchors=["Vezir"],
        min_occurrences=2,
        min_sessions=1,
    )
    assert rc == 0
    assert dest.is_file()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert cleaned == ["vocab-harvest-team-a"]
    assert "--anchors" in seen_args[0]


def test_cli_vocab_harvest_review_and_rejected(tmp_path: Path, monkeypatch):
    from vezir.client.api import ApiResult, VezirClient

    monkeypatch.setenv("VEZIR_URL", "https://vezir.example.com")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_" + "b" * 32)
    monkeypatch.setenv("VEZIR_TEAM_ID", "team-a")

    decisions: list[tuple[list, list]] = []

    monkeypatch.setattr(
        VezirClient,
        "trigger_vocab_harvest",
        lambda self, **_kw: ApiResult.success({"queued": True, "status": "queued"}),
    )
    monkeypatch.setattr(
        VezirClient,
        "get_vocab_harvest",
        lambda self: ApiResult.success(
            {
                "status": "ready",
                "error": None,
                "pending_candidates": [
                    {
                        "canonical": "Vezir",
                        "observed": "vzr",
                        "suggested_layer": "alias",
                        "total_occurrences": 5,
                        "distinct_sessions": 3,
                        "holdout_occurrences": 1,
                        "examples": [
                            {
                                "session_id": "01S1",
                                "start": 10.0,
                                "before": "Check vzr.",
                                "after": "Check Vezir.",
                                "holdout": True,
                            }
                        ],
                    },
                    {
                        "canonical": "Vezir",
                        "observed": "easier",
                        "suggested_layer": "alias",
                        "total_occurrences": 3,
                        "distinct_sessions": 2,
                        "holdout_occurrences": 0,
                        "examples": [],
                    },
                ],
            }
        ),
    )

    def fake_decide(self, *, approve=None, reject=None):
        decisions.append((list(approve or []), list(reject or [])))
        return ApiResult.success(
            {
                "approved_count": len(approve or []),
                "rejected_count": len(reject or []),
                "pending_count": 0,
            }
        )

    monkeypatch.setattr(VezirClient, "decide_vocab_harvest", fake_decide)
    monkeypatch.setattr(
        VezirClient,
        "get_rejected_vocabulary",
        lambda self: ApiResult.success(
            {"rejected": [{"canonical": "Vezir", "observed": "easier", "rejected_by": "alice"}]}
        ),
    )
    monkeypatch.setattr(
        VezirClient,
        "delete_rejected_vocabulary",
        lambda self, **_kw: ApiResult.success({"removed": 1, "rejected": []}),
    )

    runner = CliRunner()
    r_harv = runner.invoke(main, ["vocab", "harvest", "--wait"])
    assert r_harv.exit_code == 0, r_harv.output
    assert "Harvest ready: 2 pending candidate(s)." in r_harv.output

    # Interactive review: approve first as alias ('a'), reject second ('r').
    r_rev = runner.invoke(main, ["vocab", "review"], input="a\nr\n")
    assert r_rev.exit_code == 0, r_rev.output
    assert decisions == [
        (
            [{"canonical": "Vezir", "observed": "vzr", "layer": "alias"}],
            [{"canonical": "Vezir", "observed": "easier"}],
        )
    ]

    # Offline --from-file review
    local_rep = tmp_path / "report.json"
    local_rep.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "canonical": "pyannote",
                        "observed": "pie a note",
                        "suggested_layer": "alias",
                        "total_occurrences": 4,
                        "distinct_sessions": 2,
                        "holdout_occurrences": 0,
                        "examples": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    r_file = runner.invoke(
        main, ["vocab", "review", "--from-file", str(local_rep)], input="t\n"
    )
    assert r_file.exit_code == 0, r_file.output
    assert decisions[-1] == (
        [{"canonical": "pyannote", "observed": "pie a note", "layer": "term"}],
        [],
    )

    r_rej = runner.invoke(main, ["vocab", "rejected"])
    assert r_rej.exit_code == 0 and "easier -> Vezir" in r_rej.output

    r_rm = runner.invoke(main, ["vocab", "rejected", "--remove", "Vezir", "easier"])
    assert r_rm.exit_code == 0 and "Removed 1 rejected pair(s)." in r_rm.output


def test_harvest_worker_records_error_on_total_llm_outage(tmp_path: Path, monkeypatch):
    """Total LLM failure (non-zero rc or zero-success report) must set status='error'."""
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path / "data"))
    queue.create_team("team-a", "Team A")
    team_a = queue.resolve_team_uuid("team-a")
    assert team_a

    sid = "01JHARV000000000000000E1"
    queue.enqueue(sid, "alice", team_id=team_a)
    queue.update_status(sid, "done")
    sd = config.sessions_dir() / sid
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"{sid}.json").write_text('{"segments": []}', encoding="utf-8")

    # Case 1: millet fails loudly with non-zero exit code on TEE outage.
    def fake_fail_rc(_team_id, _dirs, *, output_path, log_path=None, **_kw):
        if log_path is not None:
            log_path.write_text("Error: vocabulary harvest failed: simulated TEE outage\n")
        return 1

    monkeypatch.setattr(meet_runner, "harvest_vocabulary", fake_fail_rc)
    worker.harvest_vocabulary_for_team(team_a)
    st1 = vocabulary.load_team_harvest_state(team_a)
    assert st1["status"] == "error"
    assert st1["error"] and "simulated TEE outage" in st1["error"]

    # Case 2: belt-and-suspenders guard if a report returns 0 successes + unknown backend.
    def fake_zero_success(_team_id, _dirs, *, output_path, **_kw):
        config.secure_write_text(
            output_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "backend": "unknown",
                    "seed_sessions": 2,
                    "llm_errors": 2,
                    "candidates": [],
                }
            ),
        )
        return 0

    monkeypatch.setattr(meet_runner, "harvest_vocabulary", fake_zero_success)
    worker.harvest_vocabulary_for_team(team_a)
    st2 = vocabulary.load_team_harvest_state(team_a)
    assert st2["status"] == "error"
    assert st2["error"] and "LLM error" in st2["error"]


def test_second_harvest_filters_satisfied_term_and_glossary_but_keeps_new_alias(
    client: TestClient,
):
    token = auth.issue("alice", team_id="team-a")
    team_uuid = queue.resolve_team_uuid("team-a")
    assert team_uuid is not None
    hdrs = _headers(token, "team-a")

    # Existing vocabulary already has WhisperX (terms + glossary), Caddy (terms only),
    # and Vezir (terms + one alias).
    vocabulary.save_team_vocabulary(
        team_uuid,
        {
            "terms": ["WhisperX", "Caddy", "Vezir"],
            "aliases": {"Vezir": ["vzr"]},
            "glossary": {"WhisperX": "ASR alignment engine"},
        },
    )

    # Simulate a brand-new second harvest report containing all four shapes.
    vocabulary.save_team_harvest_state(
        team_uuid,
        {
            "status": "ready",
            "error": None,
            "updated_at": "2026-11-26T16:00:00Z",
            "report": {
                "schema_version": 1,
                "sessions_scanned": 4,
                "candidates": [
                    {
                        "canonical": "WhisperX",
                        "observed": "whisper",
                        "suggested_layer": "term_and_glossary",
                    },
                    {
                        "canonical": "Caddy",
                        "observed": "caddy",
                        "suggested_layer": "term_and_glossary",
                    },
                    {
                        "canonical": "Vezir",
                        "observed": "vizier",
                        "suggested_layer": "term_only",
                    },
                    {
                        "canonical": "Vezir",
                        "observed": "the zeer",
                        "suggested_layer": "alias",
                    },
                ],
            },
        },
    )

    resp = client.get("/api/vocabulary/harvest", headers=hdrs)
    assert resp.status_code == 200
    pending = resp.json()["pending_candidates"]
    obs_set = {c["observed"] for c in pending}
    assert "whisper" not in obs_set  # term_and_glossary satisfied
    assert "vizier" not in obs_set  # term_only satisfied
    assert "caddy" in obs_set  # glossary entry still missing
    assert "the zeer" in obs_set  # genuinely new alias for existing canonical term


def test_harvest_decide_rejects_same_batch_conflict_and_clears_stale_rejection(
    client: TestClient,
):
    token = auth.issue("alice", team_id="team-a")
    team_uuid = queue.resolve_team_uuid("team-a")
    assert team_uuid is not None
    hdrs = _headers(token, "team-a")

    # Case 1: same-batch approve + reject of ("Vezir", "vzr") (case-insensitive)
    # must fail with 400 and leave both files untouched.
    r_conflict = client.post(
        "/api/vocabulary/harvest/decide",
        headers=hdrs,
        json={
            "approve": [{"canonical": "Vezir", "observed": "vzr", "layer": "alias"}],
            "reject": [{"canonical": "vezir", "observed": "VZR"}],
        },
    )
    assert r_conflict.status_code == 400
    assert "cannot both approve and reject" in r_conflict.text
    assert vocabulary.load_team_vocabulary(team_uuid) == {
        "terms": [],
        "aliases": {},
        "glossary": {},
    }
    assert vocabulary.load_team_rejected(team_uuid) == []

    # Case 2: reject ("Vezir", "vzr") first, then explicitly approve it later ->
    # approval succeeds and clears the contradictory rejection entry.
    r_rej = client.post(
        "/api/vocabulary/harvest/decide",
        headers=hdrs,
        json={"approve": [], "reject": [{"canonical": "Vezir", "observed": "vzr"}]},
    )
    assert r_rej.status_code == 200
    assert len(vocabulary.load_team_rejected(team_uuid)) == 1

    r_app = client.post(
        "/api/vocabulary/harvest/decide",
        headers=hdrs,
        json={
            "approve": [{"canonical": "Vezir", "observed": "vzr", "layer": "alias"}],
            "reject": [],
        },
    )
    assert r_app.status_code == 200
    assert vocabulary.load_team_vocabulary(team_uuid)["aliases"] == {"Vezir": ["vzr"]}
    assert vocabulary.load_team_rejected(team_uuid) == []
