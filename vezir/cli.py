"""Vezir CLI: `vezir serve`, `vezir scribe`, `vezir token`."""
from __future__ import annotations

import sys
from pathlib import Path

import click

from . import __version__, config


@click.group()
@click.version_option(__version__, prog_name="vezir")
def main():
    """vezir — internal scribe service wrapping millet."""
    import logging
    # Ensure client-side loggers (vezir.client.*) have a handler so
    # warnings/errors are visible.  Server-side code calls
    # config.configure_logging() in create_app() instead.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


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
        # Honor X-Forwarded-For/-Proto from the loopback Caddy proxy ONLY.
        # Without this every request behind Caddy carried the proxy's IP,
        # so all teammates shared one per-IP login rate bucket (one
        # runaway client locked out everyone) and log attribution was
        # useless.  forwarded_allow_ips pins trust to localhost so a
        # caller reaching uvicorn directly cannot spoof its IP.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
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
              help="Where `millet record` writes audio (default ~/millet-recordings)")
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
@click.option("--no-pause", "no_pause", is_flag=True, default=False,
              help="Skip the post-recording pause that offers a last chance "
                   "to drop files into the attachments folder (automatically "
                   "skipped when stdin is not a TTY).")
@click.argument("record_args", nargs=-1, type=click.UNPROCESSED)
def scribe(server_url, token, title, output_dir, compress, wait, wait_timeout,
           open_labeling, preset, auto_label, sync, personal, no_pause,
           record_args):
    """Record a meeting locally and upload to vezir.

    Any RECORD_ARGS after `--` are forwarded to `millet record`.
    Example: vezir scribe --title standup -- --virtual-sink
    """
    from .client.config import (
        load_client_prefs,
        save_client_prefs,
    )
    from .client.scribe import run_scribe

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
            no_pause=no_pause,
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
@click.option("--team", "team", default=None,
              help="Team slug/id to upload to (default: active team in "
                   "teams.json or $VEZIR_TEAM_ID)")
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
def upload_cmd(server_url, token, team, title, compress, preset, auto_label, sync,
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

    # Resolve credentials + the active team so the upload carries the
    # X-Team-Id header (required by v0.7.0+ servers; a missing header is a
    # hard 400).  Precedence: explicit --server/--token/--team overrides,
    # then --team's teams.json entry, then resolve_credentials() (env →
    # teams.json active → client.json).
    from .client.config import resolve_credentials, team_credentials

    team_id: str | None = None
    if team:
        t_id, t_url, t_token = team_credentials(team)
        if t_id is None:
            # Not in teams.json locally; still pass the slug through — the
            # server resolves slugs to uuids.  url/token must come from
            # elsewhere (--server/--token/env).
            team_id = team
        else:
            team_id = t_id
            server_url = server_url or t_url
            token = token or t_token
    if server_url is None or token is None or team_id is None:
        r_url, r_token, r_team, _src = resolve_credentials()
        server_url = server_url or r_url
        token = token or r_token
        if team_id is None:
            team_id = r_team
    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        click.echo("vezir: error: VEZIR_TOKEN is not set", err=True)
        sys.exit(1)
    config.validate_token_format(token)
    if not team_id:
        click.echo(
            "vezir: error: no team selected; pass --team <slug>, set "
            "VEZIR_TEAM_ID, or run `vezir login` to populate teams.json",
            err=True,
        )
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
        if personal:
            # Match server-side behavior: personal sessions are never
            # synced regardless of the --sync flag.  Make the local
            # log honest about it.
            sync = False
        click.echo(f"vezir: uploading {audio_file} to {server_url} ...")
        upload_kwargs = dict(
            title=title,
            summary_preset=preset,
            auto_label=auto_label,
            sync=sync,
            personal=personal,
            progress=progress,
            on_retry=on_retry,
            team_id=team_id,
        )
        # Prefer the resumable protocol (the original failure was on a
        # resumable endpoint; it's the more robust path), fall back to the
        # one-shot endpoint when the server is too old to expose it.
        if uploader.server_supports_resumable(server_url, token, team_id=team_id):
            result = uploader.upload_resumable(
                server_url, token, audio_file, **upload_kwargs
            )
        else:
            result = uploader.upload(
                server_url, token, audio_file, **upload_kwargs
            )
        click.echo()
    except Exception as exc:
        click.echo(f"vezir: error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"vezir: uploaded as session {result['session_id']}")
    if "bytes" in result:
        click.echo(f"vezir: bytes uploaded: {result['bytes']:,}")

    if wait:
        from .client.scribe import poll_status
        click.echo("vezir: waiting for processing ...")
        poll_status(
            server_url, token, result["session_id"],
            timeout=float(wait_timeout), team_id=team_id,
        )


@main.command("upload-multi")
@click.option("--server", "server_url", default=None,
              help="Server URL (default $VEZIR_URL)")
@click.option("--token", default=None,
              help="Bearer token (default $VEZIR_TOKEN)")
@click.option("--team", "team", default=None,
              help="Team slug/id to upload to (default: active team in "
                   "teams.json or $VEZIR_TEAM_ID)")
@click.option("--title", default=None,
              help="Optional meeting title")
@click.option("--dir", "from_dir", default=None,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Directory of audio files to treat as one meeting "
                   "(.wav/.ogg/.mp3, merged in filename order)")
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
              help="Mark upload as personal (private to you, never synced).")
@click.argument(
    "audio_files",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def upload_multi_cmd(server_url, token, team, title, from_dir, preset, auto_label,
                     sync, wait, wait_timeout, personal, audio_files):
    """Upload several audio files as ONE meeting.

    Pass multiple files, or a directory via --dir.  Files are merged in
    filename order (e.g. timestamped Telegram voicenotes) into a single
    meeting on the server before transcription.

        vezir upload-multi a.ogg b.ogg c.ogg --title "Standup"
        vezir upload-multi --dir ./voicenotes/ --no-auto-label
    """
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

    # Collect + order the input files (filename order).
    paths: list[Path] = list(audio_files)
    if from_dir:
        for ext in (".wav", ".ogg", ".mp3"):
            paths.extend(sorted(Path(from_dir).glob(f"*{ext}")))
    # De-dup while preserving order, then sort by filename for determinism.
    seen: set = set()
    deduped: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            deduped.append(p)
    paths = sorted(deduped, key=lambda p: p.name)
    if not paths:
        click.echo(
            "vezir: error: no audio files given; pass files or --dir <dir>",
            err=True,
        )
        sys.exit(1)

    def on_retry(attempt: int, retries: int, exc: Exception) -> None:
        click.echo(
            f"\nvezir: upload attempt {attempt}/{retries} failed; "
            f"retrying: {exc}"
        )

    from .client.config import resolve_credentials, team_credentials

    team_id: str | None = None
    if team:
        t_id, t_url, t_token = team_credentials(team)
        if t_id is None:
            team_id = team
        else:
            team_id = t_id
            server_url = server_url or t_url
            token = token or t_token
    if server_url is None or token is None or team_id is None:
        r_url, r_token, r_team, _src = resolve_credentials()
        server_url = server_url or r_url
        token = token or r_token
        if team_id is None:
            team_id = r_team
    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        click.echo("vezir: error: VEZIR_TOKEN is not set", err=True)
        sys.exit(1)
    config.validate_token_format(token)
    if not team_id:
        click.echo(
            "vezir: error: no team selected; pass --team <slug>, set "
            "VEZIR_TEAM_ID, or run `vezir login` to populate teams.json",
            err=True,
        )
        sys.exit(1)

    if personal:
        sync = False

    click.echo(
        f"vezir: uploading {len(paths)} file(s) as one meeting to "
        f"{server_url} ..."
    )
    for i, p in enumerate(paths):
        click.echo(f"  [{i:03d}] {p.name}")
    try:
        result = uploader.upload_multi(
            server_url, token, paths,
            title=title,
            summary_preset=preset,
            auto_label=auto_label,
            sync=sync,
            personal=personal,
            on_retry=on_retry,
            team_id=team_id,
        )
    except Exception as exc:
        click.echo(f"vezir: error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"vezir: uploaded as session {result['session_id']}")
    if "parts" in result:
        click.echo(f"vezir: parts: {result['parts']}")
    if "bytes" in result:
        click.echo(f"vezir: bytes uploaded: {result['bytes']:,}")

    if wait:
        from .client.scribe import poll_status
        click.echo("vezir: waiting for processing ...")
        poll_status(
            server_url, token, result["session_id"],
            timeout=float(wait_timeout), team_id=team_id,
        )


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
        except ValueError as exc:
            raise click.BadParameter(
                f"invalid duration {s!r}; use e.g. 30d, 12h, 45m, or 'never'"
            ) from exc
        if n < 0:
            raise click.BadParameter("duration must be non-negative")
        return n * units[s[-1]]
    try:
        n = int(s)
    except ValueError as exc:
        raise click.BadParameter(
            f"invalid duration {s!r}; use e.g. 30d, 12h, 45m, or 'never'"
        ) from exc
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
    help="Mark this token as admin-tier (gates /admin/* routes).",
)
@click.option(
    "--label", default=None,
    help="Free-text annotation (e.g. 'android-phone'); shown by `token list`.",
)
def token_issue(github, expires_in, is_admin, label):
    """Issue a new bearer token. Prints plaintext ONCE; not recoverable.

    v0.7.0: tokens are no longer team-scoped.  The token identifies a
    human; per-request the client sends an ``X-Team-Id`` header naming
    which team to operate on.  The user must already be a member of
    that team (see ``vezir team add-member``).
    """
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
    click.echo()
    click.echo(
        "NOTE: this token has no team scope.  The user must be a "
        "member of every team they want to operate on.  Use "
        "`vezir team add-member --team <id> --github "
        f"{github}` to grant access.",
    )
    click.echo("Hand this to the scribe; it is not recoverable.")


@token.command("revoke")
@click.option(
    "--github", default=None,
    help="GitHub handle to revoke (with no other filter: revokes ALL of that "
         "handle's tokens; with --label/--token-id/--team: narrows the match).",
)
@click.option(
    "--label", default=None,
    help="Restrict to tokens with this exact label (e.g. 'android-phone'). "
         "Pass '-' to match label-less tokens.",
)
@click.option(
    "--token-id", "token_id_prefix", default=None,
    help="Restrict to tokens whose id (first chars of stored hash, as shown "
         "by `vezir token list --show-id`) starts with this prefix. "
         "Min 4 chars.",
)
@click.option(
    "--yes", "-y", "skip_confirm", is_flag=True, default=False,
    help="Skip the interactive confirmation prompt.",
)
def token_revoke(github, label, token_id_prefix, skip_confirm):
    """Revoke tokens by handle / label / id.

    v0.7.0: the ``--team`` filter was removed because tokens are no
    longer team-scoped.  To remove a human's access to a team, use
    ``vezir team remove-member`` instead.

    Examples:

      \b
      # Revoke every token for alice:
      vezir token revoke --github alice

      \b
      # Revoke just alice's lost phone:
      vezir token revoke --github alice --label android-phone

      \b
      # Revoke a specific token by id when one handle has duplicates:
      vezir token revoke --token-id 9f3b2a1c
    """
    from .server import auth

    if github is None and label is None and token_id_prefix is None:
        click.echo(
            "error: at least one filter is required "
            "(--github / --label / --token-id). "
            "Refusing to nuke the entire token store.",
            err=True,
        )
        sys.exit(2)

    try:
        # Dry-run pass first so we can show the operator exactly what
        # would be revoked and prompt before mutating tokens.json.
        all_rows = auth.list_tokens()
    except Exception as exc:  # pragma: no cover - read failure is unusual
        click.echo(f"error: failed to read tokens.json: {exc}", err=True)
        sys.exit(2)

    if token_id_prefix is not None and len(token_id_prefix) < 4:
        click.echo(
            "error: --token-id must be at least 4 characters",
            err=True,
        )
        sys.exit(2)

    def _matches(row: dict) -> bool:
        if github is not None and row.get("github") != github:
            return False
        if label is not None:
            row_label = row.get("label")
            if label == "-":
                if row_label is not None:
                    return False
            else:
                if row_label != label:
                    return False
        if token_id_prefix is not None:
            if not (row.get("token_id") or "").startswith(token_id_prefix):
                return False
        return True

    matched = [r for r in all_rows if _matches(r)]
    if not matched:
        click.echo("(no tokens matched; nothing to revoke)")
        return

    # Preview.
    click.echo(f"Would revoke {len(matched)} token(s):")
    for row in matched:
        click.echo(
            f"  github={row.get('github') or '?'}  "
            f"label={row.get('label') or '-'}  "
            f"id={row.get('token_id') or '?'}  "
            f"issued={row.get('issued_at') or '?'}"
        )
    if not skip_confirm:
        if not click.confirm("Proceed?", default=False):
            click.echo("Aborted; no changes made.")
            sys.exit(1)

    removed = auth.revoke_by_filter(
        github=github,
        label=label,
        token_id_prefix=token_id_prefix,
    )
    click.echo(f"Removed {len(removed)} token(s).")


@token.command("enroll")
@click.option("--github", required=True, help="GitHub handle of the scribe")
@click.option("--server", "server_url", default=None,
              help="Server URL the device should connect to "
                   "(default $VEZIR_URL or computed).")
@click.option(
    "--expires-in", default=_DEFAULT_TOKEN_LIFETIME, show_default=True,
    help="Token lifetime (e.g. 30d, 12h, 45m, 'never'). Default 90d.",
)
@click.option(
    "--label", default=None,
    help="Free-text annotation (e.g. 'android-phone').",
)
def token_enroll(github, server_url, expires_in, label):
    """Issue a token and print a terminal QR for a mobile device to scan.

    v0.7.0: the HTML enrollment page was removed; this command now
    renders the QR payload directly in the terminal using half-block
    characters.  Tokens are no longer team-scoped -- the device sends
    ``X-Team-Id`` per-request -- so ``--team`` is gone.  The user
    must already be a member of every team they intend to use; if not,
    run ``vezir team add-member`` first.
    """
    from .server import auth
    from .server.enroll import _load_caddy_root_cert, build_payload, render_qr_terminal

    seconds = _parse_duration(expires_in)
    plaintext = auth.issue(
        github,
        expires_in_seconds=seconds if seconds > 0 else None,
        is_admin=False,
        label=label,
    )

    # Best-effort server URL.  --server wins, then $VEZIR_URL, then
    # config.server_url()'s default.
    base = (server_url or config.server_url()).rstrip("/")

    # Build the QR payload.  When VEZIR_CADDY_ROOT_CERT_PATH points at
    # a readable PEM, _load_caddy_root_cert returns it and build_payload
    # emits a v2 payload with ca_pem embedded -- which the Android app's
    # CaTrustManager pins so TLS to the self-signed Caddy cert works.
    # When the env var is unset (or the file unreadable), ca_pem is None
    # and we fall back to a v1 payload that relies on the system trust
    # store.
    ca_pem = _load_caddy_root_cert()
    payload = build_payload(base, plaintext, ca_pem=ca_pem)
    qr_art = render_qr_terminal(payload)

    click.echo(f"Token issued for github={github}")
    click.echo(f"  server:   {base}")
    if label:
        click.echo(f"  label:    {label}")
    click.echo()
    click.echo("Scan this QR with the Vezir Android app:")
    click.echo()
    click.echo(qr_art)
    click.echo()
    click.echo(f"  VEZIR_TOKEN={plaintext}")
    click.echo()
    click.echo("(token also printed above for manual entry / desktop joiners)")
    click.echo("This token is not recoverable; revoke and re-issue if lost.")


@token.command("list")
@click.option("--dormant", "dormant_days", type=int, default=None,
              help="Only show tokens with no successful use in the last N days.")
@click.option(
    "--show-id", is_flag=True, default=False,
    help="Show a per-token id column (first 12 chars of the stored hash) "
         "usable with `vezir token revoke --token-id <prefix>`.",
)
def token_list(dormant_days, show_id):
    """List token entries (handles, expiry, last use; never the plaintext).

    v0.7.0: the ``team`` column was removed because tokens no longer
    carry a team scope.  To see who's on each team use
    ``vezir team members <id>``.

    Default columns: github, role, label, issued, expires, last_used.
    With ``--show-id`` an additional ``id`` column appears (a non-secret
    prefix of the stored hash, safe to copy-paste; this is the value
    accepted by ``vezir token revoke --token-id``).

    ``expires`` of ``never`` is a legacy (pre-0.1.12) row or an explicit
    ``--expires-in never`` issue.  ``last_used`` is updated at most once
    per minute per token (debounced) and only reflects successful auth.

    With ``--dormant N`` only rows whose ``last_used`` is older than N
    days (or ``never used``) appear -- useful for "who should I rotate?".
    """
    import calendar as _calendar
    import time as _time

    from .server import auth as _auth

    # v0.7.2: tokens live in vezir.sqlite, read via the auth helper which
    # returns a non-secret 12-char ``token_id`` (never the full hash).
    rows = _auth.list_tokens()
    if not rows:
        click.echo("(no tokens issued)")
        return

    def _age_seconds(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            t = _calendar.timegm(_time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
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
    ids = [str(r.get("token_id") or "?") for r in rows]
    g_w = _w(githubs, len("github"))
    l_w = _w(labels, len("label"))
    r_w = _w(roles, len("role"))
    i_w = _w(ids, len("id"))

    if show_id:
        click.echo(
            f"  {'github':<{g_w}}  {'role':<{r_w}}  "
            f"{'label':<{l_w}}  {'id':<{i_w}}  "
            f"{'issued':<20}  {'expires':<20}  last_used"
        )
    else:
        click.echo(
            f"  {'github':<{g_w}}  {'role':<{r_w}}  "
            f"{'label':<{l_w}}  "
            f"{'issued':<20}  {'expires':<20}  last_used"
        )
    for entry in rows:
        github = str(entry.get("github") or "?")
        role = "admin" if entry.get("is_admin") else "scribe"
        label = str(entry.get("label") or "-")
        tid = str(entry.get("token_id") or "?")
        issued = str(entry.get("issued_at") or "?")
        expires = entry.get("expires_at") or "never"
        last_used = entry.get("last_used_at") or "never used"
        # Annotate expired rows so the operator can spot revoke-and-reissue
        # candidates at a glance.
        age = _age_seconds(expires) if expires != "never" else None
        suffix = "  (expired)" if age is not None and age > 0 else ""
        if show_id:
            click.echo(
                f"  {github:<{g_w}}  {role:<{r_w}}  "
                f"{label:<{l_w}}  {tid:<{i_w}}  "
                f"{issued:<20}  {expires:<20}  {last_used}{suffix}"
            )
        else:
            click.echo(
                f"  {github:<{g_w}}  {role:<{r_w}}  "
                f"{label:<{l_w}}  "
                f"{issued:<20}  {expires:<20}  {last_used}{suffix}"
            )


# ── session (refresh-token sessions) ────────────────────────────────────────


@main.group()
def session():
    """Manage sessions: auth families (list/revoke) and recordings (move/rm/set-title).

    Two related roles share this group:

    * **Auth sessions** — each interactive login (nostr / Google) creates a
      *session family* with a short-lived access JWT and a rotating refresh
      token.  ``list`` / ``revoke`` inspect and revoke families directly
      against the local queue DB (operator posture, like ``token``).
      Revoking stops the refresh token from minting new access tokens; any
      outstanding access JWT still lapses within its short TTL.

    * **Recordings** — ``move`` / ``rm`` / ``set-title`` operate on
      individual recorded sessions (reassign team, delete, rename).

    (v0.12.1: these were previously split across two ``session`` groups; the
    second shadowed the first, so ``list`` / ``revoke`` were unreachable.)
    """


@session.command("list")
@click.option("--github", default=None, help="Filter to one GitHub handle.")
@click.option(
    "--all", "show_all", is_flag=True, default=False,
    help="Include revoked/expired sessions (default hides them).",
)
def session_list(github, show_all):
    """List session families, newest first."""
    import time as _time

    from .server import sessions_auth

    rows = sessions_auth.list_sessions(github)
    now = _time.time()

    def _expired(row) -> bool:
        exp = sessions_auth._parse_iso(row.get("refresh_expires_at"))
        cap = sessions_auth._parse_iso(row.get("absolute_max_at"))
        return (exp is not None and now >= exp) or (
            cap is not None and now >= cap
        )

    shown = [
        r for r in rows
        if show_all or (not r["revoked"] and not _expired(r))
    ]
    if not shown:
        click.echo("No matching sessions.")
        return

    click.echo(
        f"{'SID':<34}  {'GITHUB':<16}  {'METHOD':<7}  "
        f"{'CREATED':<20}  {'STATE'}"
    )
    for r in shown:
        if r["revoked"]:
            state = "revoked"
        elif _expired(r):
            state = "expired"
        else:
            state = "active"
        admin = "*" if r["is_admin"] else ""
        click.echo(
            f"{r['sid']:<34}  {r['github'] + admin:<16}  "
            f"{r['auth_method']:<7}  {r['created_at']:<20}  {state}"
        )


@session.command("revoke")
@click.option("--sid", default=None, help="Revoke one session by id.")
@click.option(
    "--github", default=None,
    help="Revoke ALL active sessions for a GitHub handle.",
)
@click.option(
    "--yes", "skip_confirm", is_flag=True, default=False,
    help="Skip the confirmation prompt.",
)
def session_revoke(sid, github, skip_confirm):
    """Revoke a session by --sid, or every session for a --github handle."""
    from .server import sessions_auth

    if bool(sid) == bool(github):
        raise click.UsageError("provide exactly one of --sid or --github.")

    if sid:
        if not skip_confirm and not click.confirm(
            f"Revoke session {sid}?"
        ):
            click.echo("Aborted.")
            return
        ok = sessions_auth.revoke_session(sid)
        click.echo("Revoked." if ok else "No active session with that id.")
        return

    if not skip_confirm and not click.confirm(
        f"Revoke ALL active sessions for {github!r}?"
    ):
        click.echo("Aborted.")
        return
    n = sessions_auth.revoke_all_for(github)
    click.echo(f"Revoked {n} session(s) for {github!r}.")


# ── npub (nostr allowlist) ──────────────────────────────────────────────────


@main.group()
def npub():
    """Manage the nostr login allowlist (server-side).

    A nostr member authenticates by signing a NIP-98 event with their
    nostr key (via `vezir login` / Amber) instead of presenting a
    ``vzr_`` bearer token.  This group maps an ``npub1…`` public key to a
    GitHub handle + privilege tier so the rest of the auth chain
    (team membership, admin gating) works identically to tokens.

    The user must still be a member of every team they operate on
    (``vezir team add-member``).
    """


@npub.command("add")
@click.option(
    "--npub", "npub_str", required=True,
    help="The user's nostr public key as 'npub1…' (or 64-char hex).",
)
@click.option("--github", required=True, help="GitHub handle to bind this key to.")
@click.option(
    "--admin", "is_admin", is_flag=True, default=False,
    help="Mark this member as admin-tier (gates /admin/* routes).",
)
@click.option(
    "--label", default=None,
    help="Free-text annotation (e.g. 'milfort-laptop'); shown by `npub list`.",
)
def npub_add(npub_str, github, is_admin, label):
    """Authorize a nostr public key to log in as a given GitHub handle.

    Idempotent: re-running with the same npub updates the github/admin/
    label fields (use case: promoting to admin, fixing a handle).
    """
    from . import nostr_nip19
    from .server import nostr_members

    try:
        pubkey_hex = nostr_nip19.to_hex(npub_str)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--npub") from exc

    nostr_members.add(pubkey_hex, github, is_admin=is_admin, label=label)
    click.echo(f"Authorized nostr key for github={github}")
    click.echo(f"  npub:   {nostr_nip19.encode_npub(pubkey_hex)}")
    click.echo(f"  hex:    {pubkey_hex}")
    if is_admin:
        click.echo("  role:   admin (can reach /admin/* routes)")
    if label:
        click.echo(f"  label:  {label}")
    click.echo()
    click.echo(
        "NOTE: this key has no team scope.  Ensure the user is a member "
        f"of each team they need: `vezir team add-member --github {github} "
        "--team <id>`.  They can now run `vezir login`."
    )


@npub.command("list")
def npub_list():
    """List authorized nostr keys (npub, github, role, label, added)."""
    from . import nostr_nip19
    from .server import nostr_members

    rows = nostr_members.list_members()
    if not rows:
        click.echo("(no nostr keys authorized)")
        return

    def _w(items, min_w):
        return max([min_w] + [len(s) for s in items])

    githubs = [str(r.get("github") or "?") for r in rows]
    labels = [str(r.get("label") or "-") for r in rows]
    roles = ["admin" if r.get("is_admin") else "scribe" for r in rows]
    g_w = _w(githubs, len("github"))
    l_w = _w(labels, len("label"))
    r_w = _w(roles, len("role"))

    click.echo(
        f"  {'github':<{g_w}}  {'role':<{r_w}}  {'label':<{l_w}}  "
        f"{'added':<20}  npub"
    )
    for entry in rows:
        github = str(entry.get("github") or "?")
        role = "admin" if entry.get("is_admin") else "scribe"
        label = str(entry.get("label") or "-")
        added = str(entry.get("added_at") or "?")
        try:
            display_npub = nostr_nip19.encode_npub(entry["npub"])
        except Exception:
            display_npub = entry.get("npub") or "?"
        click.echo(
            f"  {github:<{g_w}}  {role:<{r_w}}  {label:<{l_w}}  "
            f"{added:<20}  {display_npub}"
        )


@npub.command("remove")
@click.option(
    "--npub", "npub_str", required=True,
    help="The 'npub1…' (or 64-char hex) key to de-authorize.",
)
def npub_remove(npub_str):
    """Remove a nostr key from the allowlist.  Idempotent."""
    from . import nostr_nip19
    from .server import nostr_members

    try:
        pubkey_hex = nostr_nip19.to_hex(npub_str)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--npub") from exc

    removed = nostr_members.remove(pubkey_hex)
    if removed:
        click.echo(f"Removed nostr key {nostr_nip19.encode_npub(pubkey_hex)}")
    else:
        click.echo("(no matching nostr key; nothing removed)")


# ── google (Google sign-in allowlist) ───────────────────────────────────────


@main.group()
def google():
    """Manage the Google sign-in allowlist (server-side).

    A google member authenticates with their Workspace account (e.g.
    ``@blinkbtc.com``) via `vezir login --method google` instead of a
    nostr key or ``vzr_`` token.  This group maps a verified email to a
    GitHub handle + privilege tier so the rest of the auth chain works
    identically.  The user must still be a member of every team they
    operate on (``vezir team add-member``).

    Requires Google sign-in to be configured on the server
    (VEZIR_GOOGLE_CLIENT_ID + secret).
    """


@google.command("add")
@click.option(
    "--email", required=True,
    help="The user's Google account email (e.g. alice@blinkbtc.com).",
)
@click.option("--github", required=True, help="GitHub handle to bind this email to.")
@click.option(
    "--admin", "is_admin", is_flag=True, default=False,
    help="Mark this member as admin-tier (gates /admin/* routes).",
)
@click.option(
    "--label", default=None,
    help="Free-text annotation; shown by `google list`.",
)
def google_add(email, github, is_admin, label):
    """Authorize a Google email to log in as a given GitHub handle.

    Idempotent: re-running with the same email updates github/admin/label.
    """
    from .server import google_members

    try:
        google_members.add(email, github, is_admin=is_admin, label=label)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--email") from exc

    click.echo(f"Authorized google email for github={github}")
    click.echo(f"  email:  {email.strip().lower()}")
    if is_admin:
        click.echo("  role:   admin (can reach /admin/* routes)")
    if label:
        click.echo(f"  label:  {label}")
    click.echo()
    click.echo(
        "NOTE: this email has no team scope.  Ensure the user is a member "
        f"of each team they need: `vezir team add-member --github {github} "
        "--team <id>`.  They can now run `vezir login --method google`."
    )


@google.command("list")
def google_list():
    """List authorized Google emails (email, github, role, label, added)."""
    from .server import google_members

    rows = google_members.list_members()
    if not rows:
        click.echo("(no google emails authorized)")
        return

    def _w(items, min_w):
        return max([min_w] + [len(s) for s in items])

    githubs = [str(r.get("github") or "?") for r in rows]
    labels = [str(r.get("label") or "-") for r in rows]
    roles = ["admin" if r.get("is_admin") else "scribe" for r in rows]
    g_w = _w(githubs, len("github"))
    l_w = _w(labels, len("label"))
    r_w = _w(roles, len("role"))

    click.echo(
        f"  {'github':<{g_w}}  {'role':<{r_w}}  {'label':<{l_w}}  "
        f"{'added':<20}  email"
    )
    for entry in rows:
        github = str(entry.get("github") or "?")
        role = "admin" if entry.get("is_admin") else "scribe"
        label = str(entry.get("label") or "-")
        added = str(entry.get("added_at") or "?")
        email = str(entry.get("email") or "?")
        click.echo(
            f"  {github:<{g_w}}  {role:<{r_w}}  {label:<{l_w}}  "
            f"{added:<20}  {email}"
        )


@google.command("remove")
@click.option("--email", required=True, help="The Google email to de-authorize.")
def google_remove(email):
    """Remove a Google email from the allowlist.  Idempotent."""
    from .server import google_members

    try:
        removed = google_members.remove(email)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--email") from exc
    if removed:
        click.echo(f"Removed google email {email.strip().lower()}")
    else:
        click.echo("(no matching google email; nothing removed)")


# ── team ──────────────────────────────────────────────────────────────────────

@main.group()
def team():
    """Manage teams (v0.6.0+ multi-team support)."""


@team.command("list")
def team_list():
    """List all configured teams."""
    from .server import queue as _queue
    rows = _queue.list_teams()
    if not rows:
        click.echo("(no teams configured)")
        return
    # Column widths.  v0.7.4: the human-facing key is the slug; the uuid
    # is shown abbreviated for operators who need it (session move etc).
    slugs = [str(r.get("slug") or r.get("id") or "?") for r in rows]
    names = [str(r.get("name") or "?") for r in rows]
    remotes = [str(r.get("sync_remote") or "-") for r in rows]
    slug_w = max([len("slug")] + [len(s) for s in slugs])
    name_w = max([len("name")] + [len(s) for s in names])
    remote_w = max([len("sync_remote")] + [len(s) for s in remotes])
    click.echo(
        f"  {'slug':<{slug_w}}  {'name':<{name_w}}  "
        f"{'sync_remote':<{remote_w}}  {'meeting_type':<12}  uuid"
    )
    for r in rows:
        rslug = str(r.get("slug") or r.get("id") or "?")
        rname = str(r.get("name") or "?")
        rremote = str(r.get("sync_remote") or "-")
        rmt = str(r.get("sync_meeting_type") or "sandbox")
        ruuid = str(r.get("id") or "?")
        click.echo(
            f"  {rslug:<{slug_w}}  {rname:<{name_w}}  "
            f"{rremote:<{remote_w}}  {rmt:<12}  {ruuid}"
        )


@team.command("create")
@click.option("--id", "team_id", required=True,
              help="Slug (3-32 chars, lowercase alphanum + hyphen, "
                   "starts with a letter). Mutable display name "
                   "(v0.7.4+); the stable key is an auto-assigned uuid.")
@click.option("--name", required=True, help="Human display name.")
@click.option("--sync-remote", default=None,
              help="Git URL for this team's millet sync target.")
@click.option("--sync-meeting-type", default="sandbox", show_default=True,
              help="Meeting-type prefix passed to `millet sync`.")
def team_create(team_id, name, sync_remote, sync_meeting_type):
    """Create a new team."""
    from .server import queue as _queue
    try:
        team_uuid = _queue.create_team(
            team_id, name,
            sync_remote=sync_remote,
            sync_meeting_type=sync_meeting_type,
        )
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    click.echo(f"team created: slug={team_id} name={name!r} uuid={team_uuid}")
    if sync_remote:
        click.echo(f"  sync_remote: {sync_remote}")
    click.echo(f"  sync_meeting_type: {sync_meeting_type}")


@team.command("set-name")
@click.option("--id", "team_id", required=True, help="Team slug or uuid.")
@click.option("--name", required=True, help="New display name.")
def team_set_name(team_id, name):
    """Update a team's freeform display name.

    To change the slug, use ``vezir team rename`` (v0.7.4+).
    """
    from .server import queue as _queue
    try:
        _queue.update_team_name(team_id, name)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    row = _queue.get_team(team_id)
    click.echo(f"team renamed: slug={row['slug']} name={row['name']!r}")


@team.command("rename")
@click.option("--id", "team_id", required=True,
              help="Current team slug or uuid.")
@click.option("--new-slug", required=True, help="New slug.")
def team_rename(team_id, new_slug):
    """Change a team's slug (v0.7.4+).

    The team's stable uuid is unchanged, so jobs, memberships,
    on-disk dirs, and any client keyed on the uuid (every v0.5.2+
    client, via /api/me) survive the rename with no cascade.
    """
    from .server import queue as _queue
    try:
        _queue.rename_team_slug(team_id, new_slug)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    row = _queue.get_team(new_slug)
    click.echo(f"team slug changed: slug={row['slug']} uuid={row['id']}")


@team.command("delete")
@click.option("--id", "team_id", required=True, help="Team slug.")
@click.option(
    "--reassign-to", "reassign_to", default=None,
    help="If given, jobs are reassigned to this team and memberships "
         "are dropped.  If omitted, the command refuses when the team "
         "has any jobs or members still attached.",
)
@click.option(
    "--yes", "confirm", is_flag=True, default=False,
    help="Skip the interactive confirmation prompt.",
)
def team_delete(team_id, reassign_to, confirm):
    """Delete a team.

    Default policy is refuse-if-not-empty: operator must clean up
    jobs (``vezir session move``) and members (``vezir team
    remove-member``) first, then re-run.  Pass ``--reassign-to <slug>``
    to cascade both in one shot.

    v0.7.0: jobs are REASSIGNED to the destination team; memberships
    on the deleted team are DROPPED (not migrated -- the destination
    team's members are governed by its own membership table).  The
    on-disk ``teams/<id>/`` directory (roster, voiceprints,
    sync_config) is removed.
    """
    from .server import queue as _queue
    if _queue.get_team(team_id) is None:
        click.echo(f"error: team {team_id!r} does not exist", err=True)
        sys.exit(2)

    # Show a preview of what will happen.
    job_rows = [r for r in _queue.list_recent(limit=10_000)
                if r.get("team_id") == team_id]
    n_jobs = len(job_rows)
    n_members = len(_queue.get_team_members(team_id))

    click.echo(f"Deleting team {team_id!r}:")
    click.echo(f"  jobs in this team:    {n_jobs}")
    click.echo(f"  members of this team: {n_members}")
    if reassign_to:
        click.echo(f"  on cascade: jobs -> {reassign_to!r}, memberships -> DROPPED")
    else:
        click.echo("  cascade: NONE (will refuse if any jobs/members exist)")

    if not confirm:
        click.confirm("Proceed?", abort=True)

    try:
        stats = _queue.delete_team(team_id, reassign_to=reassign_to)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    click.echo(f"team {team_id!r} deleted.")
    if stats['reassigned_to']:
        click.echo(
            f"  jobs reassigned: {stats['jobs_reassigned']} "
            f"(-> {stats['reassigned_to']})"
        )
    else:
        click.echo("  jobs reassigned: 0")
    click.echo(f"  memberships dropped: {stats['members_dropped']}")
    click.echo(f"  on-disk dir removed: {stats['on_disk_removed']}")


@team.command("set-sync")
@click.option("--id", "team_id", required=True, help="Team slug.")
@click.option("--remote", "sync_remote", default=None,
              help="Git URL.  Pass empty string to clear.")
@click.option("--meeting-type", "sync_meeting_type", default=None,
              help="Meeting-type prefix (sandbox / production / ...).")
def team_set_sync(team_id, sync_remote, sync_meeting_type):
    """Update sync_remote and/or sync_meeting_type on an existing team."""
    from .server import queue as _queue
    if _queue.get_team(team_id) is None:
        click.echo(f"error: team {team_id!r} does not exist", err=True)
        sys.exit(2)
    kwargs: dict = {}
    if sync_remote is not None:
        kwargs["sync_remote"] = sync_remote or None
    if sync_meeting_type is not None:
        kwargs["sync_meeting_type"] = sync_meeting_type
    if not kwargs:
        click.echo("nothing to update (pass --remote and/or --meeting-type)")
        return
    try:
        _queue.update_team_sync(team_id, **kwargs)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    row = _queue.get_team(team_id)
    click.echo(f"team updated: id={team_id}")
    click.echo(f"  sync_remote: {row['sync_remote'] or '-'}")
    click.echo(f"  sync_meeting_type: {row['sync_meeting_type']}")


# ── memberships (v0.7.0) ─────────────────────────────────────────────────────


@team.command("add-member")
@click.option("--team", "team_id", required=True, help="Team slug.")
@click.option("--github", required=True, help="GitHub handle to add.")
@click.option(
    "--role", default="scribe", show_default=True,
    type=click.Choice(["admin", "scribe"]),
    help="Role within this team.  'admin' is per-team; the server-wide "
         "admin bit lives on the token, not here.",
)
def team_add_member(team_id, github, role):
    """Add a user to a team.

    v0.7.0: replaces the implicit "user is a member of every team
    they hold a token for".  The user can now be added without ever
    issuing them a new token -- their existing token will work
    against this team via ``X-Team-Id``.
    """
    from .server import queue as _queue
    if _queue.get_team(team_id) is None:
        click.echo(f"error: team {team_id!r} does not exist", err=True)
        sys.exit(2)
    try:
        _queue.add_membership(github, team_id, role=role, added_by="cli")
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    click.echo(
        f"member added: github={github} team={team_id} role={role}",
    )


@team.command("remove-member")
@click.option("--team", "team_id", required=True, help="Team slug.")
@click.option("--github", required=True, help="GitHub handle to remove.")
def team_remove_member(team_id, github):
    """Remove a user from a team.

    The user's tokens are NOT affected -- they may still be a member
    of other teams.  Use ``vezir token revoke --github <handle>`` to
    rotate their tokens too if needed.
    """
    from .server import queue as _queue
    if _queue.get_team(team_id) is None:
        click.echo(f"error: team {team_id!r} does not exist", err=True)
        sys.exit(2)
    removed = _queue.remove_membership(github, team_id)
    if not removed:
        click.echo(
            f"({github!r} is not a member of {team_id!r}; nothing to remove)",
        )
        return
    click.echo(f"member removed: github={github} team={team_id}")


@team.command("members")
@click.argument("team_id")
def team_members(team_id):
    """List members of a team with their per-team roles."""
    from .server import queue as _queue
    if _queue.get_team(team_id) is None:
        click.echo(f"error: team {team_id!r} does not exist", err=True)
        sys.exit(2)
    members = _queue.get_team_members(team_id)
    if not members:
        click.echo(f"(team {team_id!r} has no members)")
        return
    g_w = max([len("github")] + [len(m["github"]) for m in members])
    r_w = max([len("role")] + [len(m["role"]) for m in members])
    click.echo(
        f"  {'github':<{g_w}}  {'role':<{r_w}}  added_at              by",
    )
    for m in members:
        click.echo(
            f"  {m['github']:<{g_w}}  {m['role']:<{r_w}}  "
            f"{m.get('added_at') or '?':<20}  {m.get('added_by') or '-'}",
        )


# ── team config (v0.6.1: client-side multi-team credentials) ────────────────

@team.group("config")
def team_config():
    """Manage client-side team credentials (~/.config/vezir/teams.json).

    v0.6.1+: lets a thin client hold credentials for multiple teams
    and switch between them at runtime (TUI ctrl+t binding, or
    `vezir team config use <id>` from the CLI).  When teams.json
    has an active entry, those credentials WIN over env vars and
    over the legacy client.json url/token.

    The store is at ``~/.config/vezir/teams.json`` (mode 0600).
    """


@team_config.command("add")
@click.option("--id", "team_id", required=True,
              help="Team slug (matches the server-side team id).")
@click.option("--url", required=True,
              help="Server URL for this team (typically the same vezir "
                   "server, just different team slug).")
@click.option("--token", required=True,
              help="Bearer token issued by `vezir token issue --team <id>`.")
@click.option("--label", default=None,
              help="Human display name shown in the TUI title bar "
                   "(defaults to team id).")
@click.option("--activate/--no-activate", "activate", default=False,
              help="Make this the active team immediately.  Implicit "
                   "for the first team added.")
def team_config_add(team_id, url, token, label, activate):
    """Add or update a team in the client-side credentials store."""
    from .client.config import add_team_credentials
    cfg = add_team_credentials(
        team_id, url, token, label=label, activate=activate,
    )
    click.echo(f"team added: id={team_id} url={url} label={label or team_id}")
    if cfg.get("active") == team_id:
        click.echo("  (active)")
    click.echo(
        f"  stored at: ~/.config/vezir/teams.json "
        f"({len(cfg['teams'])} team(s) configured)"
    )


@team_config.command("list")
def team_config_list():
    """List teams configured in the client-side credentials store."""
    from .client.config import load_teams_config
    cfg = load_teams_config()
    teams = cfg["teams"]
    if not teams:
        click.echo("(no teams configured locally)")
        click.echo(
            "Add one with: vezir team config add --id <slug> "
            "--url <https://...> --token <vzr_...>"
        )
        return
    active = cfg.get("active")
    ids = [t["id"] for t in teams]
    labels = [str(t.get("label") or t["id"]) for t in teams]
    urls = [str(t.get("url") or "") for t in teams]
    id_w = max([len("id")] + [len(s) for s in ids])
    lbl_w = max([len("label")] + [len(s) for s in labels])
    url_w = max([len("url")] + [len(s) for s in urls])
    click.echo(
        f"  {'':<2}{'id':<{id_w}}  {'label':<{lbl_w}}  {'url':<{url_w}}"
    )
    for t in teams:
        marker = "* " if t["id"] == active else "  "
        click.echo(
            f"  {marker}{t['id']:<{id_w}}  "
            f"{(t.get('label') or t['id']):<{lbl_w}}  "
            f"{t.get('url') or '':<{url_w}}"
        )
    click.echo()
    click.echo(f"active: {active or '(none)'}")


@team_config.command("use")
@click.argument("team_id")
def team_config_use(team_id):
    """Set the active team in the client-side credentials store."""
    from .client.config import set_active_team
    try:
        cfg = set_active_team(team_id)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    click.echo(f"active team set to: {team_id}")
    click.echo(f"({len(cfg['teams'])} team(s) configured)")


@team_config.command("remove")
@click.argument("team_id")
def team_config_remove(team_id):
    """Remove a team from the client-side credentials store."""
    from .client.config import load_teams_config, remove_team_credentials
    before = load_teams_config()
    if not any(t["id"] == team_id for t in before["teams"]):
        click.echo(f"team {team_id!r} is not configured locally; nothing to remove")
        return
    cfg = remove_team_credentials(team_id)
    click.echo(f"team removed: {team_id}")
    if cfg.get("active"):
        click.echo(f"active is now: {cfg['active']}")
    else:
        click.echo("no teams remaining; active is unset")


# ── login (nostr / NIP-46) ───────────────────────────────────────────────────


@main.command()
@click.option(
    "--url", "url", default=None,
    help="vezir server URL (default: $VEZIR_URL, then the active team's URL).",
)
@click.option(
    "--team", "team_id", default=None,
    help="Team slug to store this session under (default: active team or 'default').",
)
@click.option(
    "--relay", "relay", multiple=True, default=(),
    help="Nostr relay for the NIP-46 handshake. Repeatable; pass multiple "
         "times to fan out (more relays = more reliable delivery of the "
         "signer's responses). Default: vezir's 5-relay set (DEFAULT_RELAYS).",
)
@click.option(
    "--timeout", "timeout", type=int, default=180, show_default=True,
    help="Seconds to wait for you to approve in your signer.",
)
@click.option(
    "--method", "method", type=click.Choice(["nostr", "google"]), default="nostr",
    show_default=True,
    help="Sign-in method: 'nostr' (remote signer / Amber) or 'google' "
         "(@<workspace-domain> account via the device-code flow).",
)
@click.option(
    "--verbose", "verbose", is_flag=True, default=False,
    help="Print NIP-46 handshake debug logs (which relay events arrive, "
         "their encryption scheme, and why any are skipped).",
)
def login(url, team_id, relay, timeout, method, verbose):
    """Log in via nostr (Amber / nsec.app) or Google, and store a session.

    Default (`--method nostr`) generates a ``nostrconnect://`` request
    (URI + QR); approve it in your signer.  `--method google` runs the
    OAuth device-code flow for your ``@blinkbtc.com`` account.  Either way
    vezir stores a short-lived session token; no key/password touches this
    machine.

    Authorize server-side first: `vezir npub add …` (nostr) or
    `vezir google add …` (Google).
    """
    import logging
    import os

    if verbose:
        logging.getLogger("vezir.nip46").setLevel(logging.DEBUG)
        _handler = logging.StreamHandler()
        # Timestamps (ms precision) so a slow login can be attributed to a
        # phase: relay connect, connect-ack wait, get_public_key, sign_event.
        _handler.setFormatter(
            logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s")
        )
        logging.getLogger("vezir.nip46").addHandler(_handler)

    from .client import config as client_config

    # Resolve target URL: explicit flag > env > active team.
    resolved_url = url or os.environ.get("VEZIR_URL")
    resolved_team = team_id
    if not resolved_url or not resolved_team:
        active_team, active_url, _tok = client_config.active_team_credentials()
        resolved_url = resolved_url or active_url
        resolved_team = resolved_team or active_team
    resolved_team = resolved_team or "default"
    if not resolved_url:
        click.echo(
            "error: no server URL; pass --url, set $VEZIR_URL, or configure "
            "a team with `vezir team config add`.",
            err=True,
        )
        sys.exit(2)

    if method == "google":
        _login_google(resolved_url, resolved_team, timeout, client_config)
        return

    try:
        from .client.nostr import login as nostr_login
        from .client.nostr import nip46
    except Exception as exc:  # pragma: no cover - import guard
        click.echo(
            f"error: nostr login support not installed ({exc}); "
            "run `pip install 'vezir[nostr]'`.",
            err=True,
        )
        sys.exit(2)

    def _on_auth_url(u: str) -> None:
        click.echo()
        click.echo(f"  Your signer needs approval — open: {u}")

    client = nip46.Nip46Client(
        relays=list(relay) or None,  # () -> use DEFAULT_RELAYS
        name="vezir",
        on_auth_url=_on_auth_url,
        # App origin for the signer's consent screen and (on signers that
        # support it) the origin-bound sign_event:27235 pre-approval.  Must
        # be the same base login_url_for() derives the NIP-98 u-tag from,
        # so the hosts match by construction.
        url=resolved_url.rstrip("/"),
        image="https://raw.githubusercontent.com/pretyflaco/vezir/main/assets/logo/vezir.png",
    )
    connect_uri = client.build_connect_uri()

    click.echo(f"Logging in to {resolved_url} (team: {resolved_team})")
    click.echo()
    click.echo("Scan this QR or paste the URI into your nostr signer:")
    click.echo()
    try:
        from .server.enroll import render_qr_terminal
        click.echo(render_qr_terminal(connect_uri))
    except Exception:
        pass  # QR is a convenience; the URI below always works.
    click.echo(connect_uri)
    click.echo()
    _clock_warn = _clock_unsynced_warning()
    if _clock_warn:
        click.echo(_clock_warn, err=True)
        click.echo()
    click.echo(f"Waiting up to {timeout}s for you to approve in your signer…")

    try:
        client.wait_for_connection(timeout=timeout)
        template = nostr_login.build_login_event_template(
            nostr_login.login_url_for(resolved_url),
            clock_offset=client.clock_offset,
        )
        signed = client.sign_event(template, timeout=timeout)
    except nip46.Nip46Error as exc:
        click.echo(f"error: {exc}", err=True)
        client.close()
        sys.exit(1)
    finally:
        # Keep the connection only until signing is done.
        pass

    # Honor the client's TLS trust resolution (internal CA support).
    verify = _login_verify()
    try:
        body = nostr_login.post_login(
            resolved_url,
            nostr_login.auth_header_from_event(signed),
            verify=verify,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        client.close()
        sys.exit(1)
    finally:
        client.close()

    client_config.set_team_session(
        resolved_team,
        resolved_url,
        body["session_jwt"],
        body.get("npub", client.user_pubkey or ""),
        label=resolved_team,
        expires_at=_expires_at_from_body(body),
        refresh_token=body.get("refresh_token"),
        refresh_expires_at=_refresh_expires_at_from_body(body),
    )
    click.echo()
    click.echo(f"Logged in as github={body.get('github')} "
               f"(admin={body.get('is_admin')}).")
    click.echo(_session_validity_message(body, resolved_team))
    memberships = body.get("memberships") or []
    if memberships:
        names = ", ".join(m.get("slug") or m.get("team_id") for m in memberships)
        click.echo(f"Team memberships: {names}")


@main.command("logout")
@click.option(
    "--team", "team_id", default=None,
    help="Team to log out of (default: active team).",
)
def logout(team_id):
    """Log out: revoke the current session server-side and clear it locally.

    Best-effort revocation — even if the server is unreachable, the local
    session (access + refresh token) is removed so this machine can no
    longer authenticate without a fresh `vezir login`.
    """
    from .client import config as client_config

    cfg = client_config.load_teams_config()
    target = team_id or cfg.get("active")
    if not target:
        click.echo("No active session to log out of.")
        return

    entry = next((t for t in cfg.get("teams", []) if t["id"] == target), None)
    if entry is None:
        click.echo(f"No local session for team {target!r}.")
        return

    url = entry.get("url")
    token = entry.get("token")
    if url and token:
        try:
            from .client.api import VezirClient
            client = VezirClient(url, token, team_id=entry.get("id"))
            # Hit logout directly; a 401 here just means the access token
            # already lapsed — the server reaps the family on idle anyway.
            client._post("/api/auth/logout")
        except Exception:
            click.echo(
                "warning: could not reach server to revoke session; "
                "clearing locally.", err=True,
            )

    client_config.remove_team_credentials(target)
    click.echo(f"Logged out of team {target!r}.")


def _expires_at_from_body(body: dict) -> float | None:
    """Compute a unix-seconds expiry from a login body's ``expires_in``.

    Stored in teams.json so clients can proactively warn before a session
    (access) JWT expires (0.8.9).  Returns None when the server didn't
    report it.
    """
    import time as _time
    exp_in = body.get("expires_in")
    try:
        return _time.time() + int(exp_in) if exp_in else None
    except (TypeError, ValueError):
        return None


def _refresh_expires_at_from_body(body: dict) -> float | None:
    """Compute a unix-seconds refresh-token idle expiry from a login body.

    Reads ``refresh_expires_in`` (seconds).  Returns None when the server
    didn't report it (e.g. a pre-refresh server).
    """
    import time as _time
    exp_in = body.get("refresh_expires_in")
    try:
        return _time.time() + int(exp_in) if exp_in else None
    except (TypeError, ValueError):
        return None


def _session_validity_message(body: dict, team: str) -> str:
    """Human-readable 'session stored' line reflecting refresh support.

    With a refresh token the session stays alive across access-token
    expiries (silent refresh), so we report the longer refresh window;
    without one we report the access-token lifetime like the pre-refresh
    client did.
    """
    if body.get("refresh_token"):
        days = int(body.get("refresh_expires_in", 0)) // 86400
        if days >= 1:
            return (
                f"Session stored for team '{team}'; stays signed in with "
                f"use (idle re-login after ~{days}d)."
            )
        return f"Session stored for team '{team}'; auto-refreshes with use."
    hours = int(body.get("expires_in", 0)) // 3600
    return f"Session stored for team '{team}'; valid ~{hours}h."


def _login_verify():
    """Resolve httpx ``verify`` the same way VezirClient does.

    Trusts the public/default store AND any configured internal Caddy CA
    (appends, never replaces) so login works against both the public
    Let's Encrypt front and internal ``tls internal`` hosts.
    """
    from .client.trust import resolve_verify
    return resolve_verify()


def _login_google(resolved_url, resolved_team, timeout, client_config) -> None:
    """Google device-code sign-in: prompt, poll, store the session."""
    try:
        from .client import google_login
    except Exception as exc:  # pragma: no cover - import guard
        click.echo(f"error: Google login support unavailable ({exc}).", err=True)
        sys.exit(2)

    verify = _login_verify()
    click.echo(f"Logging in to {resolved_url} (team: {resolved_team}) via Google")
    click.echo()

    def _on_prompt(user_code: str, verification_url: str) -> None:
        click.echo("To sign in, open this URL and enter the code:")
        click.echo()
        click.echo(f"   {verification_url}")
        click.echo(f"   code: {user_code}")
        click.echo()
        click.echo("Waiting for you to approve in the browser…")

    try:
        # Device-grant timeout is generous; let the user finish in a browser.
        body = google_login.login(
            resolved_url,
            verify=verify,
            timeout=max(timeout, 300),
            on_prompt=_on_prompt,
        )
    except google_login.GoogleLoginError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    client_config.set_team_session(
        resolved_team,
        resolved_url,
        body["session_jwt"],
        body.get("email", ""),
        label=resolved_team,
        expires_at=_expires_at_from_body(body),
        refresh_token=body.get("refresh_token"),
        refresh_expires_at=_refresh_expires_at_from_body(body),
    )
    click.echo()
    click.echo(f"Logged in as github={body.get('github')} "
               f"(admin={body.get('is_admin')}) via {body.get('email')}.")
    click.echo(_session_validity_message(body, resolved_team))
    memberships = body.get("memberships") or []
    if memberships:
        names = ", ".join(m.get("slug") or m.get("team_id") for m in memberships)
        click.echo(f"Team memberships: {names}")


def _clock_unsynced_warning() -> str | None:
    """Best-effort: warn if the local clock is NOT NTP-synchronized.

    An unsynced clock that runs behind the signer's makes the signer's
    relay-side ``since`` filter drop our NIP-46 requests, so login hangs
    after connect.  The client self-corrects via the signer's timestamp,
    but a heads-up saves debugging.  Returns a message string or None.
    Linux-only (timedatectl); silently no-op elsewhere.
    """
    import shutil
    import subprocess
    if not shutil.which("timedatectl"):
        return None
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return None
    if out == "no":
        return (
            "warning: your system clock is not NTP-synchronized. If login "
            "stalls after connecting, sync it:  sudo timedatectl set-ntp true"
        )
    return None


# ── session: recording management (v0.6.2+: move / rm / set-title) ────────────
#
# NOTE: these attach to the single `session` group defined earlier (the
# auth-session group with `list` / `revoke`).  There used to be a SECOND
# `@main.group() def session()` here that silently shadowed the first,
# making `vezir session list` / `vezir session revoke` unreachable
# (v0.12.1 fix).  Do not re-introduce a duplicate group — add subcommands
# to the existing one.

@session.command("move")
@click.argument("session_id")
@click.option(
    "--to-team", "to_team", required=True,
    help="Destination team slug.  Must exist (use `vezir team list`).",
)
@click.option(
    "--yes", "confirm", is_flag=True, default=False,
    help="Skip the interactive confirmation prompt.",
)
def session_move(session_id, to_team, confirm):
    """Reassign a session to a different team (v0.6.2+).

    Pure DB-row update: session artifacts on disk are team-agnostic
    (``~/vezir-data/sessions/<id>/`` is keyed by session_id only) so
    nothing needs to move.

    Known limitations (intentional, documented):

    * Voiceprint backwash: any embeddings already trained from
      previously-confirmed labels on this session live in the SOURCE
      team's voiceprint DB.  They stay there; nothing is copied to
      the destination.  A future ``vezir voiceprints reseed`` could
      address this if anyone needs it.
    * Sync backwash: if this session has already been pushed to the
      source team's sync remote, that copy stays in the source repo.
      The next sync (manual or post-edit) will push a copy to the
      destination team's repo, so the session ends up in both.
    """
    from .server import queue as _queue
    row = _queue.get(session_id)
    if not row:
        click.echo(f"error: session {session_id!r} not found", err=True)
        sys.exit(2)
    src_team = row.get("team_id") or "(unassigned)"
    # v0.7.4: jobs store the team uuid; resolve the target slug to its
    # uuid before the same-team comparison.
    dest_uuid = _queue.resolve_team_uuid(to_team)
    if dest_uuid is None:
        available = [t["slug"] for t in _queue.list_teams()]
        click.echo(
            f"error: destination team {to_team!r} does not exist "
            f"(known teams: {', '.join(available) or '(none)'})",
            err=True,
        )
        sys.exit(2)
    if src_team == dest_uuid:
        click.echo(
            f"session {session_id} is already in team {to_team!r}; "
            f"nothing to do"
        )
        return

    click.echo(f"session {session_id}: team {src_team} -> {to_team}")
    if not confirm:
        click.confirm("Proceed?", abort=True)

    try:
        _queue.set_job_team(session_id, to_team)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    click.echo(
        "moved.  Note: source team's voiceprint DB still holds any "
        "embeddings trained from this session's prior labels."
    )


@session.command("rm")
@click.argument("session_id")
@click.option("--server", "server_url", default=None,
              help="Server URL (default $VEZIR_URL)")
@click.option("--token", default=None,
              help="Bearer token (default $VEZIR_TOKEN)")
@click.option("--team", "team", default=None,
              help="Team slug/id the session belongs to (default: active "
                   "team in teams.json or $VEZIR_TEAM_ID)")
@click.option(
    "--yes", "-y", "confirm", is_flag=True, default=False,
    help="Skip the interactive confirmation prompt.",
)
def session_rm(session_id, server_url, token, team, confirm):
    """Permanently remove a session from a team (v0.8.12+).

    Hard delete: removes the session's database row and its on-disk
    artifacts (audio, transcript, summary, PDF) on the server.  Only the
    server-wide admin or the session's original uploader may delete it.

    This is local-only: if the session was already synced to the team's
    git repo, that pushed copy is NOT removed — clean it up in the repo
    manually if needed (the server returns a warning when this applies).
    """
    from .client.api import VezirClient
    from .client.config import (
        resolve_credentials,
        team_credentials,
    )

    team_id: str | None = None
    if team:
        t_id, t_url, t_token = team_credentials(team)
        if t_id is None:
            team_id = team
        else:
            team_id = t_id
            server_url = server_url or t_url
            token = token or t_token
    if server_url is None or token is None or team_id is None:
        r_url, r_token, r_team, _src = resolve_credentials()
        server_url = server_url or r_url
        token = token or r_token
        if team_id is None:
            team_id = r_team
    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        click.echo("vezir: error: VEZIR_TOKEN is not set", err=True)
        sys.exit(1)
    config.validate_token_format(token)
    if not team_id:
        click.echo(
            "vezir: error: no team selected; pass --team <slug>, set "
            "VEZIR_TEAM_ID, or run `vezir login` to populate teams.json",
            err=True,
        )
        sys.exit(1)

    if not confirm:
        click.confirm(
            f"Permanently delete session {session_id}? This cannot be undone.",
            abort=True,
        )

    api = VezirClient(server_url, token, team_id=team_id)
    result = api.delete_session(session_id)
    if not result.is_ok():
        code = result.http_error[0] if result.http_error else None
        if code == 404:
            click.echo(
                f"vezir: error: session {session_id} not found in this team",
                err=True,
            )
        elif code == 403:
            click.echo(
                "vezir: error: not permitted — only an admin or the original "
                "uploader can delete this session",
                err=True,
            )
        else:
            click.echo(f"vezir: error: {result.error_message()}", err=True)
        sys.exit(1)

    click.echo(f"vezir: deleted session {session_id}")
    payload = result.ok
    if isinstance(payload, dict) and payload.get("warning"):
        click.echo(f"vezir: warning: {payload['warning']}")


@session.command("set-title")
@click.argument("session_id")
@click.argument("title")
@click.option("--server", "server_url", default=None,
              help="Server URL (default $VEZIR_URL)")
@click.option("--token", default=None,
              help="Bearer token (default $VEZIR_TOKEN)")
@click.option("--team", "team", default=None,
              help="Team slug/id the session belongs to (default: active "
                   "team in teams.json or $VEZIR_TEAM_ID)")
def session_set_title(session_id, title, server_url, token, team):
    """Add or change a session's title after it was recorded (v0.12.0+).

    Useful when a scribe forgot to name a session at record time.  Only
    the server-wide admin or the session's original uploader may retitle
    it.  Pass an empty string ("") to clear the title.

    The title is not baked into the transcript/summary/PDF, so nothing is
    regenerated.  If the session was already synced, the pushed git folder
    is not renamed automatically — re-run sync to repropagate (the server
    returns a warning when this applies).
    """
    from .client.api import VezirClient
    from .client.config import (
        resolve_credentials,
        team_credentials,
    )

    team_id: str | None = None
    if team:
        t_id, t_url, t_token = team_credentials(team)
        if t_id is None:
            team_id = team
        else:
            team_id = t_id
            server_url = server_url or t_url
            token = token or t_token
    if server_url is None or token is None or team_id is None:
        r_url, r_token, r_team, _src = resolve_credentials()
        server_url = server_url or r_url
        token = token or r_token
        if team_id is None:
            team_id = r_team
    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        click.echo("vezir: error: VEZIR_TOKEN is not set", err=True)
        sys.exit(1)
    config.validate_token_format(token)
    if not team_id:
        click.echo(
            "vezir: error: no team selected; pass --team <slug>, set "
            "VEZIR_TEAM_ID, or run `vezir login` to populate teams.json",
            err=True,
        )
        sys.exit(1)

    api = VezirClient(server_url, token, team_id=team_id)
    result = api.set_title(session_id, title)
    if not result.is_ok():
        code = result.http_error[0] if result.http_error else None
        if code == 404:
            click.echo(
                f"vezir: error: session {session_id} not found in this team",
                err=True,
            )
        elif code == 403:
            click.echo(
                "vezir: error: not permitted — only an admin or the original "
                "uploader can retitle this session",
                err=True,
            )
        else:
            click.echo(f"vezir: error: {result.error_message()}", err=True)
        sys.exit(1)

    payload = result.ok
    new_title = payload.get("title") if isinstance(payload, dict) else None
    if new_title:
        click.echo(f"vezir: session {session_id} titled {new_title!r}")
    else:
        click.echo(f"vezir: session {session_id} title cleared")
    if isinstance(payload, dict) and payload.get("warning"):
        click.echo(f"vezir: warning: {payload['warning']}")


# ── voiceprints ───────────────────────────────────────────────────────────────

@main.group()
def voiceprints():
    """Manage per-team voiceprint databases (v0.6.2+)."""


def _resolve_team_arg(team_id: str | None) -> str:
    """Resolve --team for CLI commands that need a per-team scope.

    v0.6.2+: every per-team CLI command requires a team.  When the
    server has exactly one team, default to it for convenience (the
    same UX pattern v0.6.0 established for ``vezir token issue``).
    Otherwise fail with a clear list of valid options.
    """
    from .server import queue as _queue
    if team_id:
        # v0.7.4: accept slug or uuid; return the stable uuid so path
        # helpers resolve the correct teams/<uuid>/ dir.
        uuid = _queue.resolve_team_uuid(team_id)
        if uuid is None:
            available = [t["slug"] for t in _queue.list_teams()]
            click.echo(
                f"error: team {team_id!r} not found "
                f"(known teams: {', '.join(available) or '(none)'})",
                err=True,
            )
            sys.exit(2)
        return uuid
    teams = _queue.list_teams()
    if len(teams) == 1:
        return teams[0]["id"]
    if not teams:
        click.echo(
            "error: no teams configured; run `vezir team create --id <slug> "
            "--name <name>` first",
            err=True,
        )
        sys.exit(2)
    available = ", ".join(t["slug"] for t in teams)
    click.echo(
        f"error: --team is required when multiple teams exist "
        f"(choose one of: {available})",
        err=True,
    )
    sys.exit(2)


@voiceprints.command("seed")
@click.option(
    "--from", "source", required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to an existing millet speaker_profiles.json to copy in",
)
@click.option(
    "--team", "team_id", default=None,
    help="Team slug to seed.  Required when more than one team exists; "
         "defaults to the only team otherwise.",
)
@click.option(
    "--merge", is_flag=True, default=False,
    help="Merge into the existing team DB instead of refusing when it is populated. "
    "Per-name policy: the profile with the higher n_sessions wins.",
)
def voiceprints_seed(source, team_id, merge):
    """Seed or merge a team's voiceprint DB from an existing millet profile file."""
    from .server import voiceprints as vp_mod
    team_id = _resolve_team_arg(team_id)
    stats = vp_mod.seed_from(source, team_id, merge=merge)
    click.echo(
        f"Done: {stats['added']} added, {stats['updated']} updated, "
        f"{stats['kept']} kept (source had fewer sessions). "
        f"Team {team_id!r} DB now has {stats['total']} profile(s) at "
        f"{config.team_speaker_profiles_path(team_id)}"
    )


@voiceprints.command("list")
@click.option(
    "--team", "team_id", default=None,
    help="Team slug to list.  Required when more than one team exists; "
         "defaults to the only team otherwise.",
)
def voiceprints_list(team_id):
    """List names enrolled in a team's voiceprint DB."""
    from .server import voiceprints as vp_mod
    team_id = _resolve_team_arg(team_id)
    names = vp_mod.list_known_names(team_id)
    if not names:
        click.echo(f"(no voiceprints for team {team_id!r})")
        return
    click.echo(f"voiceprints in team {team_id!r}:")
    for n in names:
        click.echo(f"  {n}")


# ── relabel ───────────────────────────────────────────────────────────────────

@main.command()
@click.option(
    "--team", "team_id", default=None,
    help="Team slug/uuid to scope to.  Required when more than one team "
         "exists; defaults to the only team otherwise.",
)
@click.option(
    "--session", "session_ids", multiple=True,
    help="Re-label this specific session id.  Repeatable.  Mutually "
         "exclusive with --all-needs-labeling.",
)
@click.option(
    "--all-needs-labeling", "all_needs", is_flag=True, default=False,
    help="Re-label every session in the team currently in needs_labeling.",
)
@click.option(
    "--sync/--no-sync", "do_sync", default=False,
    help="When a session fully resolves, sync it to the team repo.  Default "
         "--no-sync: update labels/artifacts/status only.",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Show which sessions would be re-labeled, then exit without changes.",
)
def relabel(team_id, session_ids, all_needs, do_sync, dry_run):
    """Re-run auto-labeling on already-transcribed sessions.

    Use after (re)seeding a team's voiceprint DB to recover sessions that
    landed in needs_labeling because the DB was empty at processing time.
    Recognized speakers get auto-applied; unrecognized ones stay raw and the
    session remains needs_labeling (now with the known speakers pre-filled).
    """
    from .server import queue as _queue
    from .server import worker

    team_id = _resolve_team_arg(team_id)

    if session_ids and all_needs:
        click.echo(
            "error: pass either --session or --all-needs-labeling, not both",
            err=True,
        )
        sys.exit(2)
    if not session_ids and not all_needs:
        click.echo(
            "error: specify --session <id> (repeatable) or "
            "--all-needs-labeling",
            err=True,
        )
        sys.exit(2)

    # Resolve the target session ids, all scoped to this team.
    targets: list[str] = []
    if all_needs:
        rows = [
            j for j in _queue.list_recent(limit=10000, team_id=team_id)
            if j.get("status") == "needs_labeling"
        ]
        targets = [j["id"] for j in rows]
    else:
        for sid in session_ids:
            row = _queue.get(sid)
            if not row:
                click.echo(f"  skip {sid}: not found", err=True)
                continue
            if (row.get("team_id") or "") != team_id:
                click.echo(
                    f"  skip {sid}: belongs to a different team", err=True,
                )
                continue
            targets.append(sid)

    if not targets:
        click.echo(f"No sessions to re-label for team {team_id!r}.")
        return

    click.echo(
        f"Re-labeling {len(targets)} session(s) for team {team_id!r} "
        f"(sync={'on' if do_sync else 'off'}):"
    )
    if dry_run:
        for sid in targets:
            row = _queue.get(sid) or {}
            click.echo(f"  [dry-run] {sid}  {row.get('title')!r}")
        return

    done = nl = errs = 0
    for sid in targets:
        res = worker.reauto_label_session(sid, sync=do_sync)
        status_ = res.get("status")
        matched = res.get("matched") or []
        unresolved = res.get("unresolved") or []
        err = res.get("error")
        line = (
            f"  {sid}: {status_ or 'error'} | "
            f"matched={len(matched)} unresolved={len(unresolved)}"
        )
        if matched:
            line += f" | names: {', '.join(sorted(set(matched)))}"
        if res.get("synced"):
            line += " | synced"
        if err:
            line += f" | note: {err}"
        click.echo(line)
        if err and status_ is None:
            errs += 1
        elif status_ == "done":
            done += 1
        elif status_ == "needs_labeling":
            nl += 1

    click.echo(
        f"Done: {done} resolved, {nl} still need labeling, {errs} error(s)."
    )


# ── status ────────────────────────────────────────────────────────────────────

@main.command()
def status():
    """Print server-side runtime info (paths, counts)."""
    from .server import queue
    click.echo(f"vezir version: {__version__}")
    click.echo(f"data dir:      {config.data_dir()}")
    click.echo(f"sessions dir:  {config.sessions_dir()}")
    click.echo(f"teams dir:     {config.teams_dir()}  (per-team voiceprint DBs)")
    click.echo(f"queue DB:      {config.queue_db_path()}")
    # v0.6.0: status is an admin-side overview, so it uses the global
    # path (no team scope, no viewer filter) — see queue.list_recent
    # docstring.  Per-team breakdown follows below.
    rows = queue.list_recent(limit=200)
    by_status: dict[str, int] = {}
    by_team: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        t = r.get("team_id") or "(unassigned)"
        by_team[t] = by_team.get(t, 0) + 1
    click.echo(f"recent jobs ({len(rows)} of last 200):")
    for k, v in sorted(by_status.items()):
        click.echo(f"  status={k}: {v}")
    click.echo("by team:")
    for k, v in sorted(by_team.items()):
        click.echo(f"  team={k}: {v}")
    click.echo("teams configured:")
    teams = queue.list_teams()
    if not teams:
        click.echo("  (none — run `vezir team create`)")
    for t in teams:
        click.echo(f"  {t['id']:<16} {t['name']}")


# ── doctor ────────────────────────────────────────────────────────────────────

@main.command()
def doctor():
    """Diagnose configuration and environment issues.

    Checks credential resolution, SSL certs, server connectivity, token
    validity, file permissions, migration status, and more.  Client checks
    always run; server checks run only when local vezir-data is detected.

    Exit code: 0 if no errors, 1 if any [ERROR] found.

    \b
    Example:
        vezir doctor
    """
    from .doctor import run_doctor
    sys.exit(run_doctor())


# ── pull (v0.7.0: team meeting artifact sharing) ─────────────────────────────


@main.command("pull")
@click.option(
    "--server", "server_url", default=None,
    help="Server URL (default $VEZIR_URL / teams.json).",
)
@click.option(
    "--token", default=None,
    help="Bearer token (default $VEZIR_TOKEN / teams.json).",
)
@click.option(
    "--limit", default=50, show_default=True,
    help="Max sessions to fetch.",
)
@click.option(
    "--since", default=None,
    help="ISO date or datetime, e.g. 2026-05-20 or 2026-05-20T14:00:00Z.",
)
@click.option(
    "--session", "session_id", default=None,
    help="Pull a single session by ID.",
)
@click.option(
    "-o", "--output-dir", default=None, type=click.Path(),
    help="Output directory (default ~/vezir-meetings/<team>/).",
)
def pull_cmd(server_url, token, limit, since, session_id, output_dir):
    """Download meeting artifacts from the vezir server.

    Pulls summaries, transcripts, and PDFs for team meetings into
    ~/vezir-meetings/<team>/.  Idempotent: already-pulled sessions
    are skipped on re-runs.

    \b
    Examples:
        vezir pull                       # pull recent team meetings
        vezir pull --since 2026-05-20    # pull since a date
        vezir pull --session 01KSG...    # pull a specific session
        vezir pull --limit 200           # pull more history
    """
    from .client.api import VezirClient
    from .client.pull import pull_team_sessions

    server_url = server_url or config.server_url()
    token = token or config.client_token()
    team_id = config.client_team_id()
    if not token:
        click.echo(
            "vezir pull: error: no token configured. "
            "Set VEZIR_TOKEN or run `vezir team config add`.",
            err=True,
        )
        sys.exit(1)
    if not team_id:
        click.echo(
            "vezir pull: error: no team_id configured. "
            "Set VEZIR_TEAM_ID or run `vezir team config use <id>`.",
            err=True,
        )
        sys.exit(1)
    config.validate_token_format(token)

    api = VezirClient(server_url, token, team_id=team_id)
    out = Path(output_dir) if output_dir else None

    try:
        pulled = pull_team_sessions(
            api,
            output_dir=out,
            limit=limit,
            since=since,
            session_id=session_id,
        )
    except KeyboardInterrupt:
        click.echo("vezir pull: interrupted", err=True)
        sys.exit(130)
    except Exception as exc:
        click.echo(f"vezir pull: error: {exc}", err=True)
        sys.exit(1)

    sys.exit(0 if pulled >= 0 else 1)


if __name__ == "__main__":
    main()
