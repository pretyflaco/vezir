#!/usr/bin/env bash
# Smoke test: start vezir, issue a token, upload a sample WAV, poll status.
#
# Usage:
#   ./tests/test_e2e.sh [path-to-sample.wav]
#
# Requires:
#   - vezir installed in the active venv (`pip install -e .`)
#   - `meet` (meetscribe) installed; `meet check` passes
#   - VEZIR_DATA pointing somewhere safe (default: ~/vezir-data-test)
#
# This is a development convenience, not a unit test.

set -euo pipefail

WAV="${1:-}"
if [[ -z "${WAV}" ]]; then
  # Try to find any existing recording.
  for d in "${HOME}/meet-recordings"/*/; do
    for f in "$d"*.wav; do
      if [[ -f "$f" ]]; then WAV="$f"; break 2; fi
    done
  done
fi
if [[ -z "${WAV}" || ! -f "${WAV}" ]]; then
  echo "ERROR: no WAV provided and none found under ~/meet-recordings/" >&2
  echo "Usage: $0 path/to/meeting.wav" >&2
  exit 1
fi

export VEZIR_DATA="${VEZIR_DATA:-${HOME}/vezir-data-test}"
export VEZIR_PORT="${VEZIR_PORT:-8123}"
export VEZIR_HOST="127.0.0.1"
URL="http://127.0.0.1:${VEZIR_PORT}"

echo ">>> using VEZIR_DATA=${VEZIR_DATA}"
mkdir -p "${VEZIR_DATA}"

echo ">>> issuing token"
TOKEN_OUT="$(vezir token issue --github e2e-test)"
echo "${TOKEN_OUT}"
TOKEN="$(echo "${TOKEN_OUT}" | grep -oE 'VEZIR_TOKEN=[^[:space:]]+' | cut -d= -f2-)"
if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: failed to parse token" >&2
  exit 1
fi

echo ">>> starting vezir serve in background"
vezir serve > /tmp/vezir-test.log 2>&1 &
SERVE_PID=$!
trap 'kill -INT "${SERVE_PID}" 2>/dev/null || true; wait "${SERVE_PID}" 2>/dev/null || true' EXIT

echo ">>> waiting for /health"
for i in $(seq 1 30); do
  if curl -fsS "${URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
  if (( i == 30 )); then
    echo "ERROR: vezir did not come up; tail of log:" >&2
    tail -50 /tmp/vezir-test.log >&2
    exit 1
  fi
done

echo ">>> /health"
curl -fsS "${URL}/health" | tee /dev/stderr; echo

echo ">>> uploading ${WAV}"
RESP="$(curl -fsS \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "audio=@${WAV}" \
  -F "title=e2e-test" \
  "${URL}/upload")"
echo "${RESP}"

SID="$(echo "${RESP}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')"
echo ">>> session id: ${SID}"

echo ">>> tail of server log (truncated):"
tail -15 /tmp/vezir-test.log || true

echo ">>> NOTE: the worker now runs meet transcribe; this can take minutes."
echo ">>> check status with:"
echo "    curl -H 'Authorization: Bearer ${TOKEN}' ${URL}/api/sessions/${SID}"
echo ">>> or open ${URL}/s/${SID} in your browser (with the same token)."
