from __future__ import annotations

from pathlib import Path

from vezir.server import meet_runner


def test_env_for_meet_sets_hf_hub_offline(tmp_path, monkeypatch):
    """The millet subprocess env forces offline HF model use so diarization
    doesn't make a network HEAD request on every load."""
    monkeypatch.setattr(
        meet_runner.config, "team_speaker_profiles_path",
        lambda team_id: tmp_path / f"{team_id}.json",
    )
    env = meet_runner._env_for_meet(tmp_path / "HOME", "blink")
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["HOME"] == str(tmp_path / "HOME")
    assert "XDG_CONFIG_HOME" not in env


def _session_dir(tmp_path: Path) -> Path:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    return session_dir


def _patch_transcribe_config(
    monkeypatch,
    *,
    device: str = "cpu",
    compute_type: str = "int8",
    torch_device: str | None = None,
    asr_backend: str | None = None,
    mlx_model: str | None = None,
) -> None:
    monkeypatch.setattr(meet_runner.config, "meet_device", lambda: device)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_compute_type",
        lambda selected_device=None: compute_type,
    )
    monkeypatch.setattr(
        meet_runner.config,
        "meet_torch_device",
        lambda selected_device=None: torch_device,
    )
    monkeypatch.setattr(meet_runner.config, "meet_asr_backend", lambda: asr_backend)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_mlx_model",
        lambda selected_backend=None: (
            mlx_model if selected_backend == asr_backend else None
        ),
    )


def test_build_transcribe_args_passes_device_and_compute_type(monkeypatch, tmp_path):
    _patch_transcribe_config(monkeypatch, device="cpu", compute_type="int8")

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        str(tmp_path / "session"),
    ]


def test_transcribe_runs_built_args(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    _patch_transcribe_config(monkeypatch, device="cpu", compute_type="int8")

    def fake_run_meet(args, job_id, team_id, log_path=None):
        captured["args"] = args
        captured["job_id"] = job_id
        captured["team_id"] = team_id
        captured["log_path"] = log_path
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)

    session_dir = _session_dir(tmp_path)
    log_path = tmp_path / "worker.log"

    rc = meet_runner.transcribe(session_dir, "job-1", "blink", log_path)

    assert rc == 0
    assert captured["args"] == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        str(session_dir),
    ]
    assert captured["job_id"] == "job-1"
    assert captured["team_id"] == "blink"
    assert captured["log_path"] == log_path


def test_build_transcribe_args_uses_linux_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: True)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_supports_option",
        lambda option: False,
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cuda",
        "--compute-type",
        "float16",
        str(tmp_path / "session"),
    ]


def test_build_transcribe_args_uses_cpu_without_linux_cuda(monkeypatch, tmp_path):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: False)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_supports_option",
        lambda option: False,
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        str(tmp_path / "session"),
    ]


