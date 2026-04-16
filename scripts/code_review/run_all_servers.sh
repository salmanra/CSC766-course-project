#!/usr/bin/env bash
# Launch all four code_review tool servers in the background and kill them
# cleanly when this script exits.
#
# Usage:
#   bash scripts/code_review/run_all_servers.sh
#   bash scripts/code_review/run_all_servers.sh --backend local
#
# Environment:
#   PYTHON       — python interpreter to use (default: python3)
#   PARSER_PORT  / LINTER_PORT / SCANNER_PORT / SUMMARIZER_PORT — port overrides
#   SUMMARIZER_BACKEND — "template" (default) or "local"

set -euo pipefail

PYTHON="${PYTHON:-python3}"
PARSER_PORT="${PARSER_PORT:-8101}"
LINTER_PORT="${LINTER_PORT:-8102}"
SCANNER_PORT="${SCANNER_PORT:-8103}"
SUMMARIZER_PORT="${SUMMARIZER_PORT:-8104}"
SUMMARIZER_BACKEND="${SUMMARIZER_BACKEND:-template}"

# Parse optional --backend flag for convenience.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)
      SUMMARIZER_BACKEND="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVERS_DIR="${REPO_ROOT}/server/code_review"
LOGS_DIR="${REPO_ROOT}/profiler_logs"
mkdir -p "${LOGS_DIR}"

PIDS=()

cleanup() {
  echo
  echo "[run_all_servers] stopping servers: ${PIDS[*]}"
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[run_all_servers] repo_root=${REPO_ROOT}"
echo "[run_all_servers] parser    -> :${PARSER_PORT}"
echo "[run_all_servers] linter    -> :${LINTER_PORT}"
echo "[run_all_servers] scanner   -> :${SCANNER_PORT}"
echo "[run_all_servers] summarizer-> :${SUMMARIZER_PORT}  (backend=${SUMMARIZER_BACKEND})"
echo

"${PYTHON}" "${SERVERS_DIR}/parser_server.py"    --port "${PARSER_PORT}"    &
PIDS+=("$!")

"${PYTHON}" "${SERVERS_DIR}/linter_server.py"    --port "${LINTER_PORT}" \
  --parser-url "http://127.0.0.1:${PARSER_PORT}" &
PIDS+=("$!")

"${PYTHON}" "${SERVERS_DIR}/scanner_server.py"   --port "${SCANNER_PORT}" \
  --parser-url "http://127.0.0.1:${PARSER_PORT}" &
PIDS+=("$!")

"${PYTHON}" "${SERVERS_DIR}/summarizer_server.py" --port "${SUMMARIZER_PORT}" \
  --backend "${SUMMARIZER_BACKEND}" &
PIDS+=("$!")

echo "[run_all_servers] PIDs: ${PIDS[*]}"
echo "[run_all_servers] Press Ctrl+C to stop all servers."
wait
