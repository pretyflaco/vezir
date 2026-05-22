# Connecting to Vezir with nostr-vpn

nostr-vpn (`nvpn`) is a decentralized mesh VPN that uses Nostr keys for
identity.  No accounts, no sign-ups, no fees.  It replaces Tailscale as
the network layer between scribe laptops and the vezir server.

## How it works

1. The vezir server runs `nvpn` and publishes an **invite link**.
2. You install `nvpn`, import the invite, and start the daemon.
3. Your machine joins the mesh -- a direct encrypted tunnel forms
   automatically (NAT traversal included, no port forwarding needed).
4. You point `VEZIR_URL` at the server's tunnel IP and use vezir
   normally.

## Two secrets -- don't mix them up

You'll receive two different secrets from the admin.  They serve
different purposes and go in different places:

| Secret | Looks like | Purpose | Where it goes |
|---|---|---|---|
| nvpn invite | `nvpn://invite/eyJ2...` | Joins the VPN mesh | `nvpn import-invite` (or `open` on macOS) |
| Vezir token | `vzr_Ab3x...` (47 chars) | Authenticates uploads | `VEZIR_TOKEN` env var |

Do not paste the invite into `VEZIR_TOKEN` or vice versa.

## Current participants

The live participant list is in `~/.config/nvpn/config.toml` under the
`participants = [...]` key (and is mirrored to `/root/.config/nvpn/config.toml`
for the system daemon).  A human-readable roster mapping npubs to names
and devices is maintained out-of-tree by the admin at
`~/vezir-data/nvpn-peers.md`.

To join, send your npub (printed by `nvpn init`) to the admin and they
will run `nvpn add-participant --participant <your-npub>`.

---

## Linux setup

### Step 1 -- Install nvpn

```bash
curl -fsSL "https://github.com/mmalmi/nostr-vpn/releases/latest/download/nvpn-x86_64-unknown-linux-musl.tar.gz" \
  | tar -xz -C /tmp
cd /tmp/nvpn && ./install.sh ~/.local/bin
```

For arm64, replace `x86_64` with `aarch64` in the URL above.

Verify:

```bash
nvpn version
```

### Step 2 -- Initialize

```bash
nvpn init
```

Note your `nostr_pubkey` (npub) from the output and send it to the
admin so they can approve your join request.

### Step 3 -- Import the invite

```bash
nvpn import-invite 'nvpn://invite/...'
```

You should see `invite_imported` and `join_request_queued=true`.

### Step 4 -- Install the system service

```bash
sudo "$(which nvpn)" service install
```

If `sudo` can't find `nvpn`, use the full path:
`sudo ~/.local/bin/nvpn service install`

**Important:** the service reads its config from `/root/.config/nvpn/`.
Copy your user config there so the daemon picks it up:

```bash
sudo mkdir -p /root/.config/nvpn
sudo cp ~/.config/nvpn/config.toml /root/.config/nvpn/config.toml
sudo systemctl restart nvpn
```

> Every time you change your nvpn config (e.g. import a new invite),
> repeat the `sudo cp` + `sudo systemctl restart nvpn` step.

> Do **not** use `nvpn start --daemon --connect` -- always start via
> the systemd service so the daemon runs as root and can create the
> tunnel interface.

Check that the service is running:

```bash
sudo systemctl status nvpn
```

### Step 5 -- Verify the tunnel

```bash
ip addr show | grep -A3 utun
# Should show a utun100 interface with a 10.44.x.x address
```

Test connectivity to the vezir server (ask the admin for the tunnel IP):

```bash
curl -sS http://<SERVER_TUNNEL_IP>:8000/health
# Expected: {"status":"ok","version":"...","data_dir":"..."}
```

### Step 6 -- Install/upgrade vezir and configure

```bash
pip install --user --upgrade vezir
vezir --version
```

Add to your `~/.bashrc`:

```bash
export VEZIR_URL=http://<SERVER_TUNNEL_IP>:8000
export VEZIR_TOKEN=<your-vzr-token>
```

Then reload: `source ~/.bashrc`

### Step 7 -- Test

```bash
vezir scribe --title "test recording"
# Speak for a few seconds, then Ctrl+C
# Should compress, upload, and show processing status
```

---

## macOS (Apple Silicon) setup

### Step 1 -- Install nvpn

**Option A: Native app (recommended)** -- download the `.dmg` from
https://github.com/mmalmi/nostr-vpn/releases/latest and install
normally.  The native app has a tray icon and manages the daemon
automatically.

**Option B: CLI only**

```bash
curl -fsSL "https://github.com/mmalmi/nostr-vpn/releases/latest/download/nvpn-aarch64-apple-darwin.tar.gz" \
  | tar -xz -C /tmp
cd /tmp/nvpn && ./install.sh
```

Verify:

```bash
nvpn version
```

### Step 2 -- Initialize

```bash
nvpn init
```

Note your `nostr_pubkey` (npub) from the output and send it to the
admin.

### Step 3 -- Import the invite

**If using the native app:** run this from terminal -- it triggers the
app's URL-scheme handler:

```bash
open 'nvpn://invite/...'
```

> Do **not** paste the invite into the "Add network" field in the app.
> That creates a new empty local network instead of importing the
> invite.

**If using CLI only:**

```bash
nvpn import-invite 'nvpn://invite/...'
```

### Step 4 -- Start the service

**If using the native app:** the app manages the daemon automatically.
Skip to Step 5.

**If using CLI only:**

```bash
sudo nvpn service install
```

The service reads config from root's home.  Copy your user config:

```bash
sudo mkdir -p "/var/root/Library/Application Support/nvpn"
sudo cp ~/Library/Application\ Support/nvpn/config.toml \
  "/var/root/Library/Application Support/nvpn/config.toml"
```

Start the service:

```bash
sudo launchctl kickstart -k system/nvpn
```

> Every time you change your nvpn config (e.g. import a new invite),
> repeat the `sudo cp` + kickstart step.

> Do **not** use `nvpn start --daemon --connect` -- always use the
> system service so the daemon runs with the privileges it needs.

### Step 5 -- Verify the tunnel

```bash
ifconfig | grep -A5 utun
# Should show a utun interface with a 10.44.x.x address
```

Test connectivity to the vezir server:

```bash
curl -sS http://<SERVER_TUNNEL_IP>:8000/health
# Expected: {"status":"ok","version":"...","data_dir":"..."}
```

### Step 6 -- Install/upgrade vezir and configure

```bash
pip3 install --user --upgrade vezir
vezir --version
```

Add to your `~/.zshrc`:

```bash
export VEZIR_URL=http://<SERVER_TUNNEL_IP>:8000
export VEZIR_TOKEN=<your-vzr-token>
```

Then reload: `source ~/.zshrc`

### Step 7 -- Test

```bash
vezir scribe --title "test recording"
# Speak for a few seconds, then Ctrl+C
# Should compress, upload, and show processing status
```

---

## Android (vezir-android thin client)

The Android client bundles its own nvpn integration -- no separate
`nvpn` install required on the phone.  Sideload the APK from the
[vezir-android releases page](https://github.com/pretyflaco/vezir-android/releases/latest),
generate keys in-app, then send the displayed npub to the admin to be
added with `nvpn add-participant`.  Token, preset, and privacy
opt-outs are configured via the in-app UI rather than env vars or CLI
flags.

End-to-end validated 2026-05-22 on vezir-android 0.1.4 against muscle
running vezir 0.1.11.  See the
[wiki onboarding page](https://github.com/blinkbitcoin/blink-wip/wiki/pretyflaco----2026-05-21-Vezir-Onboarding-with-nostr-vpn)
for the full UI walkthrough.

---

## Updating nvpn

### Linux

```bash
curl -fsSL "https://github.com/mmalmi/nostr-vpn/releases/latest/download/nvpn-x86_64-unknown-linux-musl.tar.gz" \
  | tar -xz -C /tmp
cd /tmp/nvpn && ./install.sh ~/.local/bin
sudo cp ~/.local/bin/nvpn /usr/local/bin/   # update the service binary too
sudo systemctl restart nvpn
```

### macOS

If using the native app, check for updates via the app's built-in
updater.  For CLI-only:

```bash
curl -fsSL "https://github.com/mmalmi/nostr-vpn/releases/latest/download/nvpn-aarch64-apple-darwin.tar.gz" \
  | tar -xz -C /tmp
cd /tmp/nvpn && ./install.sh
sudo launchctl kickstart -k system/nvpn
```

---

## Troubleshooting

### `nvpn: command not found` after install

**Linux:** make sure `~/.local/bin` is in your PATH:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

**macOS:** the installer defaults to `/opt/homebrew/bin` or
`/usr/local/bin`, both usually in PATH already.

### `sudo nvpn: command not found`

sudo resets PATH.  Use the full path: `sudo ~/.local/bin/nvpn ...`
or copy the binary: `sudo cp ~/.local/bin/nvpn /usr/local/bin/`

### Daemon fails with "Operation not permitted"

The daemon is not running as root.  Start it via the system service,
not `nvpn start --daemon`.

### `nvpn status` shows `daemon: stopped` but the service shows active

Known nvpn issue: the CLI reads from the user config, not the running
daemon socket.  Trust `sudo systemctl status nvpn` (Linux) or
`sudo launchctl list | grep nvpn` (macOS) for the real state.

### No peers / tunnel not connecting

1. Confirm the admin has approved your npub:
   `nvpn add-participant --participant <your-npub>` (admin runs this)
2. Confirm you copied the config to root (see Step 4 for your platform)
3. Check logs:
   - Linux: `sudo journalctl -u nvpn --no-pager -n 50`
   - macOS: `log show --predicate 'process == "nvpn"' --last 5m`

### curl to server tunnel IP hangs

- Verify the tunnel interface exists: `ip addr show | grep utun`
  (Linux) or `ifconfig | grep utun` (macOS)
- Verify the server's nvpn daemon is running (ask the admin)
- Wait 30-60 seconds -- Nostr relay discovery can be slow on first
  connection

### Vezir upload returns 401 "invalid bearer token"

Check your token for copy-paste artifacts:

```bash
echo "len=${#VEZIR_TOKEN}"   # expected: 47
echo "$VEZIR_TOKEN" | cat -A  # look for trailing ^\ or whitespace
```

Common causes: trailing backslash from line-wrap, extra whitespace,
or accidentally using the nvpn invite string instead of the `vzr_`
token.  Vezir >= 0.1.6 warns about these automatically.

---

## Removing nvpn

### Linux

```bash
sudo systemctl stop nvpn
sudo nvpn service uninstall
sudo rm -rf /root/.config/nvpn
rm -rf ~/.config/nvpn
rm -f ~/.local/bin/nvpn
sudo rm -f /usr/local/bin/nvpn
```

### macOS

If using the native app, drag it to Trash.  For CLI:

```bash
sudo nvpn service uninstall
sudo rm -rf "/var/root/Library/Application Support/nvpn"
rm -rf ~/Library/Application\ Support/nvpn
rm -f /usr/local/bin/nvpn /opt/homebrew/bin/nvpn
```