def test_build_transcribe_args_uses_apple_silicon_mps_defaults(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(meet_runner.config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(meet_runner.config, "_mps_available", lambda: True)
    monkeypatch.setattr(
        meet_runner.config,
        "_meet_supports_device",
        lambda device: device == "mps",
    )
    monkeypatch.setattr(
        meet_runner.config,
        "_ctranslate2_supports_device",
        lambda device: device == "mps",
    )
    monkeypatch.setattr(
        meet_runner.config,
        "meet_supports_option",
        lambda option: False,
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "mps",
        "--compute-type",
        "float32",
        str(tmp_path / "session"),
    ]


def test_build_transcribe_args_uses_split_apple_silicon_torch_device(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_TORCH_DEVICE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(meet_runner.config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(meet_runner.config, "_mps_available", lambda: True)
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: False)
    monkeypatch.setattr(meet_runner.config, "_meet_supports_device", lambda device: False)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_supports_option",
        lambda option: option == "--torch-device",
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        "--torch-device",
        "mps",
        str(tmp_path / "session"),
    ]


def test_build_transcribe_args_uses_mlx_asr_backend(monkeypatch, tmp_path):
    _patch_transcribe_config(
        monkeypatch,
        device="cpu",
        compute_type="int8",
        torch_device="mps",
        asr_backend="mlx",
        mlx_model="mlx-community/whisper-tiny",
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        "--asr-backend",
        "mlx",
        "--mlx-model",
        "mlx-community/whisper-tiny",
        "--torch-device",
        "mps",
        str(tmp_path / "session"),
    ]


# ── default-language passthrough ─────────────────────────────────────────────


def test_build_transcribe_args_adds_default_language(monkeypatch, tmp_path):
    _patch_transcribe_config(monkeypatch, device="cpu", compute_type="int8")
    monkeypatch.setattr(meet_runner.config, "meet_default_language", lambda: "en")
    monkeypatch.setattr(meet_runner.config, "meet_supports_option", lambda opt: True)
    monkeypatch.setattr(meet_runner.config, "team_sync_config_path",
                        lambda team_id: tmp_path / "nonexistent.json")
    args = meet_runner.build_transcribe_args(_session_dir(tmp_path), team_id="blink")
    assert "--default-language" in args
    assert args[args.index("--default-language") + 1] == "en"


def test_build_transcribe_args_per_team_overrides_global(monkeypatch, tmp_path):
    import json as _json
    _patch_transcribe_config(monkeypatch, device="cpu", compute_type="int8")
    monkeypatch.setattr(meet_runner.config, "meet_default_language", lambda: "en")
    monkeypatch.setattr(meet_runner.config, "meet_supports_option", lambda opt: True)
    cfg = tmp_path / "team_sync.json"
    cfg.write_text(_json.dumps({"default_language": "de"}))
    monkeypatch.setattr(meet_runner.config, "team_sync_config_path", lambda team_id: cfg)
    args = meet_runner.build_transcribe_args(_session_dir(tmp_path), team_id="t")
    assert args[args.index("--default-language") + 1] == "de"


def test_build_transcribe_args_no_default_language_when_unset(monkeypatch, tmp_path):
    _patch_transcribe_config(monkeypatch, device="cpu", compute_type="int8")
    monkeypatch.setattr(meet_runner.config, "meet_default_language", lambda: None)
    monkeypatch.setattr(meet_runner.config, "team_sync_config_path",
                        lambda team_id: tmp_path / "nope.json")
    args = meet_runner.build_transcribe_args(_session_dir(tmp_path), team_id="blink")
    assert "--default-language" not in args


# ── duplicate-folder guard: only force-retry when genuinely Skipped ──────────


def _write_log(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "job.log"
    p.write_text(text)
    return p


def test_sync_log_shows_skipped_true(tmp_path):
    log = _write_log(
        tmp_path,
        "--- millet sync /x\nSyncing: 01X\n  Skipped: not a scheduled meeting\n",
    )
    assert meet_runner._sync_log_shows_skipped(log) is True


def test_sync_log_shows_skipped_false_on_git_error(tmp_path):
    log = _write_log(
        tmp_path,
        "--- millet sync /x\nSyncing: 01X\n  Staged: a\n"
        "  Error: Command failed: git push\n",
    )
    assert meet_runner._sync_log_shows_skipped(log) is False


def test_sync_does_not_force_on_git_error(monkeypatch, tmp_path):
    """A schedule-matched sync that fails to push must NOT fall through to
    --force --meeting-type (which created duplicate folders)."""
    sd = _session_dir(tmp_path)
    log_path = tmp_path / "job.log"
    monkeypatch.setattr(meet_runner, "ensure_session_json", lambda *a, **k: None)
    calls = []

    def fake_run_meet(args, **kwargs):
        calls.append(args)
        # Simulate millet sync: matched a schedule, staged, then push failed.
        log_path.write_text(
            "--- millet sync /x\nSyncing: 01X\n  Staged: a\n"
            "  Error: Command failed: git push\n"
        )
        return 1

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)
    rc = meet_runner.sync(sd, "01X", "blink", log_path)
    assert rc != 0
    # Only ONE invocation (no --force retry).
    assert len(calls) == 1
    assert "--force" not in calls[0]


def test_sync_forces_when_skipped(monkeypatch, tmp_path):
    """When step-1 is genuinely Skipped (no schedule match), force-retry."""
    sd = _session_dir(tmp_path)
    log_path = tmp_path / "job.log"
    monkeypatch.setattr(meet_runner, "ensure_session_json", lambda *a, **k: None)
    monkeypatch.setattr(meet_runner, "_get_job_title", lambda jid: "Dev Sync")
    calls = []

    def fake_run_meet(args, **kwargs):
        calls.append(args)
        if "--force" not in args:
            log_path.write_text(
                "--- millet sync /x\nSyncing: 01X\n  Skipped: not a scheduled meeting\n"
            )
            return 0
        log_path.write_text("--- millet sync --force /x\n  Pushed 5 file(s).\n  Done: 5\n")
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)
    rc = meet_runner.sync(sd, "01X", "blink", log_path)
    assert rc == 0
    assert len(calls) == 2
    assert "--force" in calls[1]


# ── explicit meeting_type override (v0.7.16 "sync as") ───────────────────────


def test_sync_override_skips_detection_and_forces_folder(monkeypatch, tmp_path):
    """An explicit meeting_type forces --force --meeting-type <slug> and does
    NOT attempt schedule detection (no plain `sync` call first)."""
    sd = _session_dir(tmp_path)
    log_path = tmp_path / "job.log"
    monkeypatch.setattr(meet_runner, "ensure_session_json", lambda *a, **k: None)
    monkeypatch.setattr(meet_runner, "_get_job_title", lambda jid: None)
    calls = []

    def fake_run_meet(args, **kwargs):
        calls.append(args)
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)
    rc = meet_runner.sync(
        sd, "01X", "blink", log_path, meeting_type="Post Scrum"
    )
    assert rc == 0
    # Exactly one invocation: the forced override (no detection step).
    assert len(calls) == 1
    args = calls[0]
    assert "--force" in args
    i = args.index("--meeting-type")
    # Slugified for path safety.
    assert args[i + 1] == "post-scrum"


