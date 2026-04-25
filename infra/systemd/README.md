# Systemd user unit

Run vezir as a user-level service that survives reboots and SSH
disconnects.

## Install

```bash
mkdir -p ~/.config/systemd/user
cp infra/systemd/vezir.service ~/.config/systemd/user/
```

## Secrets / env file

Vezir's worker shells out to `meet` which needs `HF_TOKEN` (for
diarization) and optionally `OPENROUTER_API_KEY` /
`MEETSCRIBE_SUMMARY_BACKEND` (for cloud summaries). Systemd `--user`
services do **not** source `~/.profile` or `~/.bashrc`, so put them in
an EnvironmentFile:

```bash
mkdir -p ~/.config/environment.d
chmod 700 ~/.config/environment.d

# Copy values from your existing shell env without leaking them to the
# terminal:
bash -c '
  set -a; source ~/.profile; set +a
  {
    printf "HF_TOKEN=%s\n"                  "$HF_TOKEN"
    printf "OPENROUTER_API_KEY=%s\n"        "$OPENROUTER_API_KEY"
    printf "MEETSCRIBE_SUMMARY_BACKEND=%s\n" "$MEETSCRIBE_SUMMARY_BACKEND"
  } > ~/.config/environment.d/vezir.conf
'
chmod 600 ~/.config/environment.d/vezir.conf
```

Format is strict: `VAR=VALUE`, no `export`, no quoting, no comments.

## Linger (run without active login session)

Required if the box is headless and you want vezir up over reboots
without an SSH session:

```bash
loginctl enable-linger $USER
loginctl show-user $USER | grep Linger   # should show Linger=yes
```

## Start

```bash
systemctl --user daemon-reload
systemctl --user enable --now vezir.service
systemctl --user is-active vezir.service   # -> active

# Tail the journal
journalctl --user -u vezir.service -f
```

## Verify reachability

From the same host:

```bash
curl -sS http://127.0.0.1:8000/health
```

From another Tailscale peer (replace with your tailnet name):

```bash
curl -sS http://muscle.tail178bd.ts.net:8000/health
```

## Common operations

```bash
systemctl --user restart vezir.service
systemctl --user stop    vezir.service
systemctl --user disable vezir.service        # stop + remove from autostart
journalctl --user -u vezir.service -n 100 --no-pager
```
