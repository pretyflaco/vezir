# Caddy reverse proxy for vezir

From 0.1.12 onward, `vezir serve` binds to `127.0.0.1:8000` by default. A
reverse proxy is needed to expose it over the VPN with TLS. Caddy is the
recommended choice; this directory has a working sample and an installer.

## Why TLS over a VPN?

Two reasons:

1. **Defence in depth.** A misconfigured firewall, a stale Tailscale ACL,
   or a compromised nvpn key shouldn't immediately expose bearer tokens
   in cleartext. Caddy stops the plaintext at the box's edge.

2. **Caddy access logs scrub `Authorization` and `Cookie` headers**
   (see `Caddyfile.example`'s `common_logging` snippet). Without a
   reverse proxy, anything that logs the request line — uvicorn's
   access log, a future debug `tcpdump`, an nginx-style sidecar —
   captures the bearer token on disk.

## Files

* `Caddyfile.example` — fully commented sample with one listener per VPN
  transport (Tailscale and nvpn).
* `install-caddy.sh` — installs Caddy via Homebrew (macOS) or the
  official apt repo (Debian/Ubuntu) and drops the sample Caddyfile into
  the right place. Idempotent; safe to re-run.

## Quick start

```bash
cd infra/caddy
./install-caddy.sh
# Edit /etc/caddy/Caddyfile (Linux) or $(brew --prefix)/etc/Caddyfile (macOS)
# to use your real Tailscale ts.net hostname and nvpn IP.

# macOS
brew services start caddy

# Linux
sudo systemctl enable --now caddy

# Verify
curl -sS https://muscle.tail178bd.ts.net/health   # Tailscale: trusted Let's Encrypt
curl -sS --cacert /etc/ssl/caddy-root.crt \
     https://10.44.141.239/health                 # nvpn: internal CA
```

## TLS strategy by transport

| Transport | Cert source | Joiner action |
|-----------|-------------|---------------|
| Tailscale | Let's Encrypt via ts.net DNS-01 (or `tailscale serve` which proxies to Caddy) | None — system trust store works |
| nvpn      | Caddy internal CA (auto-rotated) | Trust the CA root once. Android imports it from the enrollment QR automatically (0.1.4+). CLI users run `vezir trust-server` against the QR JSON. |

## Migration from pre-0.1.12 (vezir bound to 0.0.0.0)

Operators upgrading need to do three things in order:

1. **Install Caddy** (above).
2. **Update `VEZIR_URL`** for all clients to the Caddy host (HTTPS).
   This is part of the enrollment QR payload, so re-enrolling devices
   handles it. CLI users update their shell rc.
3. **Restart vezir.** It now binds 127.0.0.1; uvicorn refuses external
   connections automatically. Confirm with `ss -ltn | grep 8000` —
   should show `127.0.0.1:8000`, never `0.0.0.0:8000`.

If you need the old behaviour temporarily (e.g. you have not yet rolled
out Caddy and want to keep dogfood working), set:

```bash
export VEZIR_HOST=0.0.0.0
```

This is a deliberate opt-in escape hatch, not a default.

## Rate limiting

Vezir has an in-process token-bucket limiter (`vezir/server/ratelimit.py`)
that enforces:

* /upload — 10 / token / minute
* /login — 20 / IP / minute
* /api/* — 60 / token / minute

Caddy can layer an edge limiter on top via the `caddy-ratelimit` plugin
if dogfood ever sees abuse from a single source. Not required for v0.1.12.

## When to consider Tailscale Serve instead

If your team is Tailscale-only (no nvpn), `tailscale serve` can terminate
TLS for you with zero Caddy config. The tradeoff: you lose Caddy's
log-scrubbing and security-headers blocks, so you'd want to add those
inside vezir's FastAPI middleware instead. Caddy is the recommended path
unless you have a specific reason to prefer Tailscale Serve.
