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
                              │ HTTPS (TLS to vezir.example.com)
                              ▼
        ┌─────────────────────────────────────────────┐
        │ VPS <VPS_PUBLIC_IP> / <VPS_PUBLIC_IPV6>     │
        │ nftables DNAT tcp/443 (IPv4 + IPv6)         │ L4 only: ciphertext
        │ wg0 10.99.0.1 / fd99::1                     │
        └──────────────────────┬──────────────────────┘
                               │ WireGuard (muscle dials out,
                               │ UDP 51820, PersistentKeepalive=25)
                              ▼
        ┌─────────────────────────────────────────────┐
        │ muscle wg0 10.99.0.2 / fd99::2              │ home, double-NAT
        │ Caddy :443 ── TERMINATES TLS                │ real LE cert (DNS-01)
        │ vezir uvicorn 127.0.0.1:8000                │
        └─────────────────────────────────────────────┘
```

### Key property: the VPS never sees plaintext

TLS is **terminated on muscle**, not on the VPS. The VPS runs
nftables DNAT rules that forward the raw TCP stream of `:443` into the
WireGuard tunnel to `10.99.0.2:443` (IPv4) or `[fd99::2]:443` (IPv6).
Everything the VPS handles is
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
| VPS public IPv4  | `<VPS_PUBLIC_IP>`                         |
| VPS public IPv6  | `<VPS_PUBLIC_IPV6>`                       |
| VPS OS           | Ubuntu 24.04                            |
| VPS hostname     | `<vps-hostname>`                           |
| Public hostname  | `vezir.example.com` (DNS at Gandi)    |
| WireGuard subnets| `10.99.0.0/24`, ULA `fd99::/64`       |
| VPS WG IPs       | `10.99.0.1`, `fd99::1`                |
| muscle WG IPs    | `10.99.0.2`, `fd99::2`                |
| WG UDP port      | `51820`                                 |
| Public TCP port  | `443` (HTTPS)                           |
| SSH              | `22`, **key-only** (no passwords)       |

## Files in this directory

| File                     | Where it runs | Purpose |
|--------------------------|---------------|---------|
| `wg-vps.conf.example`    | VPS           | WireGuard interface for the VPS end |
| `wg-muscle.conf.example` | muscle        | WireGuard interface for the muscle end |
| `nftables-dnat.conf`     | VPS           | IPv4/IPv6 DNAT `:443` to muscle + forward rules |
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

For dual stack, UFW must have `IPV6=yes` in `/etc/default/ufw`. If it
does not, set it and recreate the allows so both families are covered:

```bash
sudo sed -i 's/^IPV6=.*/IPV6=yes/' /etc/default/ufw
sudo ufw allow 22/tcp comment 'ssh'
sudo ufw allow 443/tcp comment 'https (DNAT to muscle)'
sudo ufw allow 51820/udp comment 'wireguard'
sudo ufw reload
sudo ufw status verbose   # each allow must show both (v4) and (v6)
```

Also configure the **provider/cloud firewall** (it runs outside UFW):
allow inbound TCP 443 to the VPS public IPv4 and IPv6 addresses and UDP
51820 to the WireGuard endpoint; restrict SSH to trusted sources where
possible. The public IPv6 address is the provider-assigned global address,
never the tunnel's ULA `fd99::1`.

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

The IPv6 addresses are ULA and exist only inside WireGuard. The VPS peer
allows only muscle's two tunnel addresses; muscle's peer allows
`0.0.0.0/0, ::/0` so real public client sources can traverse the tunnel.
`Table = off` plus source-specific policy routes keeps those broad peer
AllowedIPs from hijacking muscle's normal egress.

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
ping -c3 10.99.0.1       # VPS WG IPv4
ping -6 -c3 fd99::1      # VPS WG IPv6
sudo wg show             # recent handshake + rx/tx counters
ip rule show; ip route show table 100
ip -6 rule show; ip -6 route show table 100
ip route get 203.0.113.1 from 10.99.0.2   # must use wg0, table 100
ip -6 route get 2001:db8::1 from fd99::2  # must use wg0, table 100
ip route get 203.0.113.1                  # normal egress, not wg0
ip -6 route get 2001:db8::1               # normal egress (or unreachable), not wg0
```

> muscle is the dialer because it's the one behind NAT. `Endpoint` is set
> only in muscle's config (pointing at the VPS public IP:51820); the VPS
> learns muscle's address from the incoming handshake.
> `PersistentKeepalive=25` on muscle keeps the NAT mapping alive.

---

## Step 3 — nftables DNAT on the VPS

Forward public TCP `:443` into the tunnel. Copy `nftables-dnat.conf` to
the VPS, then enable both forwarding families:

```bash
scp infra/vps/nftables-dnat.conf ubuntu@<VPS_PUBLIC_IP>:/tmp/
ssh ubuntu@<VPS_PUBLIC_IP> 'sudo cp /tmp/nftables-dnat.conf /etc/nftables.d/vezir-dnat.conf'
ssh ubuntu@<VPS_PUBLIC_IP> 'printf "%s\n" "net.ipv4.ip_forward=1" "net.ipv6.conf.all.forwarding=1" | sudo tee /etc/sysctl.d/99-vezir-forward.conf'
```

