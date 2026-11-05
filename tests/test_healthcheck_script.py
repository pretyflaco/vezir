"""infra/systemd/vezir-healthcheck.sh, run for real against stubbed tools.

``curl``, ``ip``, ``systemd-cat`` and ``systemctl`` are replaced by small
shell stubs on ``PATH``; everything else (bash, grep, coreutils) is real.
The stubs read their behavior from the environment:

* ``FAKE_LOOPBACK`` / ``FAKE_PUBLIC``: ``"<http_code>:<curl_exit>"`` for the
  loopback and public URL respectively.
* ``FAKE_WG_ADDR``: the IPv4 address ``ip -4 addr show dev wg0`` reports
  (empty = no wg0).
* ``FAKE_RULE``: ``1`` if ``ip rule show`` lists ``from 10.99.0.2 lookup 100``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "infra" / "systemd" / "vezir-healthcheck.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_CURL = r"""#!/usr/bin/env bash
url="${@: -1}"
echo "$url" >> "$FAKE_LOG_DIR/curl.log"
case "$url" in
  http://127.0.0.1:*) spec="$FAKE_LOOPBACK" ;;
  *) spec="$FAKE_PUBLIC" ;;
esac
printf '%s' "${spec%%:*}"
exit "${spec##*:}"
"""

_IP = r"""#!/usr/bin/env bash
case "$*" in
  "-4 addr show dev wg0")
    [ -n "${FAKE_WG_ADDR:-}" ] || { echo 'Device "wg0" does not exist.' >&2; exit 1; }
    echo "5: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420"
    echo "    inet $FAKE_WG_ADDR/24 scope global wg0"
    ;;
  "rule show")
    echo "0:	from all lookup local"
    [ "${FAKE_RULE:-0}" = 1 ] && echo "100:	from 10.99.0.2 lookup 100"
    echo "32766:	from all lookup main"
    ;;
  *) echo "unexpected ip $*" >&2; exit 2 ;;
esac
"""

_SYSTEMD_CAT = r"""#!/usr/bin/env bash
prio=info
while [ $# -gt 0 ]; do
  case "$1" in -p) prio="$2"; shift ;; esac
  shift
done
while IFS= read -r line; do echo "[$prio] $line" >> "$FAKE_LOG_DIR/journal.log"; done
"""

_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG_DIR/systemctl.log"
"""


@pytest.fixture
def run(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {
        "curl": _CURL, "ip": _IP, "systemd-cat": _SYSTEMD_CAT, "systemctl": _SYSTEMCTL,
    }.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    logs = tmp_path / "logs"
    logs.mkdir()
    state = tmp_path / "state"

    def _run(*, loopback="200:0", public="200:0", wg_addr="10.99.0.2", rule=True,
             public_url="https://vezir.example.com", **extra_env):
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "XDG_STATE_HOME": str(state),
            "FAKE_LOG_DIR": str(logs),
            "FAKE_LOOPBACK": loopback,
            "FAKE_PUBLIC": public,
            "FAKE_WG_ADDR": wg_addr,
            "FAKE_RULE": "1" if rule else "0",
            "VEZIR_HEALTH_RESTART_WAIT": "0",
        }
        if public_url is not None:
            env["VEZIR_PUBLIC_URL"] = public_url
        env.update(extra_env)
        proc = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30,
        )

        def _read(name):
            f = logs / name
            return f.read_text().splitlines() if f.exists() else []

        return proc, _read("journal.log"), _read("systemctl.log"), _read("curl.log")

    _run.state_file = state / "vezir-healthcheck-public.state"
    return _run


def test_healthy_logs_nothing(run):
    proc, journal, systemctl, curls = run()
    assert proc.returncode == 0
    assert journal == [] and systemctl == []
    assert curls == ["http://127.0.0.1:8000/health", "https://vezir.example.com/health"]


