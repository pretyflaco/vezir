#!/usr/bin/env bash
# vezir health watchdog.
#
# Two probes, two very different responses:
#
#   1. Loopback /health  -> RESTART vezir when not 200.  A hung or crashed
#      process is fixable by a restart, so that is what happens.
#   2. Public  /health   -> ALERT ONLY, never a restart.  The public path
#      is VPS nftables DNAT -> WireGuard tunnel -> caddy -> uvicorn, and
#      none of that is repairable by restarting vezir.  (Incident
#      2026-09-17: a modem reboot killed the WG tunnel for ~30 min while
#      this check read green on loopback the entire time.)
#
# Public alerts fire on state transitions (DOWN, then a heartbeat every
# ~20 checks ≈ 30 min at the 90 s cadence, then RECOVERED with the outage
# length) so the journal stays readable instead of spamming one line per
# tick.  The DOWN message carries the runbook pointer because the correct
# first move is diagnosing the tunnel, not touching vezir.
set -u

LOG_TAG="vezir-healthcheck"
URL="http://127.0.0.1:8000/health"
PUBLIC_URL="https://vezir.twentyone.ist/health"
STATE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/vezir-healthcheck-public.state"

# ── Probe 1: loopback — restart on failure ─────────────────────────────────

code="$(curl -sS -o /dev/null -m 8 -w "%{http_code}" "$URL" 2>/dev/null || echo 000)"
if [ "$code" != "200" ]; then
  echo "vezir health check failed (HTTP $code); restarting vezir" \
    | systemd-cat -t "$LOG_TAG" -p warning
  systemctl --user restart vezir
  sleep 5
  code2="$(curl -sS -o /dev/null -m 8 -w "%{http_code}" "$URL" 2>/dev/null || echo 000)"
  if [ "$code2" != "200" ]; then
    echo "vezir still unhealthy after restart (HTTP $code2)" \
      | systemd-cat -t "$LOG_TAG" -p err
    exit 1
  fi
  echo "vezir recovered after restart (HTTP 200)" | systemd-cat -t "$LOG_TAG" -p info
fi

# ── Probe 2: public path — alert only ───────────────────────────────────────

mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
pcode="$(curl -sS -o /dev/null -m 10 -w "%{http_code}" "$PUBLIC_URL" 2>/dev/null || echo 000)"

_count() {
  n="$(cat "$STATE_FILE" 2>/dev/null || true)"
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
}

if [ "$pcode" = "200" ]; then
  if [ -f "$STATE_FILE" ]; then
    _count
    rm -f "$STATE_FILE" 2>/dev/null || true
    echo "public path RECOVERED after $n failing check(s)" \
      | systemd-cat -t "$LOG_TAG" -p info
  fi
  exit 0
fi

_count
n=$((n + 1))
echo "$n" > "$STATE_FILE" 2>/dev/null || true
if [ "$n" -eq 1 ] || [ $((n % 20)) -eq 0 ]; then
  echo "public path unhealthy (HTTP $pcode; $n consecutive check(s)). \
Restarting vezir will NOT fix this. Check the tunnel first: 'sudo wg show' \
on the VPS (handshake age) and on saray, wg-quick@wg0 + caddy on saray, \
and any recent modem/NAT change. Runbook: infra/vps/README.md" \
    | systemd-cat -t "$LOG_TAG" -p warning
fi
exit 0
