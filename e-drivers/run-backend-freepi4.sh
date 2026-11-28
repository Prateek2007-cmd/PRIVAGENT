#!/bin/bash
# Final verification backend (v4): isolated copy of the MERGED commit, fresh venv, fresh DB.
# Usage: MERGED_COPY=<dir> ./run-backend-freepi4.sh [logfile]
# Defaults: copy at $FINAL4/backend-copy, DB at $FINAL4/data/privagent.db
set -u
F4="$HOME/workspace/navigator-audit/final4"
COPY="${MERGED_COPY:-$F4/backend-copy}"
LOG="${1:-$F4/backend-v4.log}"
export PRIVAGENT_DB_PATH="$F4/data/privagent.db"
export PRIVAGENT_SECRETS_DIR="$F4/data/secrets"
export PRIVAGENT_AUDIT_LOG_PATH="$F4/data/audit.log"
export PRIVAGENT_AGENT_PROFILE_DIR="$F4/data/agent-profile"
export PRIVAGENT_LOCAL_TOKEN="final-test-token"
export NAVIGATOR_PROVIDER=antseed
export ANTSEED_BASE_URL=http://127.0.0.1:8452/v1
export ANTSEED_MODEL=deepseek-v4.1-flash
export ANTSEED_API_KEY=dummy
export NODE_USE_ENV_PROXY=1
export PRIVAGENT_SCHEDULER_TICK_S=10
unset GEMINI_API_KEY GROQ_API_KEY DEEPAI_GATEWAY_KEY
echo "final-test-token" > "$F4/data/.local_token"
cd "$COPY" || { echo "error: merged copy not found: $COPY" >&2; exit 1; }
exec "$F4/venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level info 2>&1 | tee -a "$LOG"
