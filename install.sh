#!/usr/bin/env bash
# KnowledgeDesk installer. Works on macOS (with Docker Desktop) and Linux.
# Usage:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

OS="$(uname -s)"
SUDO=""

if ! command -v docker >/dev/null 2>&1; then
  if [ "$OS" = "Darwin" ]; then
    echo "Docker Desktop is not installed. Download it from https://www.docker.com/products/docker-desktop/ ,"
    echo "open it once, wait until it says 'Engine running', then run ./install.sh again."
    exit 1
  fi
  echo "Docker is not installed. Installing it now (needs your password)..."
  curl -fsSL https://get.docker.com | sudo sh
fi

# On Linux, use sudo if the current user can't talk to Docker directly.
if [ "$OS" != "Darwin" ] && ! docker info >/dev/null 2>&1; then
  SUDO="sudo"
fi
if ! $SUDO docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running. Start Docker (Docker Desktop on Mac), wait a minute, then run ./install.sh again."
  exit 1
fi

if [ ! -f .env ]; then
  echo "First-time setup. Answer a few questions:"
  read -rp "Company name: " company
  read -rp "Admin email: " admin_email
  while true; do
    read -rsp "Admin password (min. 8 characters): " admin_pw; echo
    [ "${#admin_pw}" -ge 8 ] && break
    echo "Too short, try again."
  done
  read -rsp "Claude API key (starts with sk-ant-): " api_key; echo
  {
    echo "COMPANY_NAME=${company}"
    echo "ADMIN_EMAIL=${admin_email}"
    echo "ADMIN_PASSWORD=${admin_pw}"
    echo "ANTHROPIC_API_KEY=${api_key}"
    echo "SECRET_KEY=$(openssl rand -hex 32)"
    echo "DB_PASSWORD=$(openssl rand -hex 16)"
  } > .env
  chmod 600 .env
  echo "Settings saved in .env"
fi

mkdir -p data/sources/_company
$SUDO docker compose up -d --build

PORT="$(grep -E '^PORT=' .env | cut -d= -f2 || true)"
PORT="${PORT:-8000}"
echo
echo "KnowledgeDesk is starting."
echo "The first start downloads the AI search and speech models (a few GB), so wait 5-15 minutes."
echo "Then open:  http://localhost:${PORT}"
echo "To watch progress:  $SUDO docker compose logs -f app   (press Ctrl+C to stop watching)"
