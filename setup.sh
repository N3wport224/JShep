#!/usr/bin/env bash
# One-command local setup for the AI SDR backend (macOS/Linux).
#
# What this does:
#   1. Checks Docker + Docker Compose are installed (prints install
#      instructions and exits if not).
#   2. Creates .env from .env.example on first run, interactively prompting
#      for the credentials most people need (Outlook SMTP/IMAP, HubSpot,
#      Google Places) - press Enter on any prompt to skip it and configure
#      it later by editing .env directly.
#   3. Builds and starts every service (docker compose up --build -d),
#      waits for Postgres to report healthy, waits for the one-shot
#      `migrate` container to apply the database schema (alembic upgrade
#      head), waits for the API to respond, then prints the dashboard URL
#      and how to sign in.
#
# Usage:
#   ./setup.sh              interactive first-time setup (or just boots the
#                            stack if .env already exists)
#   ./setup.sh --yes        never prompt; accept defaults / leave blank
#   ./setup.sh --reconfigure  re-run the credential prompts even if .env exists
#   ./setup.sh --help
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# --- colors (disabled automatically when not writing to a terminal) -------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

info()    { echo "${CYAN}==>${RESET} $*"; }
ok()      { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
fail()    { echo "${RED}✗ $*${RESET}" >&2; }
heading() { echo; echo "${BOLD}$*${RESET}"; }

NON_INTERACTIVE=false
RECONFIGURE=false
for arg in "$@"; do
  case "$arg" in
    -y|--yes) NON_INTERACTIVE=true ;;
    --reconfigure) RECONFIGURE=true ;;
    -h|--help)
      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      fail "Unknown option: $arg (see --help)"
      exit 1
      ;;
  esac
done
if [ ! -t 0 ]; then
  NON_INTERACTIVE=true
fi

# ---------------------------------------------------------------------------
# 1. Prerequisite checks
# ---------------------------------------------------------------------------
heading "1/3 Checking prerequisites"

print_docker_install_instructions() {
  echo
  fail "Docker is required and wasn't found on this machine."
  echo
  echo "  ${BOLD}macOS${RESET}"
  echo "    1. Download Docker Desktop: https://www.docker.com/products/docker-desktop/"
  echo "    2. Open the downloaded .dmg and drag Docker to Applications"
  echo "    3. Launch Docker from Applications and wait for the whale icon"
  echo "       in the menu bar to say \"Docker Desktop is running\""
  echo "    4. Re-run this script: ./setup.sh"
  echo
  echo "  ${BOLD}Linux${RESET}"
  echo "    Ubuntu/Debian:"
  echo "      curl -fsSL https://get.docker.com | sh"
  echo "      sudo usermod -aG docker \$USER   # then log out and back in"
  echo "    Or install Docker Desktop for Linux:"
  echo "      https://docs.docker.com/desktop/setup/install/linux/"
  echo "    Then re-run this script: ./setup.sh"
  echo
}

if ! command -v docker >/dev/null 2>&1; then
  print_docker_install_instructions
  exit 1
fi
ok "Docker is installed ($(docker --version))"

if ! docker info >/dev/null 2>&1; then
  fail "Docker is installed but doesn't seem to be running."
  echo "  Start Docker Desktop (macOS) or the docker service (Linux: sudo systemctl start docker), then re-run this script."
  exit 1
fi
ok "Docker daemon is running"

DOCKER_COMPOSE=()
if docker compose version >/dev/null 2>&1; then
  DOCKER_COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DOCKER_COMPOSE=(docker-compose)
else
  echo
  fail "Docker Compose is required and wasn't found."
  echo "  Docker Compose ships with Docker Desktop - if you just installed Docker"
  echo "  Desktop, restart it. On Linux without Desktop, install the plugin:"
  echo "    https://docs.docker.com/compose/install/linux/"
  exit 1
fi
ok "Docker Compose is available (${DOCKER_COMPOSE[*]})"

# ---------------------------------------------------------------------------
# 2. Environment configuration
# ---------------------------------------------------------------------------
heading "2/3 Configuring environment (.env)"

set_env_var() {
  # set_env_var KEY VALUE - updates KEY=... in .env, or appends it if the
  # key isn't present yet. Written via a temp file + mv so it works
  # identically with GNU sed (Linux) and BSD sed (macOS) - no -i flag.
  local key="$1" value="$2"
  local escaped
  escaped=$(printf '%s' "$value" | sed -e 's/[\/&]/\\&/g')
  if grep -q "^${key}=" .env 2>/dev/null; then
    sed "s/^${key}=.*/${key}=${escaped}/" .env > .env.tmp && mv .env.tmp .env
  else
    echo "${key}=${value}" >> .env
  fi
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif [ -r /dev/urandom ]; then
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  else
    python3 -c "import secrets; print(secrets.token_hex(32))"
  fi
}

