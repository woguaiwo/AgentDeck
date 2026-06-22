#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${AGENTDECK_PYTHON:-python}"
WRITE_SHELL_CONFIG=0
RUN_TESTS=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [--shell-config] [--run-tests] [--python /path/to/python]

Installs AgentDeck from this source tree in editable mode and initializes the
default platform workspace at <AgentDeck>/.agentdeck.

Options:
  --shell-config      Append the Python scripts directory to ~/.bashrc or ~/.zshrc
                      if it is not already on PATH.
  --run-tests         Run the unit test suite after installing.
  --python PATH       Python interpreter to use. Defaults to AGENTDECK_PYTHON or python.
  -h, --help          Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --shell-config)
      WRITE_SHELL_CONFIG=1
      shift
      ;;
    --run-tests)
      RUN_TESTS=1
      shift
      ;;
    --python)
      if [[ $# -lt 2 ]]; then
        echo "missing value for --python" >&2
        exit 2
      fi
      PYTHON_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "python not found: $PYTHON_BIN" >&2
  exit 2
fi

cd "$ROOT_DIR"

SCRIPTS_DIR="$("$PYTHON_BIN" - <<'PY'
import sysconfig
print(sysconfig.get_path("scripts"))
PY
)"

echo "AgentDeck source: $ROOT_DIR"
echo "Python: $("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"
echo "Scripts dir: $SCRIPTS_DIR"

"$PYTHON_BIN" -m pip install -e "$ROOT_DIR"

if [[ ":$PATH:" != *":$SCRIPTS_DIR:"* ]]; then
  echo
  echo "Note: scripts dir is not on PATH for this shell:"
  echo "  $SCRIPTS_DIR"
  echo "For this shell, run:"
  echo "  export PATH=\"$SCRIPTS_DIR:\$PATH\""
  if [[ "$WRITE_SHELL_CONFIG" -eq 1 ]]; then
    SHELL_RC="$HOME/.bashrc"
    if [[ "${SHELL:-}" == */zsh ]]; then
      SHELL_RC="$HOME/.zshrc"
    fi
    mkdir -p "$(dirname "$SHELL_RC")"
    if ! grep -F "AgentDeck installer" "$SHELL_RC" >/dev/null 2>&1; then
      {
        echo ""
        echo "# AgentDeck installer"
        echo "export PATH=\"$SCRIPTS_DIR:\$PATH\""
      } >> "$SHELL_RC"
      echo "Updated shell config: $SHELL_RC"
      echo "Open a new shell or run: source $SHELL_RC"
    else
      echo "Shell config already contains an AgentDeck installer block: $SHELL_RC"
    fi
  fi
fi

"$PYTHON_BIN" -m agentdeck init
"$PYTHON_BIN" -m agentdeck doctor

if command -v agentdeck >/dev/null 2>&1; then
  agentdeck doctor >/dev/null
  echo
  echo "Installed command: $(command -v agentdeck)"
else
  echo
  echo "agentdeck command is not on PATH yet. You can use:"
  echo "  $PYTHON_BIN -m agentdeck"
fi

if [[ "$RUN_TESTS" -eq 1 ]]; then
  "$PYTHON_BIN" -m unittest discover -s tests
fi

echo
echo "Done. Start Telegram with:"
echo "  agentdeck telegram start"
