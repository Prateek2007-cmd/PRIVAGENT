#!/bin/bash
# Stream B isolated backend — localhost only, FreePi real provider.
# Copy of the worktree backend @ 0cae209 + Stream B fixes; repo untouched.
# Frontend NEVER runs here (scheduled execution is backend-only by construction).
set -e
B="$HOME/workspace/navigator-audit/worktrees3/stream-B/live"
export PRIVAGENT_DB_PATH="$B/data/privagent.db"
export PRIVAGENT_SECRETS_DIR="$B/data/secrets"
export PRIVAGENT_AUDIT_LOG_PATH="$B/data/audit.log"
export PRIVAGENT_AGENT_PROFILE_DIR="$B/data/agent-profile"
export PRIVAGENT_LOCAL_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
printf '%s' "$PRIVAGENT_LOCAL_TOKEN" > "$B/data/.local_token"
chmod 600 "$B/data/.local_token"
export NAVIGATOR_PROVIDER=antseed
export ANTSEED_BASE_URL=http://127.0.0.1:8452/v1
export ANTSEED_MODEL=deepseek-v4.1-flash
export ANTSEED_API_KEY="x"
export NODE_USE_ENV_PROXY=1
export PRIVAGENT_SCHEDULER_TICK_S=10
unset GEMINI_API_KEY GROQ_API_KEY DEEPAI_GATEWAY_KEY
cd "$B/backend-copy"
# NOTE: run_server.py's reload=True reloader does NOT honour
# PRIVAGENT_LOCAL_TOKEN for /api/* auth in this sandbox (401 on every
# call despite the env var being present in the process); run uvicorn
# directly instead — verified 2026-11-27.
exec "$B/venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