prompt() {
  # prompt "Question text" "default" -> echoes the answer (or default)
  local question="$1" default="${2:-}" answer=""
  if [ "$NON_INTERACTIVE" = true ]; then
    echo "$default"
    return
  fi
  if [ -n "$default" ]; then
    read -r -p "  $question [$default]: " answer || true
  else
    read -r -p "  $question (press Enter to skip): " answer || true
  fi
  echo "${answer:-$default}"
}

prompt_secret() {
  # Same as prompt() but doesn't echo what's typed (for passwords/API keys).
  local question="$1" answer=""
  if [ "$NON_INTERACTIVE" = true ]; then
    echo ""
    return
  fi
  read -r -s -p "  $question (press Enter to skip, input hidden): " answer || true
  echo >&2
  echo "$answer"
}

FIRST_TIME_SETUP=false
if [ ! -f .env ]; then
  FIRST_TIME_SETUP=true
  cp .env.example .env
  ok "Created .env from .env.example"
elif [ "$RECONFIGURE" = true ]; then
  FIRST_TIME_SETUP=true
  ok ".env already exists - re-running credential prompts (--reconfigure)"
else
  ok ".env already exists - leaving it as-is (use --reconfigure to re-prompt)"
fi

if [ "$FIRST_TIME_SETUP" = true ]; then
  if [ "$NON_INTERACTIVE" = true ]; then
    warn "Non-interactive mode: leaving credential fields blank in .env."
    warn "Edit .env yourself before the app will be able to send/receive email or use AI features."
  else
    echo
    echo "  A few quick questions to get you running - press Enter on any"
    echo "  question to skip it and fill it in later by editing .env."
    echo
  fi

  echo "  ${BOLD}AI provider (required for the app to draft/classify anything)${RESET}"
  anthropic_key=$(prompt_secret "Anthropic API key (from https://console.anthropic.com/)")
  if [ -n "$anthropic_key" ]; then
    set_env_var ANTHROPIC_API_KEY "$anthropic_key"
    ok "Anthropic API key saved"
  else
    warn "No Anthropic API key set - add ANTHROPIC_API_KEY to .env before using the app"
  fi

  echo
  echo "  ${BOLD}Outlook email (SMTP for sending, IMAP for reading replies)${RESET}"
  outlook_email=$(prompt "Outlook email address" "")
  if [ -n "$outlook_email" ]; then
    outlook_password=$(prompt_secret "Outlook password or app password")
    set_env_var SMTP_HOST "smtp.office365.com"
    set_env_var SMTP_PORT "587"
    set_env_var SMTP_USE_TLS "true"
    set_env_var SMTP_USERNAME "$outlook_email"
    set_env_var FROM_EMAIL "$outlook_email"
    set_env_var IMAP_HOST "outlook.office365.com"
    set_env_var IMAP_PORT "993"
    set_env_var IMAP_USERNAME "$outlook_email"
    if [ -n "$outlook_password" ]; then
      set_env_var SMTP_PASSWORD "$outlook_password"
      set_env_var IMAP_PASSWORD "$outlook_password"
    fi
    ok "Outlook SMTP/IMAP configured for $outlook_email"
    warn "If your Microsoft account uses MFA, use an app password, not your normal password:"
    warn "  https://support.microsoft.com/en-us/account-billing/manage-app-passwords"
  else
    warn "Skipped - outbound sending and reply polling won't work until SMTP_*/IMAP_* are set in .env"
  fi

  echo
  echo "  ${BOLD}HubSpot CRM sync (optional)${RESET}"
  hubspot_token=$(prompt_secret "HubSpot private app access token")
  if [ -n "$hubspot_token" ]; then
    set_env_var HUBSPOT_ACCESS_TOKEN "$hubspot_token"
    set_env_var CRM_PROVIDER "hubspot"
    ok "HubSpot CRM sync enabled"
  else
    warn "Skipped - Positive/Interested leads won't be pushed to a CRM"
  fi

  echo
  echo "  ${BOLD}Google Places (for automated lead discovery, optional)${RESET}"
  places_key=$(prompt_secret "Google Places API key")
  if [ -n "$places_key" ]; then
    set_env_var GOOGLE_PLACES_API_KEY "$places_key"
    set_env_var LEAD_DISCOVERY_ENABLED "true"
    ok "Automated lead discovery enabled"
  else
    warn "Skipped - automated daily lead discovery stays off (LEAD_DISCOVERY_ENABLED=false)"
  fi

  echo
  echo "  ${BOLD}Dashboard admin access${RESET}"
  admin_key=$(generate_secret)
  jwt_secret=$(generate_secret)
  set_env_var ADMIN_API_KEY "$admin_key"
  set_env_var JWT_SECRET "$jwt_secret"
  ok "Generated a secure admin API key and JWT signing secret"
