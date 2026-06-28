"""Worker multi-audio merge (v0.9.0).

A meeting split across several uploaded files lands as ordered
``<id>.part-NNN<ext>`` files.  Before transcribe, the worker concatenates
them (filename order) into the single canonical ``<id><ext>`` file millet
expects.  ffmpeg is mocked here so CI never decodes real audio.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _make_parts(session_dir: Path, sid: str, contents: list[bytes]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(contents):
        (session_dir / f"{sid}.part-{i:03d}.ogg").write_bytes(c)


def _fake_ffmpeg(parts_order_box):
    """Return a fake subprocess.run that emulates ffmpeg concat -c copy.

    It reads the concat list file (the ``-i`` argument), concatenates the
    referenced part files in order into the output path (the last argv
    token), and records the order it saw.
    """
    def _run(cmd, stdout=None, stderr=None):
        # cmd: [ffmpeg, -y, -f, concat, -safe, 0, -i, <list>, ..., <out>]
        list_path = Path(cmd[cmd.index("-i") + 1])
        out_path = Path(cmd[-1])
        ordered: list[Path] = []
        for line in list_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("file "):
                p = line[len("file "):].strip().strip("'")
                ordered.append(Path(p))
        parts_order_box.append([p.name for p in ordered])
        out_path.write_bytes(b"".join(p.read_bytes() for p in ordered))

        class _R:
            returncode = 0

        return _R()

    return _run


def test_merge_concatenates_in_filename_order(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZMULTI0000000000000000"
    sdir = tmp_data / "sessions" / sid
    _make_parts(sdir, sid, [b"OggSAAA", b"OggSBBB", b"OggSCCC"])
    log_path = tmp_data / "logs" / f"{sid}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    seen: list = []
    monkeypatch.setattr(subprocess, "run", _fake_ffmpeg(seen))

    worker._merge_multi_audio(sdir, sid, log_path)

    out = sdir / f"{sid}.ogg"
    assert out.exists()
    assert out.read_bytes() == b"OggSAAAOggSBBBOggSCCC"
    # ffmpeg saw the parts in zero-padded filename order.
    assert seen == [[
        f"{sid}.part-000.ogg",
        f"{sid}.part-001.ogg",
        f"{sid}.part-002.ogg",
    ]]
    # Parts and concat list are cleaned up.
    assert list(sdir.glob(f"{sid}.part-*")) == []
    assert list(sdir.glob("*.concat.txt")) == []


def test_merge_single_part_is_renamed_no_ffmpeg(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZSINGLE000000000000000"
    sdir = tmp_data / "sessions" / sid
    _make_parts(sdir, sid, [b"OggSONLY"])
    log_path = tmp_data / "logs" / f"{sid}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _boom(*a, **k):
        raise AssertionError("ffmpeg should not run for a single part")

    monkeypatch.setattr(subprocess, "run", _boom)
    worker._merge_multi_audio(sdir, sid, log_path)

    out = sdir / f"{sid}.ogg"
    assert out.exists()
    assert out.read_bytes() == b"OggSONLY"
    assert list(sdir.glob(f"{sid}.part-*")) == []


def test_merge_no_parts_is_noop(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZNOPART000000000000000"
    sdir = tmp_data / "sessions" / sid
    sdir.mkdir(parents=True)
    # Already-merged canonical file present, no parts.
    (sdir / f"{sid}.ogg").write_bytes(b"OggSDONE")
    log_path = tmp_data / "logs" / f"{sid}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _boom(*a, **k):
        raise AssertionError("ffmpeg should not run when there are no parts")

    monkeypatch.setattr(subprocess, "run", _boom)
    worker._merge_multi_audio(sdir, sid, log_path)

    assert (sdir / f"{sid}.ogg").read_bytes() == b"OggSDONE"


def test_merge_falls_back_to_reencode_on_copy_failure(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZREENC0000000000000000"
    sdir = tmp_data / "sessions" / sid
    _make_parts(sdir, sid, [b"OggSAAA", b"OggSBBB"])
    log_path = tmp_data / "logs" / f"{sid}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    calls: list = []

    def _run(cmd, stdout=None, stderr=None):
        calls.append(cmd)
        out_path = Path(cmd[-1])

        class _R:
            pass

        # First call: -c copy → fail (rc!=0, no output written).
        if "copy" in cmd:
            r = _R()
            r.returncode = 1
            return r
        # Second call: re-encode → succeed, write output.
        out_path.write_bytes(b"REENCODED")
        r = _R()
        r.returncode = 0
        return r

    monkeypatch.setattr(subprocess, "run", _run)
    worker._merge_multi_audio(sdir, sid, log_path)

    out = sdir / f"{sid}.ogg"
    assert out.exists()
    assert out.read_bytes() == b"REENCODED"
    assert len(calls) == 2
    assert "copy" in calls[0]
    assert "libopus" in calls[1]


def test_merge_raises_when_both_attempts_fail(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZFAIL00000000000000000"
    sdir = tmp_data / "sessions" / sid
    _make_parts(sdir, sid, [b"OggSAAA", b"OggSBBB"])
    log_path = tmp_data / "logs" / f"{sid}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _run(cmd, stdout=None, stderr=None):
        class _R:
            returncode = 1

        return _R()

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="ffmpeg failed to merge"):
        worker._merge_multi_audio(sdir, sid, log_path)
