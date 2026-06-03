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
@click.argument("record_args", nargs=-1, type=click.UNPROCESSED)
def scribe(server_url, token, title, output_dir, compress, wait, wait_timeout,
           open_labeling, preset, auto_label, sync, personal, record_args):
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

    if wait:
        from .client.scribe import poll_status
        click.echo("vezir: waiting for processing ...")
        poll_status(server_url, token, result["session_id"], timeout=float(wait_timeout))


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


# ── session (v0.6.2+: cross-team move) ───────────────────────────────────────

@main.group()
def session():
    """Manage individual sessions (v0.6.2+)."""


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
