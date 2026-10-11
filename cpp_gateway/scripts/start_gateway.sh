#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${GATEWAY_DIR}/.." && pwd)"

cd "${GATEWAY_DIR}"

source "${REPO_ROOT}/scripts/env.sh"
load_dotenv "${REPO_ROOT}/.env"

GATEWAY_BUILD_DIR="${GATEWAY_BUILD_DIR:-${REPO_ROOT}/cpp_gateway/build}"
if [[ "$GATEWAY_BUILD_DIR" != /* ]]; then
  GATEWAY_BUILD_DIR="${REPO_ROOT}/${GATEWAY_BUILD_DIR}"
fi

export REPO_ROOT
export PYTHON_INTERNAL_BASE_URL="${PYTHON_INTERNAL_BASE_URL:-http://127.0.0.1:8000}"

if [ ! -x "${GATEWAY_BUILD_DIR}/cpp_gateway" ]; then
  echo "[ERROR] gateway binary not found: ${GATEWAY_BUILD_DIR}/cpp_gateway" >&2
  echo "        Run: cmake -S cpp_gateway -B ${GATEWAY_BUILD_DIR} && cmake --build ${GATEWAY_BUILD_DIR}" >&2
  exit 1
fi

exec "${GATEWAY_BUILD_DIR}/cpp_gateway"
