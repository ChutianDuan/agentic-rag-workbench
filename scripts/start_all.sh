#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${REPO_ROOT}/.run"
LOG_DIR="${REPO_ROOT}/logs"

cd "$REPO_ROOT"

source "${REPO_ROOT}/scripts/env.sh"
load_dotenv "${REPO_ROOT}/.env"

GATEWAY_BUILD_DIR="${GATEWAY_BUILD_DIR:-${REPO_ROOT}/cpp_gateway/build}"
if [[ "$GATEWAY_BUILD_DIR" != /* ]]; then
  GATEWAY_BUILD_DIR="${REPO_ROOT}/${GATEWAY_BUILD_DIR}"
fi
export GATEWAY_BUILD_DIR

mkdir -p "$RUN_DIR" "$LOG_DIR"

START_FRONTEND="${START_FRONTEND:-false}"
START_GATEWAY="${START_GATEWAY:-true}"
START_INIT_DB="${START_INIT_DB:-false}"
START_WAIT_SECONDS="${START_WAIT_SECONDS:-20}"
STOP_WAIT_SECONDS="${STOP_WAIT_SECONDS:-8}"
LOG_LINES="${LOG_LINES:-80}"
FOLLOW_LOGS="${FOLLOW_LOGS:-false}"

loopback_host() {
  case "${1:-}" in
    ""|0.0.0.0|::) printf '%s' "127.0.0.1" ;;
    *) printf '%s' "$1" ;;
  esac
}

API_BASE_URL="${PYTHON_BASE_URL:-http://$(loopback_host "${APP_HOST:-0.0.0.0}"):${APP_PORT:-8000}}"
GATEWAY_BASE_URL="${GATEWAY_BASE_URL:-http://$(loopback_host "${GATEWAY_LISTEN_HOST:-0.0.0.0}"):${GATEWAY_LISTEN_PORT:-8080}}"
FRONTEND_BASE_URL="${FRONTEND_BASE_URL:-http://127.0.0.1:${FRONTEND_PORT:-5173}}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/start_all.sh [start|stop|restart|status|logs] [all|api|worker|gateway|frontend]
  bash scripts/start_all.sh e2e [test-file]

Examples:
  START_FRONTEND=true bash scripts/start_all.sh start
  bash scripts/start_all.sh restart api
  bash scripts/start_all.sh status
  bash scripts/start_all.sh logs gateway

Environment:
  START_FRONTEND=true   include Vite in the all target
  START_GATEWAY=false  omit Gateway from the all target
  START_INIT_DB=true    initialize MySQL before starting all
  FRONTEND_PORT=5173    Vite listen port
  LOG_LINES=80          lines printed by logs
  FOLLOW_LOGS=true      follow logs after printing existing lines
EOF
}

pid_file_for() {
  printf '%s' "${RUN_DIR}/$1.pid"
}

log_file_for() {
  printf '%s' "${LOG_DIR}/$1.log"
}

service_pid() {
  local pid_file
  pid_file="$(pid_file_for "$1")"
  [ -f "$pid_file" ] || return 1

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$pid"
}

is_running() {
  local pid
  pid="$(service_pid "$1")" || return 1
  kill -0 "$pid" 2>/dev/null
}

start_service() {
  local name="$1"
  shift

  local pid_file log_file pid
  pid_file="$(pid_file_for "$name")"
  log_file="$(log_file_for "$name")"

  if is_running "$name"; then
    echo "[SKIP] ${name} already running pid=$(service_pid "$name")"
    return 0
  fi

  rm -f "$pid_file"
  echo "[START] ${name} -> ${log_file}"
  setsid "$@" >"$log_file" 2>&1 < /dev/null &
  pid="$!"
  printf '%s\n' "$pid" >"$pid_file"

  for _ in $(seq 1 10); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[ERROR] ${name} exited during startup; last log lines:" >&2
      tail -n 20 "$log_file" >&2 || true
      rm -f "$pid_file"
      return 1
    fi
    sleep 0.1
  done

  echo "[OK] ${name} running pid=${pid}"
}

stop_service() {
  local name="$1"
  local pid_file pid
  pid_file="$(pid_file_for "$name")"

  if ! is_running "$name"; then
    rm -f "$pid_file"
    echo "[SKIP] ${name} not running"
    return 0
  fi

  pid="$(service_pid "$name")"
  echo "[STOP] ${name} pid=${pid}"
  kill -- "-${pid}" 2>/dev/null || kill "$pid" 2>/dev/null || true

  for _ in $(seq 1 "$STOP_WAIT_SECONDS"); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      echo "[OK] ${name} stopped"
      return 0
    fi
    sleep 1
  done

  echo "[WARN] ${name} did not stop in ${STOP_WAIT_SECONDS}s; sending KILL" >&2
  kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  rm -f "$pid_file"
}

wait_for_http() {
  local name="$1"
  local url="$2"

  if ! command -v curl >/dev/null 2>&1; then
    echo "[WARN] curl unavailable; cannot verify ${name} readiness" >&2
    return 0
  fi

  for _ in $(seq 1 "$START_WAIT_SECONDS"); do
    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 1 "$url" 2>/dev/null || true)"
    if [ -n "$code" ] && [ "$code" != "000" ]; then
      echo "[OK] ${name} listening (${code}) ${url}"
      return 0
    fi
    sleep 1
  done

  echo "[ERROR] ${name} did not answer within ${START_WAIT_SECONDS}s: ${url}" >&2
  return 1
}

http_status() {
  local url="$1"
  if ! command -v curl >/dev/null 2>&1; then
    printf '%s' "curl unavailable"
    return
  fi

  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || true)"
  case "$code" in
    2??) printf 'HTTP %s' "$code" ;;
    000|"") printf '%s' "unreachable" ;;
    *) printf 'HTTP %s (degraded)' "$code" ;;
  esac
}

status_service() {
  local name="$1"
  local health_url="${2:-}"
  if is_running "$name"; then
    printf '[OK] %-8s pid=%s' "$name" "$(service_pid "$name")"
    if [ -n "$health_url" ]; then
      printf ' health=%s' "$(http_status "$health_url")"
    fi
    printf '\n'
  else
    rm -f "$(pid_file_for "$name")"
    printf '[--] %-8s stopped\n' "$name"
  fi
}

ensure_gateway_binary() {
  if [ -x "${GATEWAY_BUILD_DIR}/cpp_gateway" ]; then
    return 0
  fi

  configure_vcpkg_defaults

  if ! command -v cmake >/dev/null 2>&1; then
    echo "[ERROR] Gateway binary is missing and cmake is unavailable" >&2
    return 1
  fi

  echo "[BUILD] cpp_gateway"
  local cmake_args=()
  if [ -n "${CMAKE_TOOLCHAIN_FILE:-}" ]; then
    cmake_args+=("-DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}")
  fi
  if [ -n "${Drogon_DIR:-}" ]; then
    cmake_args+=("-DDrogon_DIR=${Drogon_DIR}")
  fi
  cmake -S cpp_gateway -B "$GATEWAY_BUILD_DIR" \
    -DCMAKE_BUILD_TYPE="${CMAKE_BUILD_TYPE:-Debug}" \
    "${cmake_args[@]}"
  cmake --build "$GATEWAY_BUILD_DIR" -j "${BUILD_JOBS:-4}"
}

start_named() {
  case "$1" in
    api)
      start_service api bash scripts/start_api.sh
      if ! wait_for_http api "${API_BASE_URL}/openapi.json"; then
        stop_service api
        return 1
      fi
      ;;
    worker)
      start_service worker bash scripts/start_worker.sh
      ;;
    gateway)
      ensure_gateway_binary
      start_service gateway bash cpp_gateway/scripts/start_gateway.sh
      if ! wait_for_http gateway "${GATEWAY_BASE_URL}/health"; then
        stop_service gateway
        return 1
      fi
      ;;
    frontend)
      if ! command -v npm >/dev/null 2>&1; then
        echo "[ERROR] npm is required to start the frontend" >&2
        return 1
      fi
      if [ ! -d "${REPO_ROOT}/frontend/node_modules" ]; then
        echo "[ERROR] frontend dependencies are missing; run: cd frontend && npm install" >&2
        return 1
      fi
      start_service frontend bash -c \
        "cd \"${REPO_ROOT}/frontend\" && exec npm run dev -- --host 0.0.0.0 --port \"${FRONTEND_PORT:-5173}\" --strictPort"
      if ! wait_for_http frontend "${FRONTEND_BASE_URL}/"; then
        stop_service frontend
        return 1
      fi
      ;;
  esac
}

start_target() {
  local target="$1"
  if [ "$target" != "all" ]; then
    start_named "$target"
    return
  fi

  if is_true "$START_INIT_DB"; then
    bash scripts/init_db.sh
  fi

  start_named api
  start_named worker
  if is_true "$START_GATEWAY"; then
    start_named gateway
  fi
  if is_true "$START_FRONTEND"; then
    start_named frontend
  fi
}

stop_target() {
  local target="$1"
  if [ "$target" != "all" ]; then
    stop_service "$target"
    return
  fi

  stop_service frontend
  stop_service gateway
  stop_service worker
  stop_service api
}

status_target() {
  case "$1" in
    all)
      status_service api "${API_BASE_URL}/internal/health"
      status_service worker
      status_service gateway "${GATEWAY_BASE_URL}/health"
      status_service frontend "${FRONTEND_BASE_URL}/"
      ;;
    api) status_service api "${API_BASE_URL}/internal/health" ;;
    worker) status_service worker ;;
    gateway) status_service gateway "${GATEWAY_BASE_URL}/health" ;;
    frontend) status_service frontend "${FRONTEND_BASE_URL}/" ;;
  esac
}

show_logs() {
  local target="$1"
  local files=()

  if [ "$target" = "all" ]; then
    local service
    for service in api worker gateway frontend; do
      if [ -f "$(log_file_for "$service")" ]; then
        files+=("$(log_file_for "$service")")
      fi
    done
  else
    files+=("$(log_file_for "$target")")
  fi

  if [ "${#files[@]}" -eq 0 ]; then
    echo "[INFO] no service logs found in ${LOG_DIR}"
    return 0
  fi

  tail -n "$LOG_LINES" "${files[@]}"
  if is_true "$FOLLOW_LOGS"; then
    tail -n 0 -f "${files[@]}"
  fi
}

print_endpoints() {
  echo "[DONE] stack command completed"
  echo "       Workbench: ${FRONTEND_BASE_URL}"
  echo "       Gateway:   ${GATEWAY_BASE_URL}/health"
  echo "       FastAPI:   ${API_BASE_URL}/internal/health"
  echo "       Logs:      bash scripts/start_all.sh logs"
}

ACTION="${1:-start}"
shift || true

if [ "$ACTION" = "e2e" ]; then
  exec bash scripts/e2e_all.sh "$@"
fi

TARGET="${1:-all}"
if [ "$#" -gt 0 ]; then
  shift
fi

if [ "$#" -gt 0 ]; then
  usage >&2
  exit 2
fi

case "$TARGET" in
  all|api|worker|gateway|frontend) ;;
  *)
    echo "[ERROR] unknown target: ${TARGET}" >&2
    usage >&2
    exit 2
    ;;
esac

case "$ACTION" in
  start)
    start_target "$TARGET"
    print_endpoints
    ;;
  stop)
    stop_target "$TARGET"
    ;;
  restart)
    stop_target "$TARGET"
    start_target "$TARGET"
    print_endpoints
    ;;
  status)
    status_target "$TARGET"
    ;;
  logs)
    show_logs "$TARGET"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "[ERROR] unknown action: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac
