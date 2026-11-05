#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS_DIR="$ROOT/logs"
ORCH_OUT="$LOGS_DIR/start-orchestrator.out.log"
ORCH_ERR="$LOGS_DIR/start-orchestrator.err.log"
WEB_OUT="$LOGS_DIR/start-web-sidecar.out.log"
WEB_ERR="$LOGS_DIR/start-web-sidecar.err.log"

USE_DOCKER="${VIBE_USE_DOCKER:-true}"
USE_TUNNEL="${VIBE_USE_TUNNEL:-false}"
PUBLIC_HOST="${VIBE_PUBLIC_HOSTNAME:-vibeitz.com}"

ORCH_PID=""
WEB_PID=""
DOCKER_STARTED="false"

step() {
    printf '\033[36m==> %s\033[0m\n' "$1"
}

note() {
    printf '    %s\n' "$1"
}

wait_http_ok() {
    local url="$1"
    local seconds="$2"
    local name="$3"
    local status

    for _ in $(seq 1 "$seconds"); do
        status="$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)"
        if [[ "$status" =~ ^[234][0-9][0-9]$ ]]; then
            note "$name healthy ($status)"
            return 0
        fi
        sleep 1
    done
    return 1
}

ensure_venv() {
    local dir="$1"
    local requirements="$2"
    local python="$dir/bin/python"

    if [[ ! -x "$python" ]]; then
        step "Creating venv: $dir"
        python3 -m venv "$dir"
    else
        step "Updating venv: $dir"
    fi

    "$python" -m pip install --upgrade pip wheel setuptools
    "$python" -m pip install -r "$requirements"
}

pids_on_port() {
    local port="$1"

    if command -v lsof >/dev/null 2>&1; then
        lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
        return
    fi

    if command -v fuser >/dev/null 2>&1; then
        fuser -n tcp "$port" 2>/dev/null || true
        return
    fi

    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null | awk -v port=":$port" '
            $4 ~ port "$" {
                while (match($0, /pid=[0-9]+/)) {
                    print substr($0, RSTART + 4, RLENGTH - 4)
                    $0 = substr($0, RSTART + RLENGTH)
                }
            }
        '
    fi
}

stop_pids() {
    local unique alive
    mapfile -t unique < <(
        printf '%s\n' "$@" |
            awk -v self="$$" -v parent="$PPID" 'NF && $1 != self && $1 != parent && !seen[$1]++'
    )

    if [[ "${#unique[@]}" -eq 0 ]]; then
        note "no stale runtime found"
        return
    fi

    note "stopping stale pids: ${unique[*]}"
    kill -TERM "${unique[@]}" 2>/dev/null || true
    sleep 2

    alive=()
    for pid in "${unique[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            alive+=("$pid")
        fi
    done
    if [[ "${#alive[@]}" -gt 0 ]]; then
        note "force stopping stale pids: ${alive[*]}"
        kill -KILL "${alive[@]}" 2>/dev/null || true
    fi
}

stop_stale_runtime() {
    local pids=()
    local pid

    while read -r pid _; do
        [[ -n "$pid" ]] && pids+=("$pid")
    done < <(pgrep -af "bash (./|$ROOT/)start[.]sh" 2>/dev/null || true)

    while read -r pid; do
        [[ -n "$pid" ]] && pids+=("$pid")
    done < <(pgrep -f "$ROOT/.venv/bin/python server.py" 2>/dev/null || true)

    while read -r pid; do
        [[ -n "$pid" ]] && pids+=("$pid")
    done < <(pgrep -f "$ROOT/.venv-web/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8104" 2>/dev/null || true)

    while read -r pid; do
        [[ -n "$pid" ]] && pids+=("$pid")
    done < <(pids_on_port 8100)

    while read -r pid; do
        [[ -n "$pid" ]] && pids+=("$pid")
    done < <(pids_on_port 8104)

    stop_pids "${pids[@]}"
}

cleanup() {
    local exit_code=$?
    echo
    step "Stopping vibe-rebuild"

    if [[ -n "$WEB_PID" ]] && kill -0 "$WEB_PID" 2>/dev/null; then
        kill "$WEB_PID" 2>/dev/null || true
    fi
    if [[ -n "$ORCH_PID" ]] && kill -0 "$ORCH_PID" 2>/dev/null; then
        kill "$ORCH_PID" 2>/dev/null || true
    fi

    wait "$WEB_PID" 2>/dev/null || true
    wait "$ORCH_PID" 2>/dev/null || true

    if [[ "$DOCKER_STARTED" == "true" ]]; then
        step "Stopping docker services (pdf-sidecar + open-webui)"
        docker compose -f "$ROOT/services/docker-compose.yml" down || true
    fi

    exit "$exit_code"
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf 'Missing required command: %s\n' "$1" >&2
        return 1
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$ROOT"
mkdir -p "$LOGS_DIR"
mkdir -p "$ROOT/data/open-webui"
rm -f "$ORCH_OUT" "$ORCH_ERR" "$WEB_OUT" "$WEB_ERR"

require_command python3
require_command curl

ensure_venv "$ROOT/.venv" "$ROOT/orchestrator/requirements.txt"
ensure_venv "$ROOT/.venv-web" "$ROOT/services/web-sidecar/requirements.txt"

if [[ ! -d "$HOME/.cache/ms-playwright/chromium_headless_shell-1217" ]]; then
    step "Installing Playwright Chromium"
    "$ROOT/.venv/bin/python" -m playwright install chromium || \
        note "Playwright install failed; screenshot review may fall back to text review."
fi

TOKEN_FILE="$ROOT/.vibe_token"
if [[ -z "${PORT_PROJECT_INTERNAL_TOKEN:-}" ]]; then
    if [[ -f "$TOKEN_FILE" ]]; then
        PORT_PROJECT_INTERNAL_TOKEN="$("$ROOT/.venv/bin/python" -c 'from pathlib import Path; print(Path(".vibe_token").read_text(encoding="utf-8-sig").strip())')"
    else
        PORT_PROJECT_INTERNAL_TOKEN="$("$ROOT/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
        printf '%s' "$PORT_PROJECT_INTERNAL_TOKEN" > "$TOKEN_FILE"
        chmod 600 "$TOKEN_FILE" || true
    fi
fi
export PORT_PROJECT_INTERNAL_TOKEN
export VIBE_INTERNAL_TOKEN="${VIBE_INTERNAL_TOKEN:-$PORT_PROJECT_INTERNAL_TOKEN}"
if [[ -z "${VIBE_PUBLIC_BASE_URL:-}" ]]; then
    if [[ "$USE_TUNNEL" == "true" ]]; then
        export VIBE_PUBLIC_BASE_URL="https://$PUBLIC_HOST"
    else
        export VIBE_PUBLIC_BASE_URL="http://127.0.0.1:8100"
    fi
else
export VIBE_PUBLIC_BASE_URL
fi
export VIBE_TOOL_API_PUBLIC_BASE_URL="${VIBE_TOOL_API_PUBLIC_BASE_URL:-http://host.docker.internal:8100/tool-api}"

step "Stopping stale processes from a previous run"
stop_stale_runtime

if [[ "$USE_DOCKER" != "false" ]]; then
    require_command docker
    step "Starting pdf-sidecar + open-webui via docker compose"
    docker compose -f "$ROOT/services/docker-compose.yml" up -d --build
    DOCKER_STARTED="true"
fi

step "Starting orchestrator on http://127.0.0.1:8100"
(
    cd "$ROOT/orchestrator"
    exec "$ROOT/.venv/bin/python" server.py
) >"$ORCH_OUT" 2>"$ORCH_ERR" &
ORCH_PID="$!"

if ! wait_http_ok "http://127.0.0.1:8100/healthz" 30 "orchestrator"; then
    echo "orchestrator did not respond at /healthz. Check $ORCH_ERR" >&2
    tail -n 20 "$ORCH_ERR" >&2 || true
    exit 1
fi

step "Starting web-sidecar on http://127.0.0.1:8104"
(
    cd "$ROOT/services/web-sidecar"
    exec "$ROOT/.venv-web/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8104
) >"$WEB_OUT" 2>"$WEB_ERR" &
WEB_PID="$!"

if ! wait_http_ok "http://127.0.0.1:8104/healthz" 30 "web-sidecar"; then
    echo "web-sidecar did not respond at /healthz. Check $WEB_ERR" >&2
    tail -n 20 "$WEB_ERR" >&2 || true
    exit 1
fi

if [[ "$DOCKER_STARTED" == "true" ]]; then
    wait_http_ok "http://127.0.0.1:8102/health" 60 "pdf-sidecar" || \
        note "pdf-sidecar /health did not come up. Check: docker compose -f services/docker-compose.yml logs pdf-sidecar"
    wait_http_ok "http://127.0.0.1:3100/health" 90 "open-webui" || \
        note "open-webui /health did not come up yet. Check: docker compose -f services/docker-compose.yml logs open-webui"
fi

if [[ "$USE_TUNNEL" == "true" ]]; then
    if command -v cloudflared >/dev/null 2>&1; then
        note "cloudflared is installed, but Ubuntu start.sh does not manage tunnel credentials yet."
        note "Run cloudflared separately if you need https://$PUBLIC_HOST."
    else
        note "cloudflared not found; skipping tunnel."
    fi
fi

echo
printf '\033[32mvibe-rebuild is running.\033[0m\n'
echo "  open-webui   : http://127.0.0.1:3100"
echo "  orchestrator : http://127.0.0.1:8100  (/v1/* + /api/*)"
echo "  web-sidecar  : http://127.0.0.1:8104"
if [[ "$DOCKER_STARTED" == "true" ]]; then
    echo "  pdf-sidecar  : http://127.0.0.1:8102  (docker)"
fi
echo "  logs         : $LOGS_DIR"
echo
echo "Press Ctrl+C to stop everything."
echo

while true; do
    sleep 2
    if ! kill -0 "$ORCH_PID" 2>/dev/null; then
        echo "orchestrator exited. Check $ORCH_ERR" >&2
        exit 1
    fi
    if ! kill -0 "$WEB_PID" 2>/dev/null; then
        echo "web-sidecar exited. Check $WEB_ERR" >&2
        exit 1
    fi
done
