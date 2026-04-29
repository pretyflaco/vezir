"""Vezir CLI: `vezir serve`, `vezir scribe`, `vezir token`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import __version__, config


@click.group()
@click.version_option(__version__, prog_name="vezir")
def main():
    """vezir — internal scribe service wrapping meetscribe."""


# ── serve ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--host", default=None, help="Bind address (default $VEZIR_HOST or 0.0.0.0)")
@click.option("--port", default=None, type=int, help="Port (default $VEZIR_PORT or 8000)")
@click.option("--reload", is_flag=True, help="Auto-reload on code change (dev)")
def serve(host, port, reload):
    """Run the vezir HTTP service (FastAPI + worker)."""
    import uvicorn
    h = host or config.host()
    p = port or config.port()
    click.echo(f"vezir: data dir = {config.data_dir()}")
    click.echo(f"vezir: serving on http://{h}:{p}")
    uvicorn.run(
        "vezir.server.app:app",
        host=h,
        port=p,
        reload=reload,
    )


# ── scribe ────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--server", "server_url", default=None,
              help="Server URL (default $VEZIR_URL)")
@click.option("--token", default=None,
              help="Bearer token (default $VEZIR_TOKEN)")
@click.option("--title", default=None,
              help="Optional meeting title")
@click.option("-o", "--output-dir", default=None, type=click.Path(),
              help="Where `meet record` writes audio (default ~/meet-recordings)")
@click.option("--compress/--no-compress", default=True,
              help="Compress recorded WAV to OGG/Opus before upload (default: on)")
@click.argument("record_args", nargs=-1, type=click.UNPROCESSED)
def scribe(server_url, token, title, output_dir, compress, record_args):
    """Record a meeting locally and upload to vezir.

    Any RECORD_ARGS after `--` are forwarded to `meet record`.
    Example: vezir scribe --title standup -- --virtual-sink
    """
    from .client.scribe import run_scribe
    try:
        run_scribe(
            server_url=server_url,
            token=token,
            title=title,
            output_dir=Path(output_dir) if output_dir else None,
            extra_record_args=list(record_args) if record_args else None,
            compress=compress,
        )
    except KeyboardInterrupt:
        click.echo("vezir: interrupted", err=True)
        sys.exit(130)
    except Exception as exc:
        click.echo(f"vezir: error: {exc}", err=True)
        sys.exit(1)


# ── upload ────────────────────────────────────────────────────────────────────

@main.command("upload")
@click.option("--server", "server_url", default=None,
              help="Server URL (default $VEZIR_URL)")
@click.option("--token", default=None,
              help="Bearer token (default $VEZIR_TOKEN)")
@click.option("--title", default=None,
              help="Optional meeting title")
@click.option("--compress", is_flag=True,
              help="Compress WAV input to OGG/Opus before upload")
@click.argument(
    "audio_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def upload_cmd(server_url, token, title, compress, audio_file):
    """Upload an existing WAV/OGG recording to vezir."""
    from .client import uploader

    def fmt_bytes(nbytes: int) -> str:
        if nbytes < 1024:
            return f"{nbytes} B"
        if nbytes < 1024 * 1024:
            return f"{nbytes / 1024:.1f} KiB"
        if nbytes < 1024 * 1024 * 1024:
            return f"{nbytes / (1024 * 1024):.1f} MiB"
        return f"{nbytes / (1024 * 1024 * 1024):.1f} GiB"

    def progress(sent: int, total: int, elapsed: float) -> None:
        pct = (sent / total * 100) if total else 0.0
        rate = sent / elapsed if elapsed > 0 else 0.0
        remaining = max(total - sent, 0)
        eta = remaining / rate if rate > 0 else 0.0
        click.echo(
            f"\rupload: {pct:5.1f}%  {fmt_bytes(sent)}/{fmt_bytes(total)}  "
            f"{fmt_bytes(int(rate))}/s  ETA {int(eta)}s",
            nl=False,
        )

    def on_retry(attempt: int, retries: int, exc: Exception) -> None:
        click.echo(
            f"\nvezir: upload attempt {attempt}/{retries} failed; "
            f"retrying from byte 0: {exc}"
        )

    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        click.echo("vezir: error: VEZIR_TOKEN is not set", err=True)
        sys.exit(1)

    try:
        audio_file = uploader.validate_audio_path(audio_file)
        if compress and audio_file.suffix.lower() == ".wav":
            before = audio_file.stat().st_size
            click.echo("vezir: compressing WAV to OGG/Opus before upload ...")
            audio_file = uploader.compress_wav_for_upload(audio_file, keep_wav=True)
            after = audio_file.stat().st_size
            ratio = before / after if after else 0
            click.echo(
                f"vezir: compressed {fmt_bytes(before)} -> {fmt_bytes(after)} "
                f"({ratio:.1f}x smaller)"
            )
        click.echo(f"vezir: uploading {audio_file} to {server_url} ...")
        result = uploader.upload(
            server_url,
            token,
            audio_file,
            title=title,
            progress=progress,
            on_retry=on_retry,
        )
        click.echo()
    except Exception as exc:
        click.echo(f"vezir: error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"vezir: uploaded as session {result['session_id']}")
    if "bytes" in result:
        click.echo(f"vezir: bytes uploaded: {result['bytes']:,}")
    if result.get("dashboard_url"):
        click.echo(f"vezir: dashboard: {result['dashboard_url']}")
    if result.get("dashboard_login_url"):
        click.echo(f"vezir: open in browser: {result['dashboard_login_url']}")


# ── gui ───────────────────────────────────────────────────────────────────────

@main.command()
def gui():
    """Launch the scribe GUI widget (always-on-top, Tkinter)."""
    try:
        from .client.gui import launch
    except ImportError as exc:
        click.echo(
            f"vezir gui requires Tkinter, which is not available: {exc}\n"
            "On Debian/Ubuntu: sudo apt install python3-tk",
            err=True,
        )
        sys.exit(1)
    sys.exit(launch())


# ── token ─────────────────────────────────────────────────────────────────────

@main.group()
def token():
    """Manage scribe bearer tokens (server-side)."""


@token.command("issue")
@click.option("--github", required=True, help="GitHub handle of the scribe")
def token_issue(github):
    """Issue a new bearer token. Prints plaintext ONCE; not recoverable."""
    from .server import auth
    plaintext = auth.issue(github)
    click.echo(f"Token issued for github={github}")
    click.echo(f"  VEZIR_TOKEN={plaintext}")
    click.echo("Hand this to the scribe; it is not recoverable.")


@token.command("revoke")
@click.option("--github", required=True, help="GitHub handle to revoke")
def token_revoke(github):
    """Revoke all tokens for a given GitHub handle."""
    from .server import auth
    n = auth.revoke(github)
    click.echo(f"Removed {n} token(s) for github={github}")


@token.command("enroll")
@click.option("--github", required=True, help="GitHub handle of the scribe")
@click.option("--server", "server_url", default=None,
              help="Server URL the device should connect to "
                   "(default $VEZIR_URL or computed). Used only to print "
                   "a convenience link; the token is also printed for paste.")
def token_enroll(github, server_url):
    """Issue a token and print enrollment instructions for a mobile device.

    Convenience wrapper around `vezir token issue` that also prints a
    pre-filled `/admin/enroll` URL the operator can open in their browser
    to display a QR code for the Android app to scan.
    """
    from urllib.parse import quote
    from .server import auth
    plaintext = auth.issue(github)

    # Best-effort server URL: explicit --server, then $VEZIR_URL, falling
    # back to config.server_url()'s default. Operators can pass --server to
    # override the default.
    base = (server_url or config.server_url()).rstrip("/")
    enroll_link = f"{base}/admin/enroll"

    click.echo(f"Token issued for github={github}")
    click.echo(f"  VEZIR_TOKEN={plaintext}")
    click.echo()
    click.echo("To enroll an Android (or other QR-friendly) device:")
    click.echo(
        f"  1. Open {enroll_link} in an authenticated browser tab on the "
        "operator's machine."
    )
    click.echo(
        "  2. Paste the server URL the device should connect to and the "
        "token above."
    )
    click.echo(
        "  3. Scan the QR with the Vezir Android app, or paste the JSON "
        "payload manually."
    )
    click.echo("  4. Close the tab once enrollment finishes.")
    click.echo()
    click.echo(
        "Avoid putting the token in the URL bar; use the form on the page."
    )
    click.echo("This token is not recoverable; revoke and re-issue if lost.")


@token.command("list")
def token_list():
    """List token entries (handles only; never the plaintext)."""
    p = config.tokens_json_path()
    if not p.exists():
        click.echo("(no tokens issued)")
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    for entry in data.get("tokens", []):
        click.echo(f"  {entry['github']}  issued {entry['issued_at']}")


# ── voiceprints ───────────────────────────────────────────────────────────────

@main.group()
def voiceprints():
    """Manage the central voiceprint database."""


@voiceprints.command("seed")
@click.option(
    "--from", "source", required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to an existing meetscribe speaker_profiles.json to copy in",
)
def voiceprints_seed(source):
    """Seed the central voiceprint DB from an existing meetscribe profile file."""
    from .server import voiceprints as vp_mod
    n = vp_mod.seed_from(source)
    click.echo(f"Seeded {n} profile(s) into {config.speaker_profiles_path()}")


@voiceprints.command("list")
def voiceprints_list():
    """List names enrolled in the central voiceprint DB."""
    from .server import voiceprints as vp_mod
    names = vp_mod.list_known_names()
    if not names:
        click.echo("(no voiceprints)")
        return
    for n in names:
        click.echo(f"  {n}")


# ── status ────────────────────────────────────────────────────────────────────

@main.command()
def status():
    """Print server-side runtime info (paths, counts)."""
    from .server import queue
    click.echo(f"vezir version: {__version__}")
    click.echo(f"data dir:      {config.data_dir()}")
    click.echo(f"sessions dir:  {config.sessions_dir()}")
    click.echo(f"profile DB:    {config.speaker_profiles_path()}")
    click.echo(f"queue DB:      {config.queue_db_path()}")
    rows = queue.list_recent(limit=200)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    click.echo(f"recent jobs ({len(rows)} of last 200):")
    for k, v in sorted(by_status.items()):
        click.echo(f"  {k}: {v}")


if __name__ == "__main__":
    main()
