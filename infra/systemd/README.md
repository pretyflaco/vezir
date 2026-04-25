# Systemd user unit

Install:

```bash
mkdir -p ~/.config/systemd/user
cp infra/systemd/vezir.service ~/.config/systemd/user/

# Optional: env file picked up by EnvironmentFile=
mkdir -p ~/.config/environment.d
cat > ~/.config/environment.d/vezir.conf <<'EOF'
HF_TOKEN=hf_...
OPENROUTER_API_KEY=sk-or-...
EOF

systemctl --user daemon-reload
systemctl --user enable --now vezir.service
journalctl --user -u vezir.service -f
```

To run the service even when logged out (Tailscale-only headless box):

```bash
loginctl enable-linger $USER
```
