#!/usr/bin/env bash
# Autonomous CTFY campaign launcher. One prompt, no manual per-instance setup.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT=""
for candidate in "${CTFY_PROJECT:-}" "$PWD" "$SCRIPT_DIR/.."; do
  if [[ -n "$candidate" && -f "$candidate/lib/ctfy_local.py" ]]; then
    PROJECT=$(cd "$candidate" && pwd)
    break
  fi
done
: "${PROJECT:?set CTFY_PROJECT to the sapper checkout (lib/ctfy_local.py not found)}"
ENV_FILE=${CTFY_ENV_FILE:-$HOME/.config/ctfy/env}
LOG=${CTFY_LOG:-$PROJECT/runs/qualifier-play.log}

mkdir -p "$PROJECT/runs" "$(dirname "${CTFY_LOG:-$PROJECT/runs/qualifier-play.log}")"
LOCK_FILE=${CTFY_LOCK_FILE:-$HOME/.config/ctfy/qualifier.lock}
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "ctfy-qualifier: another run holds the lock" >&2
  exit 0
fi

set -a
. "$ENV_FILE"
set +a

if [[ -n "${CTFY_INITIAL_TOKEN:-}" ]]; then
  export CTFY_TOKEN="$CTFY_INITIAL_TOKEN"
fi
: "${CTFY_TOKEN:?CTFY_TOKEN is missing}"
export CTFY_PLATFORM_URL=${CTFY_PLATFORM_URL:-https://ctfy.tsinghua.edu.cn}
export LLM_FLAVOR=${LLM_FLAVOR:-anthropic}
export LLM_API_KEY="${LLM_API_KEY:-${ANTHROPIC_API_KEY:-}}"
export LLM_MODEL=${LLM_MODEL:-deepseek-v4-pro}
# The engine speaks the OpenAI-compatible API, so this URL must include /v1.
export LLM_BASE_URL=${LLM_BASE_URL:-${OPENAI_BASE_URL:-https://deepseek.llm-next.ctfy.io/v1}}
export SAPPER_BIN=${SAPPER_BIN:-$PROJECT/openai_http_engine.py}

GW_HOST=$(python3 -c 'import os, urllib.parse; print(urllib.parse.urlsplit(os.environ["LLM_BASE_URL"]).hostname)')
GW_IP=$(getent ahostsv4 "$GW_HOST" | awk 'NR==1 {print $1}')
: "${GW_IP:?cannot resolve $GW_HOST}"

cd "$PROJECT"
args=(
  play
  --stall-minutes "${CTFY_STALL_MINUTES:-30}"
  --max-workers "${CTFY_MAX_WORKERS:-2}"
  --poll-seconds "${CTFY_POLL_SECONDS:-30}"
  --image "${CTFY_IMAGE:-sapper-ctfy-web-test:latest}"
  --max-turns "${CTFY_MAX_TURNS:-16}"
  --dispatch-timeout "${CTFY_DISPATCH_TIMEOUT:-1800}"
  --engine "${CTFY_ENGINE:-$PROJECT/openai_http_engine.py}"
  --add-host "$GW_HOST:$GW_IP"
)
if [[ -n "${CTFY_COMPETITION_ID:-}" ]]; then
  args+=(--competition "$CTFY_COMPETITION_ID")
fi
if [[ -n "${CTFY_MINUTES:-}" ]]; then
  args+=(--minutes "$CTFY_MINUTES")
fi
if [[ -n "${CTFY_ADOPT:-}" ]]; then
  args+=(--adopt "$CTFY_ADOPT")
fi

campaign_has_challenges() {
  python3 - "$PROJECT/runs" <<'PY'
import glob, json, sys, time
for path in glob.glob(sys.argv[1] + "/*/campaign.json"):
    try:
        state = json.load(open(path))
    except Exception:
        continue
    deadline = state.get("deadline")
    if isinstance(deadline, (int, float)) and deadline > time.time() \
            and state.get("challenges"):
        sys.exit(0)
sys.exit(1)
PY
}

for attempt in $(seq 1 40); do
  echo "[launcher] attempt $attempt at $(date -Is)" >>"$LOG"
  if python3 lib/ctfy_local.py "${args[@]}" >>"$LOG" 2>&1; then
    if campaign_has_challenges; then
      echo "[launcher] campaign finished" >>"$LOG"
      exit 0
    fi
    echo "[launcher] no challenges yet; waiting for the board" >>"$LOG"
  else
    echo "[launcher] attempt $attempt failed; retrying in 30s" >>"$LOG"
  fi
  sleep 30
done
echo "[launcher] giving up after 40 attempts" >>"$LOG"
exit 1
