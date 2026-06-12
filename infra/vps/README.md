# Public access via a VPS front (WireGuard + nftables TLS passthrough)

This directory documents how vezir is exposed to the public internet
**without** giving any client a VPN, and **without** the VPS ever seeing
plaintext traffic.

It supersedes the per-client nvpn/Tailscale onboarding for *reaching*
the server (nvpn still runs in parallel during the transition). Clients
now use ordinary outbound HTTPS to `https://vezir.example.com`, which
works from anywhere — including CGNAT/IPv6-only links like Starlink that
could never bridge to the IPv4-only home server directly.

## Why a VPS at all?

`muscle` (the server) sits behind double-NAT on a residential line with
**no inbound reachability** and **no delegated IPv6 prefix** from the
ISP. It can make outbound connections but nothing can connect *to* it.
A cheap VPS with a real public IP solves this: muscle dials *out* to the
VPS over WireGuard, and the VPS forwards public :443 back down that
tunnel.

## Architecture

```
                      public internet
                            │  HTTPS (TLS to vezir.example.com)
                            ▼
                 ┌──────────────────────┐
                 │  VPS  <VPS_PUBLIC_IP>   │   any small cloud VPS, Ubuntu 24.04
                 │                       │
                 │  nftables DNAT :443   │   L4 forward ONLY — sees ciphertext
                 │        │              │
                 │        ▼              │
                 │  wg0  10.99.0.1 ──────┼───┐ WireGuard (muscle dials out,
                 └──────────────────────┘   │ PersistentKeepalive=25)
                            ▲                │
                  outbound WG (UDP 51820)    │
                            │                ▼
                 ┌──────────────────────────────────┐
                 │  muscle  wg0 10.99.0.2            │  home, double-NAT
                 │                                   │
                 │  Caddy :443  ── TERMINATES TLS    │  real LE cert (Gandi DNS-01)
                 │        │                          │
                 │        ▼                          │
                 │  vezir uvicorn 127.0.0.1:8000     │
                 └──────────────────────────────────┘
```

### Key property: the VPS never sees plaintext

TLS is **terminated on muscle**, not on the VPS. The VPS runs an
nftables DNAT rule that forwards the raw TCP stream of `:443` into the
WireGuard tunnel to `10.99.0.2:443`. Everything the VPS handles is
already-encrypted TLS records. A compromised or subpoenaed VPS yields
ciphertext only — never bearer tokens, session JWTs, audio, or
transcripts. This is the "TLS passthrough" choice (vs. terminating TLS
on the VPS, which would expose plaintext there).

Consequence: the **Let's Encrypt cert lives on muscle**, and because
muscle has no inbound :80/:443, it must use the **DNS-01** challenge
(Gandi plugin). See `infra/caddy/Caddyfile.example`'s
`vezir.example.com` block.

## Inventory / facts

| Thing            | Value                                   |
|------------------|-----------------------------------------|
| VPS provider     | any small cloud VPS (1GB RAM is plenty; ~$3.50/mo)|
| VPS public IP    | `<VPS_PUBLIC_IP>`                         |
| VPS OS           | Ubuntu 24.04                            |
| VPS hostname     | `<vps-hostname>`                           |
| Public hostname  | `vezir.example.com` (DNS at Gandi)    |
| WireGuard subnet | `10.99.0.0/24`                          |
| VPS WG IP        | `10.99.0.1`                             |
| muscle WG IP     | `10.99.0.2`                             |
| WG UDP port      | `51820`                                 |
| Public TCP port  | `443` (HTTPS)                           |
| SSH              | `22`, **key-only** (no passwords)       |

## Files in this directory

| File                     | Where it runs | Purpose |
|--------------------------|---------------|---------|
| `wg-vps.conf.example`    | VPS           | WireGuard interface for the VPS end |
| `wg-muscle.conf.example` | muscle        | WireGuard interface for the muscle end |
| `nftables-dnat.conf`     | VPS           | DNAT `:443` → `10.99.0.2:443` + forward rules |
| `harden-vps.sh`          | VPS           | ufw, fail2ban, unattended-upgrades, sshd key-only |

---

## Step 0 — First login (host-key & key-only access)

The VPS was (re)provisioned, so it presents a **new** SSH host key. If
you previously had a box on this IP, your `known_hosts` still pins the
old key and SSH will refuse to connect (this is expected — not a MITM).