fi

# ---------------------------------------------------------------------------
# 3. Build, boot, and migrate
# ---------------------------------------------------------------------------
heading "3/3 Building and starting the stack"

info "Running: ${DOCKER_COMPOSE[*]} up --build -d"
"${DOCKER_COMPOSE[@]}" up --build -d

APP_PORT=$(grep -E '^APP_PORT=' .env | tail -n1 | cut -d= -f2)
APP_PORT=${APP_PORT:-8000}

wait_for_container_health() {
  local container="$1" timeout="${2:-90}" waited=0
  info "Waiting for $container to become healthy..."
  while [ "$waited" -lt "$timeout" ]; do
    local status
    status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "unknown")
    if [ "$status" = "healthy" ]; then
      ok "$container is healthy"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  warn "$container didn't report healthy within ${timeout}s (status: $status) - continuing anyway"
  return 1
}

wait_for_container_health sdr-postgres 90

info "Waiting for the database migration to finish..."
migrate_waited=0
migrate_timeout=120
migrate_exit=""
while [ "$migrate_waited" -lt "$migrate_timeout" ]; do
  migrate_exit=$(docker inspect --format='{{.State.ExitCode}}' sdr-migrate 2>/dev/null || echo "")
  migrate_running=$(docker inspect --format='{{.State.Running}}' sdr-migrate 2>/dev/null || echo "")
  if [ -n "$migrate_exit" ] && [ "$migrate_running" != "true" ]; then
    break
  fi
  sleep 2
  migrate_waited=$((migrate_waited + 2))
done

if [ "$migrate_exit" = "0" ]; then
  ok "Database schema is up to date (alembic upgrade head succeeded)"
else
  fail "Database migration did not complete successfully (exit code: ${migrate_exit:-unknown})"
  echo "  Check the logs with: ${DOCKER_COMPOSE[*]} logs migrate"
  echo "  You can retry it with: ${DOCKER_COMPOSE[*]} run --rm migrate"
fi

info "Waiting for the API to respond..."
api_waited=0
api_timeout=90
api_up=false
while [ "$api_waited" -lt "$api_timeout" ]; do
  if curl -fs "http://localhost:${APP_PORT}/health" >/dev/null 2>&1; then
    api_up=true
    break
  fi
  sleep 2
  api_waited=$((api_waited + 2))
done

echo
if [ "$api_up" = true ]; then
  echo "${GREEN}${BOLD}Setup complete - the AI SDR backend is running.${RESET}"
else
  warn "The API didn't respond within ${api_timeout}s yet - it may still be starting."
  warn "Check progress with: ${DOCKER_COMPOSE[*]} logs -f sdr-backend"
fi
echo
echo "  ${BOLD}Dashboard:${RESET}  http://localhost:${APP_PORT}/dashboard"
admin_key_in_env=$(grep -E '^ADMIN_API_KEY=' .env | tail -n1 | cut -d= -f2)
if [ -n "$admin_key_in_env" ]; then
  echo "  ${BOLD}Admin key:${RESET}  $admin_key_in_env"
  echo
  echo "  The dashboard requires this admin key. Either:"
  echo "    - open http://localhost:${APP_PORT}/dashboard?token=$admin_key_in_env in your browser, or"
  echo "    - use a REST client / curl with header: X-API-Key: $admin_key_in_env"
else
  echo
  echo "  ${YELLOW}No ADMIN_API_KEY is set - the dashboard is unauthenticated. Set ADMIN_API_KEY in .env for anything beyond local testing.${RESET}"
fi
echo
echo "  ${BOLD}API docs:${RESET}   http://localhost:${APP_PORT}/docs"
echo "  ${BOLD}Approval queue${RESET} (leads that replied positively, waiting on you to approve/reject the AI's"
echo "  drafted response) is on the dashboard above, or via GET /approvals with the admin key."
echo
echo "  Useful commands:"
echo "    ${DOCKER_COMPOSE[*]} logs -f          # tail all logs"
echo "    ${DOCKER_COMPOSE[*]} ps               # see service status"
echo "    ${DOCKER_COMPOSE[*]} down             # stop everything"
echo "    ./setup.sh --reconfigure        # re-run the credential prompts"
echo
