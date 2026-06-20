#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

WORKSPACE="${AGENTDECK_TEST_WORKSPACE:-/tmp/agentdeck-list-test-$(date +%Y%m%d-%H%M%S)}"
MANAGER_FILE="${AGENTDECK_MANAGER_FILE:-/data/lyxie/TeleAgent/Manager.txt}"
TELEAGENT_CONFIG="${AGENTDECK_TELEAGENT_CONFIG:-/data/lyxie/TeleAgent/teleagent.toml}"
BOT_NAME="${AGENTDECK_TEST_BOT:-minsys-bot3}"
PROJECT_ID="${AGENTDECK_TEST_PROJECT:-testproj}"
PROJECT_TITLE="${AGENTDECK_TEST_PROJECT_TITLE:-Test Project}"
AGENT_ID="${AGENTDECK_TEST_AGENT:-owner}"
AGENT_TITLE="${AGENTDECK_TEST_AGENT_TITLE:-Owner}"
PROJECT_CWD="${AGENTDECK_TEST_PROJECT_CWD:-$SCRIPT_DIR}"

if [[ -z "${AGENTDECK_TELEGRAM_TOKEN:-}" ]]; then
  if [[ ! -f "$MANAGER_FILE" ]]; then
    echo "Missing manager file: $MANAGER_FILE" >&2
    exit 1
  fi
  AGENTDECK_TELEGRAM_TOKEN="$(
    awk -v bot="$BOT_NAME" '
      $0 ~ bot ":" { in_bot=1; next }
      in_bot && /Token:/ { print $2; exit }
      in_bot && /^[[:space:]]*minsys-bot[0-9]+:/ { exit }
    ' "$MANAGER_FILE"
  )"
  export AGENTDECK_TELEGRAM_TOKEN
fi

if [[ -z "${AGENTDECK_TELEGRAM_ALLOWED_CHATS:-}" ]]; then
  if [[ ! -f "$TELEAGENT_CONFIG" ]]; then
    echo "Missing TeleAgent config: $TELEAGENT_CONFIG" >&2
    exit 1
  fi
  AGENTDECK_TELEGRAM_ALLOWED_CHATS="$(
    python -c '
import re
from pathlib import Path
text = Path("'"$TELEAGENT_CONFIG"'").read_text(encoding="utf-8")
match = re.search(r"allowed_chat_ids\s*=\s*\[([^\]]*)\]", text)
print(",".join(re.findall(r"-?\d+", match.group(1))) if match else "")
'
  )"
  export AGENTDECK_TELEGRAM_ALLOWED_CHATS
fi

python -c '
import os
import re
import sys

token = os.environ.get("AGENTDECK_TELEGRAM_TOKEN", "")
chats = os.environ.get("AGENTDECK_TELEGRAM_ALLOWED_CHATS", "")
token_ok = bool(re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token))
chats_ok = bool(re.fullmatch(r"-?[0-9]+(,-?[0-9]+)*", chats))
print("token_ok:", token_ok)
print("chat_id_ok:", chats_ok)
print("chat_id:", chats)
if not token_ok:
    print("Invalid AGENTDECK_TELEGRAM_TOKEN", file=sys.stderr)
    sys.exit(1)
if not chats_ok:
    print("Invalid AGENTDECK_TELEGRAM_ALLOWED_CHATS", file=sys.stderr)
    sys.exit(1)
'

echo
echo "Workspace: $WORKSPACE"
echo "Project:   $PROJECT_ID"
echo "Agent:     $AGENT_ID"
echo

PYTHONPATH=src \
python -m agentdeck \
  --workspace "$WORKSPACE" \
  init

PYTHONPATH=src \
python -m agentdeck \
  --workspace "$WORKSPACE" \
  projects create "$PROJECT_ID" \
  --title "$PROJECT_TITLE" \
  --cwd "$PROJECT_CWD" \
  --default-agent "$AGENT_ID" \
  --replace

PYTHONPATH=src \
python -m agentdeck \
  --workspace "$WORKSPACE" \
  agents create "$AGENT_ID" \
  --title "$AGENT_TITLE" \
  --project "$PROJECT_ID" \
  --adapter echo \
  --cwd "$PROJECT_CWD" \
  --replace

PYTHONPATH=src \
python -m agentdeck \
  --workspace "$WORKSPACE" \
  tasks create "Test task A" \
  --project "$PROJECT_ID" \
  --agent "$AGENT_ID"

PYTHONPATH=src \
python -m agentdeck \
  --workspace "$WORKSPACE" \
  tasks create "Test task B" \
  --project "$PROJECT_ID" \
  --agent "$AGENT_ID"

echo
echo "Local task list:"
PYTHONPATH=src \
python -m agentdeck \
  --workspace "$WORKSPACE" \
  tasks list

echo
echo "Telegram test commands:"
echo "  /list"
echo "  /use 1"
echo "  /run 1 hello from numbered task"
echo "  /list"
echo "  /job 1"
echo
echo "Starting Telegram bot."
echo "Keep this terminal open. Press Ctrl-D on an empty line to stop cleanly."
echo

(
  trap '' INT
  exec env PYTHONPATH=src \
  python -m agentdeck \
    --workspace "$WORKSPACE" \
    telegram serve
) &
BOT_PID=$!

send_exit_notice() {
  python - "$WORKSPACE" <<'PY' >/dev/null 2>&1 || true
import os
import sys
import urllib.parse
import urllib.request

workspace = sys.argv[1]
token = os.environ.get("AGENTDECK_TELEGRAM_TOKEN", "").strip()
chats = [item.strip() for item in os.environ.get("AGENTDECK_TELEGRAM_ALLOWED_CHATS", "").split(",") if item.strip()]
if not token or not chats:
    raise SystemExit(0)
text = "AgentDeck test bot is stopping after Ctrl-D.\nworkspace: " + workspace
body_base = {
    "text": text,
    "disable_web_page_preview": "true",
}
for chat_id in chats:
    body = dict(body_base)
    body["chat_id"] = chat_id
    data = urllib.parse.urlencode(body).encode("utf-8")
    urllib.request.urlopen(
        urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
        timeout=10,
    ).read()
PY
}

cleanup() {
  local exit_code=$?
  trap - EXIT
  if kill -0 "$BOT_PID" 2>/dev/null; then
    echo
    echo "Stopping Telegram bot..."
    kill "$BOT_PID" 2>/dev/null || true
    wait "$BOT_PID" 2>/dev/null || true
  fi
  exit "$exit_code"
}

trap cleanup EXIT
trap 'echo; echo "Use Ctrl-D to stop cleanly."' INT

echo "Telegram bot pid: $BOT_PID"
echo

while IFS= read -r _; do
  :
done

echo
echo "Ctrl-D received."
send_exit_notice