1. **Verify the new host fingerprint out-of-band.** In the provider web
   console (serial/VNC), run:
   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```
   It must match the `SHA256:…` your client shows on first connect.

2. **Drop the stale `known_hosts` entries** on your laptop:
   ```bash
   ssh-keygen -f ~/.ssh/known_hosts -R <VPS_PUBLIC_IP>
   ```

3. **Inject your public key** (the image is key-only; password auth is
   off, which is why you never see a password prompt). From the provider
   web console, log in and paste your laptop's public key:
   ```bash
   mkdir -p ~/.ssh && chmod 700 ~/.ssh
   echo 'ssh-ed25519 AAAA... your-laptop' >> ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```
   (Your laptop pubkey is `~/.ssh/id_ed25519.pub`.)

4. **Connect and accept the verified key:**
   ```bash
   ssh ubuntu@<VPS_PUBLIC_IP>      # or root@, depending on the image
   ```

> If you'd rather use the provider-issued password temporarily: enable
> `PasswordAuthentication yes` in the console, `ssh-copy-id`, then turn
> it back off. The key-only path above avoids ever exposing password
> auth on a public IP.

---

## Step 1 — Harden the VPS

Copy `harden-vps.sh` to the VPS and run it as root. It is idempotent:

```bash
scp infra/vps/harden-vps.sh ubuntu@<VPS_PUBLIC_IP>:/tmp/
ssh ubuntu@<VPS_PUBLIC_IP> 'sudo bash /tmp/harden-vps.sh'
```

It installs `ufw` (allow 22/tcp, 443/tcp, 51820/udp; deny the rest),
`fail2ban` (ssh jail), `unattended-upgrades`, and enforces
`PasswordAuthentication no` + `PermitRootLogin prohibit-password` in
sshd. **Confirm your key login works in a second terminal before closing
your session.**

---

## Step 2 — WireGuard tunnel

Generate keypairs on BOTH ends (never copy private keys around):

```bash
# on the VPS
wg genkey | tee /etc/wireguard/vps.key | wg pubkey > /etc/wireguard/vps.pub
# on muscle
wg genkey | tee /etc/wireguard/muscle.key | wg pubkey > /etc/wireguard/muscle.pub
```

Fill in the templates with the matching **public** keys:

* `wg-vps.conf.example`    → `/etc/wireguard/wg0.conf` on the VPS
* `wg-muscle.conf.example` → `/etc/wireguard/wg0.conf` on muscle

Bring them up:

```bash
# VPS first (it's the listener)
sudo systemctl enable --now wg-quick@wg0
# then muscle (it dials out; PersistentKeepalive keeps the NAT pinhole open)
sudo systemctl enable --now wg-quick@wg0
```

Verify the tunnel:

```bash
# from muscle
ping -c3 10.99.0.1     # VPS WG IP
sudo wg show           # should show a recent handshake + rx/tx counters
```

> muscle is the dialer because it's the one behind NAT. `Endpoint` is set
> only in muscle's config (pointing at the VPS public IP:51820); the VPS
> learns muscle's address from the incoming handshake.
> `PersistentKeepalive=25` on muscle keeps the NAT mapping alive.

---

## Step 3 — nftables DNAT on the VPS

Forward public `:443` into the tunnel. Copy `nftables-dnat.conf` to the
VPS and load it:

```bash
scp infra/vps/nftables-dnat.conf ubuntu@<VPS_PUBLIC_IP>:/tmp/
ssh ubuntu@<VPS_PUBLIC_IP> 'sudo cp /tmp/nftables-dnat.conf /etc/nftables.d/vezir-dnat.conf'
# enable IP forwarding (persisted)
ssh ubuntu@<VPS_PUBLIC_IP> 'echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-vezir-forward.conf && sudo sysctl --system'
ssh ubuntu@<VPS_PUBLIC_IP> 'sudo systemctl enable --now nftables && sudo nft -f /etc/nftables.d/vezir-dnat.conf'
```

This DNATs `tcp dport 443` to `10.99.0.2:443` and masquerades the
return path over `wg0` so muscle's replies route back through the VPS.

> **Gotcha (must do): ufw forward policy.** ufw ships with
> `DEFAULT_FORWARD_POLICY="DROP"`, and its `FORWARD` chain runs at the
> same hook as ours with a `drop` policy — so the DNAT'd packets get
> dropped before they reach `wg0` (symptom: public `:443` connection
> just times out; `tcpdump -ni wg0` shows **0 packets** while
> `tcpdump -ni ens3` shows the SYNs arriving). Fix:
> ```bash
> sudo sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
> sudo ufw reload
> # ufw reload reloads nftables from /etc/nftables.conf, so make sure
> # that file `include`s our DNAT table (see "persist" below), then:
> sudo nft -f /etc/nftables.d/vezir-dnat.conf
> ```

> **Persist across reboot + ufw reloads:** add an include so the table
> is reloaded whenever nftables is (ufw reload triggers this too):
> ```bash
> grep -q vezir-dnat /etc/nftables.conf || \
>   echo 'include "/etc/nftables.d/vezir-dnat.conf"' | sudo tee -a /etc/nftables.conf
> sudo systemctl enable nftables
> sudo nft -c -f /etc/nftables.conf   # validate it parses
> ```

> **provider 1:1 NAT note:** the VM's `ens3` holds a *private* address
> (e.g. `<VPS_PRIVATE_IP>`); the public `<VPS_PUBLIC_IP>` is 1:1-NATed to it by
> the provider. Inbound `:443` therefore arrives on `ens3` with the
> private dest — the `iifname "ens3" tcp dport 443` match handles this
> (it doesn't pin the destination IP). Confirm your public iface name
> with `ip -br link` (it's `ens3` on this image, not `eth0`).

---

## Step 4 — DNS + public cert (Gandi DNS-01)

1. **A record:** in the Gandi LiveDNS panel for `example.com`, add
   `vezir A <VPS_PUBLIC_IP>` (the VPS public IP).

2. **Caddy with the Gandi plugin** on muscle. We use the **DNS-01**
   challenge: the public hostname resolves to the VPS, but TLS terminates
   on muscle, and DNS-01 proves domain control via the Gandi API
   independent of the data path (so a WireGuard/DNAT outage never blocks
   a cert renewal).

   > muscle's egress filter blocks `proxy.golang.org`, so the xcaddy
   > build must fetch modules straight from GitHub via `GOPROXY=direct`
   > (GitHub IS reachable). `GOSUMDB=off` skips the (also-blocked)
   > checksum DB; modules come from a trusted source over TLS.

   ```bash
   # one-time: install xcaddy (also via direct proxy)
   GOPROXY=direct GOSUMDB=off \
     go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest

   # build Caddy with the Gandi DNS provider
   GOPROXY=direct GOSUMDB=off \
     ~/.local/go/bin/xcaddy build --with github.com/caddy-dns/gandi

   sudo install -m0755 ./caddy /usr/bin/caddy
   ```

3. **Gandi token** (PAT with DNS edit rights on example.com), kept out
   of the Caddyfile:
   ```bash
   sudo install -m600 /dev/stdin /etc/caddy/gandi.env <<'EOF'
   GANDI_API_TOKEN=pat-xxxxxxxx
   EOF
   # systemd drop-in so Caddy sees it
   sudo systemctl edit caddy   # add: [Service]\nEnvironmentFile=/etc/caddy/gandi.env
   ```

4. **Add the public listener** from `infra/caddy/Caddyfile.example`
   (the `vezir.example.com { ... }` block) to `/etc/caddy/Caddyfile`,
   then reload:
   ```bash
   sudo systemctl reload caddy
   ```
   Caddy will solve the DNS-01 challenge via Gandi and obtain a real
   Let's Encrypt cert. The existing nvpn/Tailscale listeners stay as-is.

   > **Gotcha (cost us several retries): Gandi LiveDNS propagation lag.**
   > New TXT records take **~2 minutes** to appear on Gandi's
   > authoritative nameservers, and public resolvers negative-cache the
   > `NXDOMAIN` (the records carry a 3h TTL). If Caddy checks propagation
   > against a public resolver (`1.1.1.1`) it can time out and abandon the
   > challenge (symptom: repeated `trying to solve challenge` with no
   > `certificate obtained`, and the `_acme-challenge` TXT keeps getting
   > created+deleted). The `vezir.example.com` block in
   > `infra/caddy/Caddyfile.example` already mitigates this: it points the
   > propagation check at Gandi's **authoritative** nameservers and adds
   > `propagation_delay 120s` + `propagation_timeout 600s`. Find your
   > domain's real authoritative NS with `dig +short NS example.com`
   > (they're `ns-XX-{a,b,c}.gandi.net`, **not** `ns-1/2/3`).

   > **Token-leak gotcha:** the stock Caddy systemd unit runs
   > `caddy run --environ`, which prints every env var — including
   > `GANDI_API_TOKEN` — to the journal at startup. Override `ExecStart`
   > to drop `--environ` (see the drop-in in step 3 above).

---

## Step 5 — Verify end to end

```bash
# From any external network (e.g. your phone on cellular):
curl -sS https://vezir.example.com/health
# => {"status":"ok","version":"0.7.x","data_dir":"..."}

