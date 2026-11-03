#!/usr/bin/env bash
# Full end-to-end QA run: fresh database, the app in test mode, simulated Google/Microsoft, then the browser script.
# Needs: PostgreSQL with pgvector on localhost (user kd/kd), Python deps, Node + Playwright.
set -euo pipefail
cd "$(dirname "$0")/../.."
WORK="${QA_WORK:-$(mktemp -d)}"
mkdir -p "$WORK"
DB="kd_qa"
export PGPASSWORD="${PGPASSWORD:-kd}"
psql -h localhost -U kd postgres -q -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" -c "CREATE DATABASE $DB"

python3 -m uvicorn tests.e2e.simulator:app --port 9999 --log-level warning > "$WORK/simulator.log" 2>&1 &
SIM_PID=$!
env DATABASE_URL="postgresql://kd:kd@localhost:5432/$DB" DATA_DIR="$WORK/data" SOURCES_DIR="$WORK/data/sources" \
    UPLOADS_DIR="$WORK/data/uploads" LICENSE_FILE="$WORK/data/license.key" MODEL_CACHE="${MODEL_CACHE:-$HOME/.cache/kd-models}" \
    COMPANY_NAME="Acme Plumbing" ADMIN_EMAIL="admin@acme.test" ADMIN_PASSWORD="admin-pass-123" SECRET_KEY="qa-secret-0123456789abcdef0123456789abcdef" \
    KD_FAKE_LLM=true KD_PROVIDER_OVERRIDE="http://127.0.0.1:9999" GOOGLE_CLIENT_ID="qa-google" GOOGLE_CLIENT_SECRET="qa" \
    MICROSOFT_CLIENT_ID="qa-ms" SLACK_CLIENT_ID="qa-slack" SLACK_CLIENT_SECRET="qa" META_APP_ID="qa-meta" META_APP_SECRET="qa" SUMMARIZE_INTERVIEWS=false SCAN_INTERVAL_SECONDS=5 \
    python3 -m uvicorn app.main:app --port 8100 --log-level warning > "$WORK/app.log" 2>&1 &
APP_PID=$!
trap 'kill $APP_PID $SIM_PID 2>/dev/null || true' EXIT
for i in $(seq 1 120); do curl -sf http://127.0.0.1:8100/api/status >/dev/null && break; sleep 1; done

OUT="${OUT:-$WORK/screens}" node tests/e2e/qa.js
echo "Screenshots and logs: $WORK"
