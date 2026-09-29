#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

source "${REPO_ROOT}/scripts/env.sh"
load_dotenv "${REPO_ROOT}/.env"

PYTHON_BASE_URL="${PYTHON_BASE_URL:-http://127.0.0.1:8000}"
GATEWAY_BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:8080}"
TEST_FILE="${1:-./README.md}"
TOP_K="${TOP_K:-3}"
QUERY_TEXT="${QUERY_TEXT:-这份文档讲了什么？}"

if [ ! -f "$TEST_FILE" ]; then
  echo "[ERROR] file not found: $TEST_FILE" >&2
  exit 1
fi

mapfile -t AUTH_HEADERS < <(build_gateway_auth_headers)

section() {
  echo "============================================================"
  echo "$1"
}

section "[1/7] create e2e user"
USERNAME="e2e_$(date +%Y%m%d_%H%M%S)_$$"
USER_RESP="$(curl -fsS -X POST "${GATEWAY_BASE_URL}/v1/users" \
  "${AUTH_HEADERS[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"${USERNAME}\"}")"
echo "$USER_RESP"
USER_ID="$(printf "%s" "$USER_RESP" | json_read "data['data']['id']")"
echo "[INFO] user_id=${USER_ID}"

section "[2/7] upload document via gateway"
UPLOAD_RESP="$(curl -fsS -X POST "${GATEWAY_BASE_URL}/v1/documents" \
  "${AUTH_HEADERS[@]}" \
  -F "user_id=${USER_ID}" \
  -F "file=@${TEST_FILE}")"
echo "$UPLOAD_RESP"
DOC_ID="$(printf "%s" "$UPLOAD_RESP" | json_read "data.get('data', data)['doc_id']")"
INGEST_TASK_ID="$(printf "%s" "$UPLOAD_RESP" | json_read "data.get('data', data)['task_id']")"
echo "[INFO] doc_id=${DOC_ID}"
echo "[INFO] ingest_task_id=${INGEST_TASK_ID}"

section "[3/7] wait ingest"
poll_gateway_task "$GATEWAY_BASE_URL" "$INGEST_TASK_ID" "ingest" 90 "${AUTH_HEADERS[@]}"

section "[4/7] create session"
SESSION_RESP="$(curl -fsS -X POST "${GATEWAY_BASE_URL}/v1/sessions" \
  "${AUTH_HEADERS[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":${USER_ID},\"title\":\"E2E Session ${USERNAME}\"}")"
echo "$SESSION_RESP"
SESSION_ID="$(printf "%s" "$SESSION_RESP" | json_read "data['data']['session_id']")"
echo "[INFO] session_id=${SESSION_ID}"

section "[5/7] submit chat"
CHAT_RESP="$(curl -fsS -X POST "${GATEWAY_BASE_URL}/v1/sessions/${SESSION_ID}/messages" \
  "${AUTH_HEADERS[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"content\":\"${QUERY_TEXT}\",\"top_k\":${TOP_K}}")"
echo "$CHAT_RESP"
CHAT_TASK_ID="$(printf "%s" "$CHAT_RESP" | json_read "data['data']['task_id']")"
echo "[INFO] chat_task_id=${CHAT_TASK_ID}"

section "[6/7] wait chat and inspect messages"
poll_gateway_task "$GATEWAY_BASE_URL" "$CHAT_TASK_ID" "chat" 90 "${AUTH_HEADERS[@]}"
MESSAGES_RESP="$(curl -fsS "${AUTH_HEADERS[@]}" "${GATEWAY_BASE_URL}/v1/sessions/${SESSION_ID}/messages")"
echo "$MESSAGES_RESP"

FIRST_CHUNK_ID="$(printf "%s" "$MESSAGES_RESP" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
messages = (payload.get("data") or {}).get("items") or []
for message in messages:
    for citation in message.get("citations") or []:
        chunk_id = citation.get("chunk_id")
        if chunk_id:
            print(chunk_id)
            raise SystemExit(0)
print("")
')"

if [ -n "$FIRST_CHUNK_ID" ]; then
  echo "[INFO] run retrieval eval with relevant_chunk_id=${FIRST_CHUNK_ID}"
  curl -fsS -X POST "${PYTHON_BASE_URL}/internal/search" \
    -H "Content-Type: application/json" \
    -d "{\"doc_id\":${DOC_ID},\"query\":\"${QUERY_TEXT}\",\"top_k\":${TOP_K},\"relevant_chunk_ids\":[${FIRST_CHUNK_ID}]}"
  echo
fi

section "[7/7] monitor overview"
curl -fsS "${AUTH_HEADERS[@]}" "${GATEWAY_BASE_URL}/v1/monitor/overview"
echo
echo "[DONE] end-to-end flow succeeded"
