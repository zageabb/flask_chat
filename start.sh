#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

# Flask Chat runtime configuration.
# Copy private values into ./secrets.env; that file is ignored by Git.
if [ -f "./secrets.env" ]; then
    . "./secrets.env"
fi

export SECRET_KEY="${SECRET_KEY:-dev-only-change-me}"
export FLASK_HOST="${FLASK_HOST:-0.0.0.0}"
export FLASK_PORT="${FLASK_PORT:-5000}"

export DATABASE_PATH="${DATABASE_PATH:-./chat.db}"
export OLLAMA_URL="${OLLAMA_URL:-http://192.168.1.249:11434}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2}"
export OLLAMA_TIMEOUT="${OLLAMA_TIMEOUT:-300}"
export OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-8192}"

export AGENT_MAX_STEPS="${AGENT_MAX_STEPS:-12}"
export AGENT_CONTEXT_MESSAGES="${AGENT_CONTEXT_MESSAGES:-100}"
export MAX_UPLOAD_DOCUMENTS="${MAX_UPLOAD_DOCUMENTS:-5}"
export DOCUMENT_TEXT_LIMIT="${DOCUMENT_TEXT_LIMIT:-20000}"
export AGENT_WORKSPACE="${AGENT_WORKSPACE:-./agent_workspace}"
export BASE_INSTRUCTIONS_FILE="${BASE_INSTRUCTIONS_FILE:-./base_instructions.md}"
export ORCHESTRATOR_INSTRUCTIONS_FILE="${ORCHESTRATOR_INSTRUCTIONS_FILE:-./orchestrator_instructions.md}"
export SKILLS_DIR="${SKILLS_DIR:-./skills}"
export SEARXNG_URL="${SEARXNG_URL:-http://192.168.1.249:8081}"

python app.py