# Public CA chain (no -k needed: it's a real Let's Encrypt cert)
curl -sSI https://vezir.example.com/health | head -1   # HTTP/2 200
```

Then a real nostr login over clearnet:

```bash
export VEZIR_URL=https://vezir.example.com
vezir login                 # approve in your signer
vezir scribe --help         # any authenticated call confirms the JWT works
```

## Security checklist

- [ ] VPS sshd is key-only (`PasswordAuthentication no`).
- [ ] ufw denies everything except 22/tcp, 443/tcp, 51820/udp.
- [ ] WireGuard private keys never left the box they were generated on.
- [ ] `ss -ltn` on muscle shows vezir on `127.0.0.1:8000` only (never
      `0.0.0.0:8000`). Caddy is the sole ingress.
- [ ] The cert at `https://vezir.example.com` is Let's Encrypt
      (not Caddy internal) — confirms TLS terminates on muscle with a
      public chain, and the VPS only passed ciphertext.
- [ ] `vezir npub list` contains only authorized keys; nostr login from
      an un-allowlisted key returns 403.

## Rollback

The VPS path is additive. To fall back to nvpn/Tailscale, just point
`VEZIR_URL` back at the tunnel IP. Stopping `wg-quick@wg0` on the VPS or
removing the DNAT rule takes the public endpoint offline without
touching the server.
```
sudo systemctl stop wg-quick@wg0     # on the VPS — kills the public path
```
