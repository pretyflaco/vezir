#!/usr/bin/env bash
# vezir Tinfoil model watchdog.
#
# Tinfoil retires models out from under us.  deepseek-v4-pro went in
# 2026-07 with notice; glm-5-2 went in 2026-09 with *none* — it simply
# vanished from /v1/models and started answering 503, which killed every
# `confidential` job (that preset never falls back, by design).
#
# This check asks the catalog daily whether the models we depend on are
# (a) still present and (b) not flagged deprecated, and logs to the
# journal if either is false.  It only reports — it never restarts vezir
# or edits config, because the fix is always a code change (the model
# names live in millet's constants, not in env).
#
#   journalctl --user -t vezir-model-check --since "-7 days"
#
set -u

LOG_TAG="vezir-model-check"
URL="https://inference.tinfoil.sh/v1/models"
ENV_FILE="${VEZIR_ENV_FILE:-$HOME/.config/environment.d/vezir.conf}"

# Models vezir's summarization depends on. Keep in sync with
# millet/summarize.py: DEFAULT_TINFOIL_MODEL + DEFAULT_TINFOIL_FALLBACK_MODEL.
MODELS="${VEZIR_TINFOIL_MODELS:-glm-5-3-flash deepseek-v4-1-flash}"

key="${TINFOIL_API_KEY:-}"
if [ -z "$key" ] && [ -r "$ENV_FILE" ]; then
  key="$(sed -n 's/^TINFOIL_API_KEY=//p' "$ENV_FILE" | head -1)"
fi
if [ -z "$key" ]; then
  echo "no TINFOIL_API_KEY available; skipping model check" \
    | systemd-cat -t "$LOG_TAG" -p info
  exit 0
fi

catalog="$(curl -sS -m 20 -H "Authorization: Bearer $key" "$URL" 2>/dev/null)" || catalog=""
if [ -z "$catalog" ]; then
  # Network blip: advisory check, not worth alerting on.
  echo "Tinfoil catalog unreachable; skipping" | systemd-cat -t "$LOG_TAG" -p info
  exit 0
fi

status=0
for model in $MODELS; do
  report="$(printf '%s' "$catalog" | MODEL="$model" python3 -c '
import json, os, sys
model = os.environ["MODEL"]
try:
    data = {m.get("id"): m for m in json.load(sys.stdin).get("data", [])}
except Exception:
    sys.exit(0)  # unparseable: treat as a blip, stay quiet
entry = data.get(model)
if entry is None:
    print("ERR|%s is NOT in the Tinfoil catalog - it has been retired; "
          "confidential summaries will fail" % model)
elif entry.get("deprecated"):
    print("WARN|%s is deprecated, removal on %s - migrate before then"
          % (model, entry.get("deprecationDate") or "an unannounced date"))
')"
  [ -z "$report" ] && continue
  level="${report%%|*}"
  message="${report#*|}"
  if [ "$level" = "ERR" ]; then
    echo "$message" | systemd-cat -t "$LOG_TAG" -p err
    status=1
  else
    echo "$message" | systemd-cat -t "$LOG_TAG" -p warning
  fi
done

exit "$status"
