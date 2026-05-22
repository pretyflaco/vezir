#!/usr/bin/env bash
# install-caddy.sh — idempotent Caddy installer for the vezir GPU server.
#
# Supports macOS (Homebrew) and Debian/Ubuntu Linux. Other distros: install
# Caddy from https://caddyserver.com/docs/install and copy Caddyfile.example
# to /etc/caddy/Caddyfile by hand.
#
# Usage:
#   ./install-caddy.sh           # install + drop sample Caddyfile if missing
#   ./install-caddy.sh --force   # overwrite an existing /etc/caddy/Caddyfile
#
# This script does NOT start Caddy automatically. Edit the Caddyfile to
# match your hostnames, then start with:
#   macOS:   brew services start caddy
#   Linux:   sudo systemctl enable --now caddy

set -euo pipefail

FORCE=0
for arg in "$@"; do
	case "$arg" in
		--force) FORCE=1 ;;
		-h|--help)
			sed -n '1,/^set -euo/p' "$0" | sed -n 's/^# \{0,1\}//p'
			exit 0
			;;
		*) echo "unknown flag: $arg" >&2; exit 2 ;;
	esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SAMPLE="$SCRIPT_DIR/Caddyfile.example"

step() { printf '\n==> %s\n' "$*"; }

install_caddy() {
	if command -v caddy >/dev/null 2>&1; then
		step "caddy already installed: $(caddy version | head -1)"
		return
	fi
	case "$(uname -s)" in
		Darwin)
			step "installing Caddy via Homebrew"
			if ! command -v brew >/dev/null 2>&1; then
				echo "Homebrew not found. Install from https://brew.sh first." >&2
				exit 1
			fi
			brew install caddy
			;;
		Linux)
			step "installing Caddy via official apt repo"
			# Cloudsmith repo per https://caddyserver.com/docs/install
			sudo apt-get update -qq
			sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl
			curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
				| sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
			curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
				| sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
			sudo apt-get update -qq
			sudo apt-get install -y -qq caddy
			;;
		*)
			echo "Unsupported OS: $(uname -s). Install Caddy manually." >&2
			exit 1
			;;
	esac
}

place_caddyfile() {
	local dest
	case "$(uname -s)" in
		Darwin) dest="$(brew --prefix)/etc/Caddyfile" ;;
		Linux)  dest="/etc/caddy/Caddyfile" ;;
		*)      dest="./Caddyfile" ;;
	esac

	if [[ -f "$dest" && "$FORCE" -eq 0 ]]; then
		step "Caddyfile already at $dest; not overwriting (--force to replace)"
		return
	fi

	step "writing sample Caddyfile to $dest"
	if [[ "$dest" == /etc/* ]]; then
		sudo install -d "$(dirname "$dest")"
		sudo install -m 0644 "$SAMPLE" "$dest"
	else
		install -d "$(dirname "$dest")"
		install -m 0644 "$SAMPLE" "$dest"
	fi
	echo "Edit $dest to replace the example hostnames before starting Caddy."
}

print_next_steps() {
	cat <<'EOF'

next steps
==========
  1. Edit the Caddyfile to use your real Tailscale ts.net hostname and
     nvpn server IP. Comments in the file explain each block.

  2. Make sure vezir is bound to 127.0.0.1:8000 (the 0.1.12 default).
     If you previously exported VEZIR_HOST=0.0.0.0, unset it now.

  3. Start Caddy:
       macOS:  brew services start caddy
       Linux:  sudo systemctl enable --now caddy

  4. For nvpn joiners, expose the internal CA root once and distribute it
     through the device-enrollment QR (Android does this automatically
     from 0.1.4+; CLI users run `vezir trust-server`).

EOF
}

install_caddy
place_caddyfile
print_next_steps
