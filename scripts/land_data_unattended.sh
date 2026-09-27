#!/bin/zsh
set -u

ROOT="${LAND_DATA_ROOT:-/Users/mars/Downloads/LAND-DATA}"
DB="${LAND_DATA_DB:-$ROOT/.local/nyc-incremental-acceptance-v2.db}"
LOG_DIR="${LAND_DATA_LOG_DIR:-$ROOT/.local/launchd}"
SOURCE_ARG="${1:-both}"
case "$SOURCE_ARG" in
  nyc) SOURCE_NAME=nyc-open-data-restaurants ;;
  osm) SOURCE_NAME=osm ;;
  events|nyc-parks-events) SOURCE_ARG=events; SOURCE_NAME=nyc-parks-events ;;
  both) SOURCE_NAME=both ;;
  *) print -r -- '{"status":"INVALID_SOURCE"}' ; exit 2 ;;
esac
LOCK_DIR="${LAND_DATA_LOCK_DIR:-$ROOT/.local/land-data-${SOURCE_ARG}.lock}"
RETRIES="${LAND_DATA_RETRIES:-2}"
DELAY="${LAND_DATA_RETRY_DELAY:-30}"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/${SOURCE_ARG}.runs.jsonl"
WRAPPER_LOG="$LOG_DIR/${SOURCE_ARG}.wrapper.jsonl"

# mkdir is atomic on macOS and makes overlapping launchd invocations harmless.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  print -r -- "{\"status\":\"SKIPPED_OVERLAP\",\"reason\":\"lock_exists\"}"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

if [[ -n "${LAND_DATA_PYTHON:-}" ]]; then
  PYTHON="$LAND_DATA_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]] && "$ROOT/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  PYTHON="$ROOT/.venv/bin/python"
elif [[ -x /opt/homebrew/bin/python3 ]]; then
  PYTHON=/opt/homebrew/bin/python3
else
  PYTHON="$(command -v python3)"
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$SOURCE_ARG" == nyc ]]; then
  TARGET_HOST="data.cityofnewyork.us"
elif [[ "$SOURCE_ARG" == osm ]]; then
  TARGET_HOST="overpass-api.de,overpass.kumi.systems"
elif [[ "$SOURCE_ARG" == events ]]; then
  TARGET_HOST="data.cityofnewyork.us"
else
  TARGET_HOST="data.cityofnewyork.us,overpass-api.de,overpass.kumi.systems"
fi
DNS_STATUS=$(TARGET_HOST="$TARGET_HOST" "$PYTHON" - <<'PY'
import os, socket
hosts = os.environ["TARGET_HOST"].split(",")
result = []
for host in hosts:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        result.append(host + ":OK")
    except OSError as exc:
        result.append(host + ":" + type(exc).__name__)
print(",".join(result))
PY
)
PROXY_PRESENT=NO
for proxy_name in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
  if [[ -n "${(P)proxy_name:-}" ]]; then PROXY_PRESENT=YES; break; fi
done
print -r -- "{\"diagnostic\":true,\"source\":\"$SOURCE_ARG\",\"target_host\":\"$TARGET_HOST\",\"dns\":\"$DNS_STATUS\",\"proxy_present\":\"$PROXY_PRESENT\",\"python\":\"$PYTHON\",\"cwd\":\"$ROOT\"}" | tee -a "$LOG_DIR/diagnostics.jsonl"