def test_sync_passes_title_to_ensure_session_json(monkeypatch, tmp_path):
    """sync() fetches the job title and forwards it to ensure_session_json."""
    sd = _session_dir(tmp_path)
    log_path = tmp_path / "job.log"
    captured = {}

    def fake_ensure(session_dir, session_id, title=None):
        captured["title"] = title
        return None

    monkeypatch.setattr(meet_runner, "ensure_session_json", fake_ensure)
    monkeypatch.setattr(meet_runner, "_get_job_title", lambda jid: "Weekly Sync")
    monkeypatch.setattr(meet_runner, "run_meet", lambda *a, **k: 0)
    meet_runner.sync(sd, "01X", "blink", log_path, meeting_type="x")
    assert captured["title"] == "Weekly Sync"


# ── title injection into session.json ────────────────────────────────────────


def test_ensure_session_json_injects_title(monkeypatch, tmp_path):
    import json
    sd = _session_dir(tmp_path)
    # Real ULID-ish id (26 chars) so the timestamp parse path runs.
    sid = "01KT6BHZCM0000000000000000"
    sj = meet_runner.ensure_session_json(sd, sid, title="Post Scrum")
    data = json.loads(sj.read_text())
    assert data["title"] == "Post Scrum"
    assert data["session_id"] == sid


def test_ensure_session_json_backfills_missing_title(tmp_path):
    import json
    sd = _session_dir(tmp_path)
    sid = "01KT6BHZCM0000000000000000"
    sj = sd / f"{sid}.session.json"
    sj.write_text(json.dumps({"started_at": "2026-06-03T08:01:00+00:00",
                              "source": "vezir", "session_id": sid}))
    meet_runner.ensure_session_json(sd, sid, title="Dev Sync")
    data = json.loads(sj.read_text())
    assert data["title"] == "Dev Sync"


def test_ensure_session_json_preserves_existing_title(tmp_path):
    import json
    sd = _session_dir(tmp_path)
    sid = "01KT6BHZCM0000000000000000"
    sj = sd / f"{sid}.session.json"
    sj.write_text(json.dumps({"started_at": "2026-06-03T08:01:00+00:00",
                              "title": "Original", "session_id": sid}))
    meet_runner.ensure_session_json(sd, sid, title="Should Not Override")
    data = json.loads(sj.read_text())
    assert data["title"] == "Original"
