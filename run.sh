#!/usr/bin/env bash
# Levanta bot + API + dashboard con un solo comando.
#
#   ./run.sh                  demo (datos sintéticos, sin red ni API keys)
#   ./run.sh paper            datos reales de Binance, ejecución simulada
#   ./run.sh live             DINERO REAL (requiere config/live.toml + confirmación)
#   ./run.sh demo --dev       además levanta Vite con hot reload en :5173
#   ./run.sh paper --docker   lo mismo, pero con docker compose
#
# Dashboard: http://127.0.0.1:8765 (solo localhost). El token está en trading_bot/.env.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT="$ROOT/trading_bot"
DASH="$ROOT/dashboard"
ENV_FILE="$BOT/.env"
MODE="demo"
DEV=0
DOCKER=0

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "✖ $*" >&2; exit 1; }
info() { echo "▸ $*"; }

for arg in "$@"; do
  case "$arg" in
    demo|paper|live) MODE="$arg" ;;
    --dev) DEV=1 ;;
    --docker) DOCKER=1 ;;
    -h|--help) usage 0 ;;
    *) echo "Argumento desconocido: $arg" >&2; usage 1 ;;
  esac
done

# ---------------------------------------------------------------- secretos
ensure_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$BOT/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    info "Creado $ENV_FILE (permisos 600). Ahí van API keys, Telegram, etc."
  fi
  if ! grep -qE '^QB_DASHBOARD_TOKEN=.{16,}' "$ENV_FILE"; then
    local token
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    grep -v '^QB_DASHBOARD_TOKEN=' "$ENV_FILE" > "$ENV_FILE.tmp" || true
    echo "QB_DASHBOARD_TOKEN=$token" >> "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo
    echo "  Token del dashboard generado (se pide al entrar; queda en $ENV_FILE):"
    echo "    $token"
    echo
  fi
}

check_mode() {
  local cfg="$BOT/config/$MODE.toml"
  [[ -f "$cfg" ]] || {
    [[ "$MODE" == "live" ]] && die "Falta config/live.toml: copiá config/live.example.toml, revisalo y probá primero con testnet = true."
    die "No existe $cfg"
  }
  if [[ "$MODE" == "live" ]]; then
    grep -qE '^QB_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK' "$ENV_FILE" || die \
      "Modo LIVE opera con dinero real. Agregá QB_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK a $ENV_FILE solo si ya pasaste paper y testnet."
    grep -qE '^QB_API_KEY=.+' "$ENV_FILE" || die "Modo LIVE sin QB_API_KEY en $ENV_FILE"
  fi
}

ensure_env
check_mode
mkdir -p "$BOT/data"

# ---------------------------------------------------------------- docker
if [[ "$DOCKER" == 1 ]]; then
  command -v docker >/dev/null || die "docker no está instalado"
  info "docker compose: modo $MODE → http://127.0.0.1:${QB_HOST_PORT:-8765}"
  cd "$ROOT"
  QB_MODE="$MODE" exec docker compose up --build
fi

# ---------------------------------------------------------------- python
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Hace falta Python >= 3.11 (encontrado: $("$PY" --version 2>&1))"
VENV="$BOT/.venv"
EXTRAS="dashboard"
[[ "$MODE" != "demo" ]] && EXTRAS="dashboard,live"
STAMP="$VENV/.installed-$EXTRAS"
if [[ ! -x "$VENV/bin/python" ]]; then
  info "Creando entorno virtual en trading_bot/.venv"
  "$PY" -m venv "$VENV"
fi
if [[ ! -f "$STAMP" || "$BOT/pyproject.toml" -nt "$STAMP" ]]; then
  info "Instalando quantbot[$EXTRAS]"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "${BOT}[$EXTRAS]"
  touch "$STAMP"
fi

# ---------------------------------------------------------------- dashboard
if [[ "$DEV" == 0 ]]; then
  command -v npm >/dev/null || die "Hace falta Node.js >= 18 (npm) para compilar el dashboard"
  if [[ ! -d "$DASH/node_modules" || "$DASH/package-lock.json" -nt "$DASH/node_modules" ]]; then
    info "Instalando dependencias del dashboard"
    (cd "$DASH" && npm ci --no-audit --no-fund --silent)
  fi
  if [[ ! -f "$DASH/dist/index.html" ]] || [[ -n "$(find "$DASH/src" "$DASH/index.html" -newer "$DASH/dist/index.html" -print -quit)" ]]; then
    info "Compilando el dashboard"
    (cd "$DASH" && npm run build --silent >/dev/null)
  fi
fi

# ---------------------------------------------------------------- arrancar
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a
cd "$BOT"
CMD=("$VENV/bin/python" -m quantbot "$MODE" --config "config/$MODE.toml")

if [[ "$DEV" == 1 ]]; then
  (cd "$DASH" && { [[ -d node_modules ]] || npm ci --no-audit --no-fund --silent; } && npm run dev -- --strictPort) &
  VITE_PID=$!
  trap 'kill $VITE_PID 2>/dev/null || true' EXIT
  info "Modo $MODE · dashboard con hot reload en http://127.0.0.1:5173 (Ctrl+C para salir)"
  "${CMD[@]}"
else
  info "Modo $MODE · dashboard en http://127.0.0.1:${QB_API_PORT:-8765} (Ctrl+C para salir: safe shutdown)"
  exec "${CMD[@]}"
fi