if [[ "$SOURCE_ARG" == nyc ]]; then
  BUDGET_SECONDS="${LAND_DATA_TIME_BUDGET_SECONDS:-2700}"
  MAX_BATCHES="${LAND_DATA_MAX_BATCHES:-8}"
  BATCH_PAUSE="${LAND_DATA_BATCH_PAUSE_SECONDS:-2}"
  batch=0
  started_epoch=$(date +%s)
  while (( batch < MAX_BATCHES )); do
    now_epoch=$(date +%s)
    if (( now_epoch - started_epoch >= BUDGET_SECONDS )); then
      print -r -- "{\"wrapper_status\":\"CONTINUE_REQUIRED\",\"reason\":\"time_budget\",\"batches\":$batch}" | tee -a "$WRAPPER_LOG"
      exit 0
    fi
    started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    output=$(cd "$ROOT" && "$PYTHON" -m land_data --db "$DB" multi-pipeline \
      --source-config config/multi-source.json --only-source nyc-open-data-restaurants 2>&1)
    code=$?
    ended=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    print -r -- "$output" | tee -a "$RUN_LOG"
    state=$(print -r -- "$output" | "$PYTHON" -c 'import json,sys
try:
    print(json.load(sys.stdin).get("sources", [{}])[0].get("status", "UNKNOWN"))
except Exception:
    print("NON_JSON_OUTPUT")')
    print -r -- "{\"wrapper_started_at\":\"$started\",\"wrapper_ended_at\":\"$ended\",\"batch\":$batch,\"exit_code\":$code,\"state\":\"$state\"}" | tee -a "$WRAPPER_LOG"
    print -r -- "{\"diagnostic\":true,\"source\":\"nyc-open-data-restaurants\",\"status\":\"$state\",\"error_type\":null}" | tee -a "$LOG_DIR/diagnostics.jsonl"
    if (( code != 0 )) || [[ "$state" == "FAILED" || "$state" == "RETRY_REQUIRED" || "$state" == "NON_JSON_OUTPUT" ]]; then
      exit ${code:-1}
    fi
    if [[ "$state" == "COMPLETE" || "$state" == "NOT_NEEDED" ]]; then
      exit 0
    fi
    (( batch++ ))
    now_epoch=$(date +%s)
    if (( batch >= MAX_BATCHES || now_epoch - started_epoch >= BUDGET_SECONDS )); then
      print -r -- "{\"wrapper_status\":\"CONTINUE_REQUIRED\",\"reason\":\"budget_or_batch_limit\",\"batches\":$batch}" | tee -a "$WRAPPER_LOG"
      exit 0
    fi
    sleep "$BATCH_PAUSE"
  done
  exit 0
fi

attempt=0
while (( attempt <= RETRIES )); do
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  args=(--source-config config/multi-source.json)
  if [[ "$SOURCE_ARG" == nyc ]]; then
    args+=(--only-source nyc-open-data-restaurants)
  elif [[ "$SOURCE_ARG" == osm ]]; then
    args+=(--only-source osm)
  elif [[ "$SOURCE_ARG" == events ]]; then
    args+=(--only-source nyc-parks-events)
  else
    args+=(--only-source nyc-open-data-restaurants --only-source osm)
  fi
  output=$(cd "$ROOT" && "$PYTHON" -m land_data --db "$DB" multi-pipeline "${args[@]}" 2>&1)
  code=$?
  ended=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  print -r -- "$output" | tee -a "$RUN_LOG"
  print -r -- "$output" | "$PYTHON" -c 'import json,sys
try:
    value=json.load(sys.stdin)
    for source in value.get("sources", []):
        reason=source.get("failure_reason") or ""
        print(json.dumps({"diagnostic": True, "source": source.get("source"),
                          "status": source.get("status"),
                          "error_type": reason.split(":", 1)[0] if reason else None}, sort_keys=True))
except Exception:
    print(json.dumps({"diagnostic": True, "status": "NON_JSON_OUTPUT", "error_type": "process_output"}))' | tee -a "$LOG_DIR/diagnostics.jsonl"
  print -r -- "{\"wrapper_started_at\":\"$started\",\"wrapper_ended_at\":\"$ended\",\"attempt\":$attempt,\"exit_code\":$code}" | tee -a "$WRAPPER_LOG"
  if (( code == 0 )); then exit 0; fi
  if (( attempt == RETRIES )); then exit "$code"; fi
  sleep "$DELAY"
  (( attempt++ ))
done