def test_timeout_with_missing_rule_names_the_rule(run):
    proc, journal, _, _ = run(public="000:28", rule=False)
    assert proc.returncode == 0
    assert len(journal) == 1
    line = journal[0]
    assert line.startswith("[err] ")
    assert "no connection, curl exit 28" in line
    assert "'from 10.99.0.2 lookup 100' is MISSING" in line
    assert "ManageForeignRoutingPolicyRules=no" in line


def test_connection_refused_with_missing_rule_names_the_rule(run):
    _, journal, _, _ = run(public="000:7", rule=False)
    assert "is MISSING" in journal[0]


def test_http_500_with_missing_rule_is_not_a_routing_failure(run):
    """An HTTP response proves the public path works, whatever the status."""
    _, journal, _, _ = run(public="500:0", rule=False)
    assert len(journal) == 1
    assert journal[0].startswith("[warning] ")
    assert "MISSING" not in journal[0]
    assert "HTTP 500, curl exit 0" in journal[0]


def test_tls_failure_with_missing_rule_is_not_blamed_on_the_rule(run):
    """No HTTP status, but the connection was made (curl 35 = TLS error)."""
    _, journal, _, _ = run(public="000:35", rule=False)
    assert journal[0].startswith("[warning] ")
    assert "MISSING" not in journal[0]


def test_timeout_with_rule_present_gets_generic_hint(run):
    _, journal, _, _ = run(public="000:28", rule=True)
    assert journal[0].startswith("[warning] ")
    assert "Restarting vezir will NOT fix this" in journal[0]
    assert "MISSING" not in journal[0]


def test_timeout_without_tunnel_address_gets_generic_hint(run):
    _, journal, _, _ = run(public="000:28", rule=False, wg_addr="")
    assert journal[0].startswith("[warning] ")
    _, journal, _, _ = run(public="000:28", rule=False, wg_addr="10.99.0.9")
    assert all("MISSING" not in line for line in journal)


def test_tunnel_address_and_table_overrides(run):
    _, journal, _, _ = run(
        public="000:28", rule=True, wg_addr="10.7.0.2",
        VEZIR_WG_TUNNEL_ADDR="10.7.0.2", VEZIR_WG_TABLE="200",
    )
    assert "'from 10.7.0.2 lookup 200' is MISSING" in journal[0]


def test_alert_cadence_and_recovery(run):
    for _ in range(19):
        run(public="000:28", rule=False)
    _, journal, _, _ = run(public="000:28", rule=False)  # check 20
    assert [line.split(";")[1].split()[0] for line in journal] == ["1", "20"]
    assert run.state_file.read_text().strip() == "20"
    _, journal, _, _ = run()
    assert journal[-1] == "[info] public path RECOVERED after 20 failing check(s)"
    assert not run.state_file.exists()


def test_public_probe_skipped_without_a_public_url(run):
    proc, journal, _, curls = run(public="000:28", public_url=None)
    assert proc.returncode == 0
    assert journal == []
    assert curls == ["http://127.0.0.1:8000/health"]


def test_explicit_public_health_url_wins(run):
    _, _, _, curls = run(
        public_url="https://ignored.example.com/",
        VEZIR_PUBLIC_HEALTH_URL="https://status.example.com/healthz",
    )
    assert curls[-1] == "https://status.example.com/healthz"


def test_trailing_slash_on_public_url(run):
    _, _, _, curls = run(public_url="https://vezir.example.com/")
    assert curls[-1] == "https://vezir.example.com/health"


def test_loopback_failure_restarts_vezir(run):
    proc, journal, systemctl, curls = run(loopback="000:7", VEZIR_PORT="8123")
    assert systemctl == ["--user restart vezir"]
    assert curls == ["http://127.0.0.1:8123/health"] * 2
    assert proc.returncode == 1
    assert journal[0].startswith("[warning] vezir health check failed (HTTP 000)")
    assert journal[1].startswith("[err] vezir still unhealthy after restart")
