#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

source "${REPO_ROOT}/scripts/env.sh"
load_dotenv "${REPO_ROOT}/.env"

GATEWAY_BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:8080}"
TEST_FILE="${1:-./README.md}"
USER_ID="${USER_ID:-1}"

if [ ! -f "$TEST_FILE" ]; then
  echo "[ERROR] file not found: $TEST_FILE" >&2
  exit 1
fi

mapfile -t AUTH_HEADERS < <(build_gateway_auth_headers)

echo "==> upload document"
UPLOAD_RESP="$(curl -fsS -X POST "${GATEWAY_BASE_URL}/v1/documents" \
  "${AUTH_HEADERS[@]}" \
  -F "user_id=${USER_ID}" \
  -F "file=@${TEST_FILE}")"
echo "$UPLOAD_RESP"

DOC_ID="$(printf "%s" "$UPLOAD_RESP" | json_read "data.get('data', data)['doc_id']")"
TASK_ID="$(printf "%s" "$UPLOAD_RESP" | json_read "data.get('data', data)['task_id']")"
echo "doc_id=$DOC_ID"
echo "task_id=$TASK_ID"

echo "==> poll task"
poll_gateway_task "$GATEWAY_BASE_URL" "$TASK_ID" "ingest" 90 "${AUTH_HEADERS[@]}"
