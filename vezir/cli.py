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
@click.option("--wait/--no-wait", default=True,
              help="Wait for server processing and report status (default: on)")
@click.option("--wait-timeout", default=600, type=int,
              help="Max seconds to wait for processing (default: 600)")
@click.option("--open-labeling", is_flag=True, default=False,
              help="Auto-open labeling page in browser when speakers need labeling")
@click.option("--preset",
    type=click.Choice(["high-quality", "confidential", "alternative"], case_sensitive=False),
    default=None,
    help="Summarization quality/privacy preset")
@click.option("--auto-label/--no-auto-label", "auto_label", default=None,
              help="Auto-label speakers against the central voiceprint DB "
                   "(default: on; persists across launches)")
@click.option("--sync/--no-sync", "sync", default=None,
              help="Sync session artifacts to the configured destination "
                   "repo (default: on; persists across launches)")
@click.option("--personal", is_flag=True, default=False,
              help="Mark recording as personal (private to you, never synced; "
                   "hidden from other team members' session lists). "
                   "Per-recording flag; not persisted.")
@click.argument("record_args", nargs=-1, type=click.UNPROCESSED)
def scribe(server_url, token, title, output_dir, compress, wait, wait_timeout,
           open_labeling, preset, auto_label, sync, personal, record_args):
    """Record a meeting locally and upload to vezir.

    Any RECORD_ARGS after `--` are forwarded to `meet record`.
    Example: vezir scribe --title standup -- --virtual-sink
    """
    from .client.scribe import run_scribe
    from .client.config import (
        load_client_prefs, save_client_prefs,
    )

    prefs = load_client_prefs()
    if auto_label is None:
        auto_label = prefs.get("auto_label", True)
    else:
        prefs["auto_label"] = auto_label
        save_client_prefs(prefs)
    if sync is None:
        sync = prefs.get("sync", True)
    else:
        prefs["sync"] = sync
        save_client_prefs(prefs)

    try:
        run_scribe(
            server_url=server_url,
            token=token,
            title=title,
            output_dir=Path(output_dir) if output_dir else None,
            extra_record_args=list(record_args) if record_args else None,
            compress=compress,
            wait=wait,
            wait_timeout=float(wait_timeout),
            open_labeling=open_labeling,
            summary_preset=preset,
            auto_label=auto_label,
            sync=sync,
            personal=personal,
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
@click.option("--preset",
    type=click.Choice(["high-quality", "confidential", "alternative"], case_sensitive=False),
    default=None,
    help="Summarization quality/privacy preset")
@click.option("--auto-label/--no-auto-label", "auto_label", default=None,
              help="Auto-label speakers against the central voiceprint DB "
                   "(default: on; persists across launches)")
@click.option("--sync/--no-sync", "sync", default=None,
              help="Sync session artifacts to the configured destination "
                   "repo (default: on; persists across launches)")
@click.option("--wait/--no-wait", default=False,
              help="Wait for server processing and report status (default: off)")
@click.option("--wait-timeout", default=600, type=int,
              help="Max seconds to wait for processing (default: 600)")
@click.option("--personal", is_flag=True, default=False,
              help="Mark upload as personal (private to you, never synced; "
                   "hidden from other team members' session lists). "
                   "Per-upload flag; not persisted.")
@click.argument(
    "audio_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def upload_cmd(server_url, token, title, compress, preset, auto_label, sync,
               wait, wait_timeout, personal, audio_file):
    """Upload an existing WAV/OGG recording to vezir."""
    from .client import uploader
    from .client.config import load_client_prefs, save_client_prefs

    prefs = load_client_prefs()
    if auto_label is None:
        auto_label = prefs.get("auto_label", True)
    else:
        prefs["auto_label"] = auto_label
        save_client_prefs(prefs)
    if sync is None:
        sync = prefs.get("sync", True)
    else:
        prefs["sync"] = sync
        save_client_prefs(prefs)

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
    config.validate_token_format(token)

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
        if personal:
            # Match server-side behavior: personal sessions are never
            # synced regardless of the --sync flag.  Make the local
            # log honest about it.
            sync = False
        click.echo(f"vezir: uploading {audio_file} to {server_url} ...")
        result = uploader.upload(
            server_url,
            token,
            audio_file,
            title=title,
            summary_preset=preset,
            auto_label=auto_label,
            sync=sync,
            personal=personal,
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

    if wait:
        from .client.scribe import poll_status
        click.echo("vezir: waiting for processing ...")
        poll_status(server_url, token, result["session_id"], timeout=float(wait_timeout))


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


# ── tui ───────────────────────────────────────────────────────────────────────

@main.command()
@click.option(
    "--serve", is_flag=True, default=False,
    help="Publish the TUI over HTTP via textual-serve (requires "
         "`pip install textual-serve`); use to browse-share the same "
         "interface (drop-in for the web dashboard once v0.5 lands).",
)
@click.option(
    "--host", default="127.0.0.1", show_default=True,
    help="Bind address for --serve (ignored without --serve).",
)
@click.option(
    "--port", default=8800, show_default=True, type=int,
    help="Port for --serve (ignored without --serve).",
)
def tui(serve: bool, host: str, port: int):
    """Launch the vezir Textual TUI (terminal-native thin client).

    Replaces the Tkinter `vezir gui` for everything except the
    always-on-top floating record widget.  Provides feature parity
    with vezir-android 0.2.5: session list, detail, retry-summary
    with preset picker, share, native speaker labeling with audio
    clip playback, artifact viewer.
    """
    try:
        from .client.tui import launch_tui
    except ImportError as exc:
        click.echo(
            f"vezir tui requires textual, which is not available: {exc}\n"
            "Install with: pip install 'vezir[tui]'  or  pip install textual",
            err=True,
        )
        sys.exit(1)
    sys.exit(launch_tui(serve=serve, host=host, port=port))


# ── token ─────────────────────────────────────────────────────────────────────

@main.group()
def token():
    """Manage scribe bearer tokens (server-side)."""


_DEFAULT_TOKEN_LIFETIME = "90d"


def _parse_duration(s: str) -> int:
    """Parse a duration like ``30d``, ``12h``, ``45m``, ``never`` to seconds.

    Returns 0 for ``never`` (= no expiry). Raises ``click.BadParameter`` on
    malformed input. Suffixes: s/m/h/d/w. Bare integer is interpreted as
    seconds.
    """
    if not s:
        raise click.BadParameter("empty duration")
    s = s.strip().lower()
    if s in ("never", "none", "0"):
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
    if s[-1] in units:
        try:
            n = int(s[:-1])
        except ValueError:
            raise click.BadParameter(
                f"invalid duration {s!r}; use e.g. 30d, 12h, 45m, or 'never'"
            )
        if n < 0:
            raise click.BadParameter("duration must be non-negative")
        return n * units[s[-1]]
    try:
        n = int(s)
    except ValueError:
        raise click.BadParameter(
            f"invalid duration {s!r}; use e.g. 30d, 12h, 45m, or 'never'"
        )
    if n < 0:
        raise click.BadParameter("duration must be non-negative")
    return n


@token.command("issue")
@click.option("--github", required=True, help="GitHub handle of the scribe")
@click.option(
    "--expires-in", default=_DEFAULT_TOKEN_LIFETIME, show_default=True,
    help="Token lifetime (e.g. 30d, 12h, 45m, 'never'). Default 90d.",
)
@click.option(
    "--admin", "is_admin", is_flag=True, default=False,
    help="Mark this token as admin-tier (required for /admin/enroll).",
)
@click.option(
    "--label", default=None,
    help="Free-text annotation (e.g. 'android-phone'); shown by `token list`.",
)
def token_issue(github, expires_in, is_admin, label):
    """Issue a new bearer token. Prints plaintext ONCE; not recoverable."""
    from .server import auth
    seconds = _parse_duration(expires_in)
    plaintext = auth.issue(
        github,
        expires_in_seconds=seconds if seconds > 0 else None,
        is_admin=is_admin,
        label=label,
    )
    click.echo(f"Token issued for github={github}")
    click.echo(f"  VEZIR_TOKEN={plaintext}")
    if seconds == 0:
        click.echo("  expires:  never")
    else:
        import time as _time
        exp = _time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() + seconds),
        )
        click.echo(f"  expires:  {exp}  ({expires_in})")
    if is_admin:
        click.echo("  role:     admin (can reach /admin/* routes)")
    if label:
        click.echo(f"  label:    {label}")
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
@click.option(
    "--expires-in", default=_DEFAULT_TOKEN_LIFETIME, show_default=True,
    help="Token lifetime (e.g. 30d, 12h, 45m, 'never'). Default 90d.",
)
@click.option(
    "--label", default=None,
    help="Free-text annotation (e.g. 'android-phone').",
)
def token_enroll(github, server_url, expires_in, label):
    """Issue a token and print enrollment instructions for a mobile device.

    Convenience wrapper around `vezir token issue` that also prints a
    pre-filled `/admin/enroll` URL the operator can open in their browser
    to display a QR code for the Android app to scan.
    """
    from .server import auth
    seconds = _parse_duration(expires_in)
    plaintext = auth.issue(
        github,
        expires_in_seconds=seconds if seconds > 0 else None,
        is_admin=False,  # device tokens are scribe-tier; admins re-issue separately
        label=label,
    )

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
@click.option("--dormant", "dormant_days", type=int, default=None,
              help="Only show tokens with no successful use in the last N days.")
