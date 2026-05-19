# Connecting to Vezir with nostr-vpn

nostr-vpn (`nvpn`) is a decentralized mesh VPN that uses Nostr keys for
identity.  No accounts, no sign-ups, no fees.  It replaces Tailscale as
the network layer between scribe laptops and the vezir server.

## How it works

1. The vezir server runs `nvpn` and publishes an **invite link**.
2. You install `nvpn`, import the invite, and start the daemon.
3. Your machine joins the mesh — a direct encrypted tunnel forms
   automatically (NAT traversal included, no port forwarding needed).
4. You point `VEZIR_URL` at the server's tunnel IP and use vezir
   normally.

## Prerequisites

- Linux x86_64 or arm64, macOS Apple Silicon, or Windows x64
- `~/.local/bin` in your `PATH` (Linux; check with `echo $PATH`)
- `sudo` access (the daemon needs to create a tunnel interface)
- An **invite string** from the vezir admin (starts with `nvpn://invite/...`)
- A **vezir bearer token** from the admin (`vzr_...`)

## Step 1 — Install nvpn

### Linux

```bash
curl -fsSL "https://github.com/mmalmi/nostr-vpn/releases/latest/download/nvpn-x86_64-unknown-linux-musl.tar.gz" \
  | tar -xz -C /tmp
cd /tmp/nvpn && ./install.sh ~/.local/bin
```

For arm64 replace `x86_64` with `aarch64` in the URL above.

### macOS (Apple Silicon)

```bash
curl -fsSL "https://github.com/mmalmi/nostr-vpn/releases/latest/download/nvpn-aarch64-apple-darwin.tar.gz" \
  | tar -xz -C /tmp
cd /tmp/nvpn && ./install.sh
```

Or install via crates.io if you have Rust: `cargo install nvpn`.

### Verify

```bash
nvpn version
# Expected: 4.x.x
```

## Step 2 — Initialize

```bash
nvpn init
```

This creates `~/.config/nvpn/config.toml` and generates a Nostr
keypair.  Note your `nostr_pubkey` (npub) — send it to the admin so
they can approve your join request.

## Step 3 — Import the invite

Paste the invite string you received from the admin:

```bash
nvpn import-invite 'nvpn://invite/...'
```

You should see `invite_imported` and `join_request_queued=true`.

## Step 4 — Install the system service and start

The daemon needs root to create the tunnel interface.  Install it as a
system service, then start:

```bash
sudo "$(which nvpn)" service install
```

**Important:** the service reads its config from `/root/.config/nvpn/`.
Copy your user config there so the daemon picks it up:

```bash
sudo mkdir -p /root/.config/nvpn
sudo cp ~/.config/nvpn/config.toml /root/.config/nvpn/config.toml
sudo systemctl restart nvpn
```

> Every time you change your nvpn config (e.g. import a new invite),
> repeat the `sudo cp` + `sudo systemctl restart nvpn` step.

Check that the service is running:

```bash
sudo systemctl status nvpn
```

## Step 5 — Verify the tunnel

```bash
ip addr show | grep -A3 utun
# Should show a utun100 interface with a 10.44.x.x address
```

Test connectivity to the vezir server (ask the admin for the server's
tunnel IP):

```bash
curl -sS http://<SERVER_TUNNEL_IP>:8000/health
# Expected: {"status":"ok","version":"...","data_dir":"..."}
```

## Step 6 — Configure vezir

Add these to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
export VEZIR_URL=http://<SERVER_TUNNEL_IP>:8000
export VEZIR_TOKEN=<your-token>
```

Then reload: `source ~/.bashrc` (or open a new terminal).

## Step 7 — Test

```bash
vezir scribe --title "test recording"
# Speak for a few seconds, then Ctrl+C
# Should compress, upload, and show processing status
```

## Updating nvpn

```bash
curl -fsSL "https://github.com/mmalmi/nostr-vpn/releases/latest/download/nvpn-x86_64-unknown-linux-musl.tar.gz" \
  | tar -xz -C /tmp
cd /tmp/nvpn && ./install.sh ~/.local/bin
sudo cp ~/.local/bin/nvpn /usr/local/bin/   # update the service binary too
sudo systemctl restart nvpn
```

## Troubleshooting

### `nvpn: command not found` after install

Make sure `~/.local/bin` is in your PATH:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### `sudo nvpn: command not found`

sudo resets PATH.  Use the full path: `sudo ~/.local/bin/nvpn ...`
or copy the binary: `sudo cp ~/.local/bin/nvpn /usr/local/bin/`

### Daemon fails with "Operation not permitted"

The daemon is not running as root.  Make sure you started it via
`sudo systemctl start nvpn`, not `nvpn start --daemon`.

### `nvpn status` shows `daemon: stopped` but `systemctl` shows active

This is a known nvpn issue: the CLI reads status from the user config,
not from the running daemon.  Trust `sudo systemctl status nvpn` for
the real state.

### No peers / tunnel not connecting

1. Confirm the admin has approved your npub:
   `nvpn add-participant --participant <your-npub>` (admin runs this)
2. Confirm you copied the config to root:
   `sudo cat /root/.config/nvpn/config.toml` should show your network
3. Check logs: `sudo journalctl -u nvpn --no-pager -n 50`

### curl to server tunnel IP hangs

- Verify the tunnel interface exists: `ip addr show | grep utun`
- Verify the server's nvpn daemon is running (ask the admin)
- Try waiting 30-60 seconds — Nostr relay discovery can be slow on
  first connection

## Removing nvpn

```bash
sudo systemctl stop nvpn
sudo nvpn service uninstall   # or: sudo rm /etc/systemd/system/nvpn.service
sudo rm -rf /root/.config/nvpn
rm -rf ~/.config/nvpn
rm ~/.local/bin/nvpn
sudo rm -f /usr/local/bin/nvpn
```
