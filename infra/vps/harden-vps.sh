#!/usr/bin/env bash
# harden-vps.sh — baseline hardening for the vezir VPS front (Ubuntu 24.04).
#
# Run as root ON THE VPS:
#   sudo bash harden-vps.sh
#
# Idempotent: safe to re-run. It does NOT touch WireGuard or nftables
# (those have their own steps in infra/vps/README.md); it only locks down
# the host: firewall, fail2ban, automatic security updates, and key-only
# SSH.
#
# SAFETY: before this script disables password auth, make sure your SSH
# *key* login already works in a SEPARATE terminal. If you lock yourself
# out, recover via the provider web console.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
	echo "must run as root (use sudo)" >&2
	exit 1
fi

echo "==> apt update + base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ufw fail2ban unattended-upgrades

echo "==> ufw: default deny inbound, allow 22/443/51820"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw allow 443/tcp comment 'https (DNAT to muscle)'
ufw allow 51820/udp comment 'wireguard'
ufw --force enable
ufw status verbose

echo "==> fail2ban: enable sshd jail"
cat >/etc/fail2ban/jail.d/vezir-sshd.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
findtime = 10m
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo "==> unattended-upgrades: enable security auto-updates"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

echo "==> sshd: key-only auth"
SSHD_DROPIN=/etc/ssh/sshd_config.d/99-vezir-hardening.conf
cat >"${SSHD_DROPIN}" <<'EOF'
# vezir VPS hardening — key-only SSH.
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
EOF
# Validate before reloading so a typo can't lock us out.
sshd -t
systemctl reload ssh || systemctl reload sshd

echo
echo "==> DONE. Verify in a SEPARATE terminal that key login still works:"
echo "      ssh <user>@<this-vps>"
echo "    Then confirm the firewall:  ufw status verbose"
echo "    fail2ban status:            fail2ban-client status sshd"