def token_list(dormant_days):
    """List token entries (handles, expiry, last use; never the plaintext).

    Columns: github, role, label, issued, expires, last_used. ``expires``
    of ``never`` is a legacy (pre-0.1.12) row or an explicit
    ``--expires-in never`` issue. ``last_used`` is updated at most once
    per minute per token (debounced) and only reflects successful auth.

    With ``--dormant N`` only rows whose ``last_used`` is older than N
    days (or ``never used``) appear — useful for "who should I rotate?".
    """
    import time as _time

    p = config.tokens_json_path()
    if not p.exists():
        click.echo("(no tokens issued)")
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = data.get("tokens", [])
    if not rows:
        click.echo("(no tokens issued)")
        return

    def _age_seconds(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            t = _time.mktime(
                _time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            ) - _time.timezone
            return _time.time() - t
        except Exception:
            return None

    if dormant_days is not None:
        threshold_sec = dormant_days * 86400
        kept = []
        for entry in rows:
            age = _age_seconds(entry.get("last_used_at"))
            if age is None or age >= threshold_sec:
                kept.append(entry)
        rows = kept
        if not rows:
            click.echo(
                f"(no tokens dormant for >= {dormant_days} days)"
            )
            return

    # Column widths.
    def _w(items: list[str], min_w: int) -> int:
        return max([min_w] + [len(s) for s in items])

    githubs = [str(r.get("github") or "?") for r in rows]
    labels = [str(r.get("label") or "-") for r in rows]
    roles = ["admin" if r.get("is_admin") else "scribe" for r in rows]
    g_w = _w(githubs, len("github"))
    l_w = _w(labels, len("label"))
    r_w = _w(roles, len("role"))

    click.echo(
        f"  {'github':<{g_w}}  {'role':<{r_w}}  {'label':<{l_w}}  "
        f"{'issued':<20}  {'expires':<20}  last_used"
    )
    for entry in rows:
        github = str(entry.get("github") or "?")
        role = "admin" if entry.get("is_admin") else "scribe"
        label = str(entry.get("label") or "-")
        issued = str(entry.get("issued_at") or "?")
        expires = entry.get("expires_at") or "never"
        last_used = entry.get("last_used_at") or "never used"
        # Annotate expired rows so the operator can spot revoke-and-reissue
        # candidates at a glance.
        age = _age_seconds(expires) if expires != "never" else None
        suffix = "  (expired)" if age is not None and age > 0 else ""
        click.echo(
            f"  {github:<{g_w}}  {role:<{r_w}}  {label:<{l_w}}  "
            f"{issued:<20}  {expires:<20}  {last_used}{suffix}"
        )


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
@click.option(
    "--merge", is_flag=True, default=False,
    help="Merge into the existing central DB instead of refusing when it is populated. "
    "Per-name policy: the profile with the higher n_sessions wins.",
)
def voiceprints_seed(source, merge):
    """Seed or merge the central voiceprint DB from an existing meetscribe profile file."""
    from .server import voiceprints as vp_mod
    stats = vp_mod.seed_from(source, merge=merge)
    click.echo(
        f"Done: {stats['added']} added, {stats['updated']} updated, "
        f"{stats['kept']} kept (source had fewer sessions). "
        f"Central DB now has {stats['total']} profile(s) at {config.speaker_profiles_path()}"
    )


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
