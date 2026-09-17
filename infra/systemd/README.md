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

From another VPN peer (replace with your network's hostname/IP):

```bash
# Tailscale
curl -sS http://muscle.tail178bd.ts.net:8000/health

# nostr-vpn (use the server's tunnel IP from `ip addr show utun100`)
curl -sS http://<nvpn-tunnel-ip>:8000/health
```

See `infra/nvpn/README.md` for nostr-vpn setup instructions.

## Common operations

```bash
systemctl --user restart vezir.service
systemctl --user stop    vezir.service
systemctl --user disable vezir.service        # stop + remove from autostart
journalctl --user -u vezir.service -n 100 --no-pager
```

## Health watchdog (optional, recommended)

`vezir-health` probes two very different things every 90 seconds and
responds in two very different ways:

| Probe | Failure response |
|---|---|
| `http://127.0.0.1:8000/health` (loopback) | restart vezir — a hung/crashed process is what a restart fixes |
| `https://vezir.twentyone.ist/health` (public) | **alert only** — journal entries under the `vezir-healthcheck` tag, never a restart |

The public path is VPS nftables DNAT → WireGuard tunnel → caddy →
uvicorn, and restarting vezir cannot repair any of that.  Incident
2026-09-17: a modem reboot killed the WG tunnel for ~30 minutes while
the loopback probe read green the whole time, so both clients saw
"transient server connection error" with nothing in the watchdog's
journal.  The public probe exists to make that class of outage visible:
it logs DOWN (with a runbook pointer to `infra/vps/README.md`), a
heartbeat every ~20 checks, and RECOVERED with the outage length.
Consecutive-failure state lives in `~/.local/state/`.

```bash
install -m 0755 infra/systemd/vezir-healthcheck.sh ~/.local/bin/
cp infra/systemd/vezir-health.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vezir-health.timer

# Force one run and read the verdict
systemctl --user start vezir-health.service
journalctl --user -t vezir-healthcheck -n 20 --no-pager
```

Edit `PUBLIC_URL` in the script if the public hostname differs.

## Tinfoil model watchdog (optional, recommended)

Tinfoil retires models out from under deployments.  `deepseek-v4-pro`
went in 2026-07 with notice; `glm-5-2` went in 2026-09 with **none** — it
vanished from the catalog and started answering HTTP 503, which killed
every `confidential` summary (that preset never falls back, by design).

`vezir-model-check` asks Tinfoil's catalog once a day whether the models
millet depends on are still present and not flagged deprecated, and logs
to the journal if either is false.  It only reports — the fix is always a
millet release, since the model names are constants in
`millet/summarize.py`, not env vars.

```bash
install -m 0755 infra/systemd/vezir-model-check.sh ~/.local/bin/
cp infra/systemd/vezir-model-check.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vezir-model-check.timer

# Run it once now and read the verdict
systemctl --user start vezir-model-check.service
journalctl --user -t vezir-model-check -n 20 --no-pager
```

It reads `TINFOIL_API_KEY` from the environment or from
`~/.config/environment.d/vezir.conf`, and exits quietly (status 0) when
no key is configured or the catalog is unreachable — an advisory check
must never become its own outage.  Override the watched list with
`VEZIR_TINFOIL_MODELS="model-a model-b"` if you pin non-default models.
