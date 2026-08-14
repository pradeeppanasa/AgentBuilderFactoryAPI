#!/usr/bin/env bash
# Full local dev bootstrap, from a fresh clone, in one command.
# See docs/local-dev.md for what each step does and why, and for the three
# Docker bugs this script's fixes (Dockerfile + docker-compose.yml) work
# around. Idempotent — safe to re-run; only the on-`.env`-copy patch step
# (step 1) is skipped if `.env` already exists, so re-running never clobbers
# a developer's own customizations.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

START_TIME=$(date +%s)

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
ok()  { printf '    \033[1;32m✓\033[0m %s\n' "$1"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$1"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

# ── Docker Compose v1 (docker-compose) vs v2 (docker compose plugin) ─────
if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
elif docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  die "Neither 'docker-compose' nor 'docker compose' is available. Install Docker Desktop first."
fi

command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH."
docker info >/dev/null 2>&1 || die "Docker daemon is not running. Start Docker Desktop and re-run."

# ── Step 0: host Python venv (needed for alembic + create_admin.py) ─────
log "Step 0/7: Python virtual environment"
if [ ! -d .venv ]; then
  PYTHON_BIN="$(command -v python3 || command -v python)"
  [ -n "$PYTHON_BIN" ] || die "No python3/python found on PATH."
  "$PYTHON_BIN" -m venv .venv
  ok "created .venv"
fi
if [ -f .venv/Scripts/python.exe ]; then
  VENV_PY=.venv/Scripts/python.exe   # Windows venv layout
else
  VENV_PY=.venv/bin/python           # POSIX venv layout
fi
# requirements.txt is the pip-installable source of truth (Section 32.3) —
# a fast hash check skips the (slow) reinstall on every re-run.
REQ_HASH_FILE=.venv/.requirements.sha256
NEW_HASH="$("$VENV_PY" -c "import hashlib; print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())")"
if [ ! -f "$REQ_HASH_FILE" ] || [ "$(cat "$REQ_HASH_FILE")" != "$NEW_HASH" ]; then
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet -r requirements.txt
  echo "$NEW_HASH" > "$REQ_HASH_FILE"
  ok "installed/updated Python dependencies"
else
  ok "Python dependencies already up to date"
fi

# ── Step 1: .env ──────────────────────────────────────────────────────
log "Step 1/7: .env"
if [ -f .env ]; then
  ok ".env already exists — leaving your values as-is"
else
  cp .env.example .env

  set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" .env; then
      sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
      printf '%s=%s\n' "$key" "$value" >> .env
    fi
  }

  # Local-dev-only values — see docs/local-dev.md's ".env" section for why
  # each of these differs from .env.example's real-deployment defaults.
  set_env JWT_SECRET_ARN "jwt-secret"
  set_env GIT_CREDENTIALS_SECRET "git-token"
  set_env IAC_OUTPUT_BUCKET "panasa-iac-artifacts-local"
  set_env AUDIT_S3_BUCKET "panasa-audit-local"
  set_env DATABASE_URL "postgresql+asyncpg://panasa:panasa@localhost:5433/panasa_agent_builder"
  set_env REDIS_URL "redis://localhost:6379/0"
  set_env DYNAMODB_ENDPOINT "http://localhost:8001"
  set_env SECRETS_MANAGER_ENDPOINT "http://localhost:4566"
  set_env S3_ENDPOINT "http://localhost:4566"
  # DynamoDB Local / LocalStack don't validate these, but boto3 refuses to
  # sign ANY request without something present, and there's no IAM role to
  # fall back to outside real AWS. LOCAL DEV ONLY — never set these in a
  # real deployment's .env (a real IAM role/task role must be used instead).
  set_env AWS_ACCESS_KEY_ID "local"
  set_env AWS_SECRET_ACCESS_KEY "local"

  ok "created .env with local dev values pre-filled"
fi

# Deliberately NOT `source .env` here: .env.example's real-deployment
# placeholders (e.g. `panasa-transcripts-<account>`) contain `<`/`>`, which
# bash parses as redirection syntax and fails on with a syntax error.
# app.config.settings reads .env itself via pydantic-settings regardless of
# the calling shell's environment; scripts/_dotenv.py covers the handful of
# bootstrap-only keys (DEFAULT_ADMIN_*) that aren't Settings fields. See
# docs/local-dev.md.

# ── Step 2: start dependency services ────────────────────────────────
log "Step 2/7: starting DynamoDB Local, LocalStack, Postgres, Redis"
"${COMPOSE[@]}" up -d dynamodb-local localstack postgres redis
ok "containers created"

# ── Step 3: wait for each to be healthy ──────────────────────────────
log "Step 3/7: waiting for services to be healthy"

wait_for() {
  local name="$1" attempts="$2" delay="$3" check="$4"
  local i=0
  until eval "$check" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge "$attempts" ]; then
      die "$name did not become healthy after $((attempts * delay))s"
    fi
    sleep "$delay"
  done
  ok "$name is healthy"
}

wait_for "Redis" 20 1 \
  "docker exec panasa-redis redis-cli ping | grep -q PONG"

wait_for "Postgres" 30 1 \
  "docker exec postgres pg_isready -U panasa -d panasa_agent_builder"

wait_for "LocalStack (S3 + Secrets Manager)" 30 2 \
  "\"$VENV_PY\" -m scripts._check_localstack"

wait_for "DynamoDB Local" 30 1 \
  "\"$VENV_PY\" -m scripts._check_dynamodb"

# ── Step 4: seed LocalStack ───────────────────────────────────────────
log "Step 4/7: seeding LocalStack (S3 buckets + Secrets Manager secrets)"
"$VENV_PY" -m scripts.seed_localstack

# ── Step 5: start the runtime, wait for it, then migrate ─────────────
log "Step 5/7: building + starting agent-builder-runtime"
"${COMPOSE[@]}" up -d --build agent-builder-runtime

wait_for "agent-builder-runtime" 60 2 \
  "docker inspect agent-builder-runtime --format='{{.State.Health.Status}}' | grep -q healthy"

log "Step 5/7: running Alembic migrations"
"$VENV_PY" -m alembic upgrade head
ok "database schema up to date"

# ── Step 6: default admin user ───────────────────────────────────────
log "Step 6/7: creating default admin user"
"$VENV_PY" -m scripts.create_admin

# ── Step 7: health check ─────────────────────────────────────────────
log "Step 7/7: health check"
HEALTH="$(curl -sf http://localhost:8000/api/v1/platform/health)"
echo "$HEALTH" | "$VENV_PY" -m json.tool 2>/dev/null || echo "$HEALTH"

ADMIN_EMAIL="$("$VENV_PY" -c "from scripts._dotenv import load_env; print(load_env().get('DEFAULT_ADMIN_EMAIL', ''))")"
ADMIN_PASSWORD="$("$VENV_PY" -c "from scripts._dotenv import load_env; print(load_env().get('DEFAULT_ADMIN_PASSWORD', ''))")"

ELAPSED=$(( $(date +%s) - START_TIME ))
printf '\n\033[1;32mStack is up in %ss.\033[0m\n' "$ELAPSED"
printf '  API:        http://localhost:8000\n'
printf '  Admin user: %s / %s\n' "${ADMIN_EMAIL:-<not set>}" "${ADMIN_PASSWORD:-<not set>}"
printf '  Logs:       %s logs -f agent-builder-runtime\n' "${COMPOSE[*]}"
printf '  Tear down:  %s down\n\n' "${COMPOSE[*]}"