> **IPv6 router-advertisement caveat:** Linux normally stops accepting
> RAs when IPv6 forwarding is enabled. If the VPS learns its public IPv6
> address or default route by SLAAC/RA on `ens3`, add
> `net.ipv6.conf.ens3.accept_ra=2` to the same sysctl file **before**
> applying it, or IPv6 default routing may disappear. Check with
> `ip -6 addr show dev ens3` and `ip -6 route show default`. A statically
> configured provider address/default route does not need `accept_ra=2`.

Validate and load without changing anything until the parse succeeds:

```bash
ssh ubuntu@<VPS_PUBLIC_IP> 'sudo nft -c -f /etc/nftables.d/vezir-dnat.conf && sudo sysctl --system && sudo systemctl enable --now nftables && sudo nft -f /etc/nftables.d/vezir-dnat.conf'
```

IPv4 DNATs to `10.99.0.2:443`; IPv6 DNATs only WAN TCP 443 to
`[fd99::2]:443`. Established return traffic is accepted in each family.
There is **no IPv4 masquerade and no NAT66**, so packets reach muscle with
the real client's source address. That preserves per-client rate limits
and log attribution; masquerading would collapse each family into one VPS
source/bucket (security audit 2026-10-30, M-1). Return packets work because
muscle policy-routes sources `10.99.0.2` and `fd99::2` through table 100
and `wg0`, while broad peer AllowedIPs accept every public client source.
Never add a masquerade rule to "fix" return routing; fix the policy route
instead.

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

1. **A record:** in the Gandi LiveDNS panel for `example.com`, add or keep
   `vezir A <VPS_PUBLIC_IP>` (the VPS public IPv4). Add
   `vezir AAAA <VPS_PUBLIC_IPV6>` **only after** the direct IPv6 tests in
   Step 5 pass. AAAA is always last during rollout and first to be removed
   during rollback, leaving IPv4 service untouched. Never publish the ULA
   `fd99::1` as AAAA.

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
   No separate IPv6 site block is needed; confirm Caddy's `:443` listener
   accepts IPv6 with `sudo ss -ltnp | grep ':443'`.

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

## Rolling upgrade of an existing IPv4 path

Apply in **server → VPS → tunnel/direct tests → AAAA** order. Merge the
template changes into the live files; do not overwrite live private keys or
endpoints with placeholders.

1. **Server (muscle) first.** Back up and add the ULA address, IPv6 policy
   rule/route and teardown, and `::/0` peer AllowedIPs:

   ```bash
   sudo cp -a /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.pre-dual-stack
   sudoedit /etc/wireguard/wg0.conf
   sudo systemctl restart wg-quick@wg0
   ping -c3 10.99.0.1
   # From an external host, IPv4 must still work before continuing:
   curl -4 -fsS https://vezir.example.com/health
   ```

2. **VPS second.** Back up, merge its ULA/peer changes and the new nftables
   file, and set both forwarding sysctls. Add the WAN `accept_ra=2` setting
   first if the provider uses RA as described in Step 3:

   ```bash
   sudo cp -a /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.pre-dual-stack
   sudo cp -a /etc/nftables.d/vezir-dnat.conf /etc/nftables.d/vezir-dnat.conf.pre-dual-stack
   sudo cp -a /etc/sysctl.d/99-vezir-forward.conf /etc/sysctl.d/99-vezir-forward.conf.pre-dual-stack
   # Save the previous runtime values too; restoring a config file cannot
   # unset values that are absent from it. Replace ens3 if the WAN differs.
   sudo sysctl net.ipv6.conf.all.forwarding net.ipv6.conf.ens3.accept_ra | \
     sudo tee /root/vezir-dual-stack-sysctl.pre
   sudoedit /etc/wireguard/wg0.conf
   sudoedit /etc/nftables.d/vezir-dnat.conf
   sudoedit /etc/sysctl.d/99-vezir-forward.conf
   sudo nft -c -f /etc/nftables.d/vezir-dnat.conf
   sudo sysctl --system
   sudo systemctl restart wg-quick@wg0
   sudo nft -f /etc/nftables.d/vezir-dnat.conf
   # From an external host, again prove IPv4 before any IPv6 DNS change:
   curl -4 -fsS https://vezir.example.com/health
   ```

3. Run both tunnel pings, the policy-route checks, and the direct IPv4/IPv6
   health tests in Steps 2 and 5. Check UFW and the provider firewall if
   IPv6 alone fails.
4. **AAAA last:** publish `vezir AAAA <VPS_PUBLIC_IPV6>` only after all
   checks pass, then run the forced `curl -4` and `curl -6` tests again.

---

## Step 5 — Verify end to end

Before publishing AAAA, test both data paths directly from an external
dual-stack host. `--resolve` keeps the correct hostname/SNI without DNS:

```bash
curl -4 -fsS --resolve vezir.example.com:443:<VPS_PUBLIC_IP> \
  https://vezir.example.com/health
curl -6 -fsS --resolve vezir.example.com:443:<VPS_PUBLIC_IPV6> \
  https://vezir.example.com/health
# Both return {"status":"ok",...}; no -k: the public CA chain must verify.
```

While running those curls, watch the forwarded packets on both ends:

```bash
# VPS: SYN arrives on WAN and leaves wg0 with the original client source.
sudo tcpdump -ni ens3 'tcp port 443'
sudo tcpdump -ni wg0 'tcp port 443'
# muscle: the same original client source arrives; replies use fd99::2
# (or 10.99.0.2 for IPv4), never a VPS masquerade address.
sudo tcpdump -ni wg0 'tcp port 443'
# On rate-limit events, keys must likewise contain real client IPs:
journalctl --user -u vezir --since '15 min ago' | grep 'ratelimit'
```

For an explicit per-IP limiter check in a maintenance window, use two
external hosts: exhaust a lightweight unauthenticated API bucket on one
and confirm it receives 429 while the other still receives 200. Do not
spray authentication failures or run this against production traffic.

Only after both direct tests pass, publish AAAA. Then verify normal DNS
selection and forced address families:

```bash
dig +short A vezir.example.com
dig +short AAAA vezir.example.com
curl -4 -fsS https://vezir.example.com/health
curl -6 -fsS https://vezir.example.com/health
curl -fsSI https://vezir.example.com/health | head -1   # HTTP/2 200
```

Then a real nostr login over clearnet:

```bash
export VEZIR_URL=https://vezir.example.com
vezir login                 # approve in your signer
vezir scribe --help         # any authenticated call confirms the JWT works
```

## Security checklist

- [ ] VPS sshd is key-only (`PasswordAuthentication no`).
- [ ] UFW has `IPV6=yes` and denies everything except 22/tcp, 443/tcp,
      51820/udp in both families; the provider firewall allows the same
      required public paths.
- [ ] `nft list ruleset` has no masquerade/NAT66; IPv6 DNAT is only WAN
      TCP 443 to `[fd99::2]:443`.
- [ ] WireGuard private keys never left the box they were generated on.
- [ ] `ss -ltn` on muscle shows vezir on `127.0.0.1:8000` only (never
      `0.0.0.0:8000`). Caddy is the sole ingress.
- [ ] The cert at `https://vezir.example.com` is Let's Encrypt
      (not Caddy internal) — confirms TLS terminates on muscle with a
      public chain, and the VPS only passed ciphertext.
- [ ] `vezir npub list` contains only authorized keys; nostr login from
      an un-allowlisted key returns 403.

## Rollback

For an IPv6 problem, **remove AAAA first** in Gandi LiveDNS and leave the A
record untouched. Wait for caches to expire, then confirm clients no longer
receive an IPv6 answer and IPv4 remains healthy:

```bash
dig +short AAAA vezir.example.com   # eventually empty
dig +short A vezir.example.com      # still <VPS_PUBLIC_IP>
curl -4 -fsS https://vezir.example.com/health
```

That is the normal, lowest-risk rollback; the dormant IPv6 tunnel rules can
stay in place. To remove them completely after DNS rollback, stop each tunnel
while its dual-stack config is still active so the IPv6 `PostDown` teardown
runs. Then restore the VPS backups first, explicitly delete the two IPv6
tables (restoring an old IPv4-only file does not delete them), and start the
old configuration. Do the same stop → restore → start sequence on the server;
a restart after restoring would run the old teardown and leave the IPv6 rule
behind. Finally restore the captured runtime sysctls: `sysctl --system` cannot
unset forwarding or `accept_ra` values that are absent from the old file.

```bash
# VPS: stop before restoring so the dual-stack teardown is used
sudo systemctl stop wg-quick@wg0
sudo cp -a /etc/wireguard/wg0.conf.pre-dual-stack /etc/wireguard/wg0.conf
sudo cp -a /etc/nftables.d/vezir-dnat.conf.pre-dual-stack /etc/nftables.d/vezir-dnat.conf
sudo cp -a /etc/sysctl.d/99-vezir-forward.conf.pre-dual-stack /etc/sysctl.d/99-vezir-forward.conf
sudo nft delete table ip6 vezir_nat
sudo nft delete table ip6 vezir_filter
sudo nft -c -f /etc/nftables.d/vezir-dnat.conf
sudo sysctl --system
sudo sysctl -p /root/vezir-dual-stack-sysctl.pre
sudo systemctl start wg-quick@wg0
sudo nft -f /etc/nftables.d/vezir-dnat.conf
# Server, only after the VPS is back on its previous configuration
sudo systemctl stop wg-quick@wg0
sudo cp -a /etc/wireguard/wg0.conf.pre-dual-stack /etc/wireguard/wg0.conf
sudo systemctl start wg-quick@wg0
# The dual-stack PostDown must have removed these on the server:
ip -6 rule show | grep 'from fd99::2'   # no output
ip -6 route show table 100              # no IPv6 default via wg0
# From an external host
curl -4 -fsS https://vezir.example.com/health
```

Do not stop the whole tunnel or add masquerade for an IPv6-only rollback;
either would needlessly disrupt or weaken the working IPv4 path.
