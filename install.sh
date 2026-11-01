#!/usr/bin/env bash
# KnowledgeDesk installer for a customer server (Ubuntu/Debian with Docker).
# Usage:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Installing it now (needs sudo)..."
  curl -fsSL https://get.docker.com | sudo sh
fi

if [ ! -f .env ]; then
  read -rp "Company name: " company
  read -rp "Admin email: " admin_email
  read -rsp "Admin password (min. 8 characters): " admin_pw; echo
  read -rsp "Claude API key (sk-ant-...): " api_key; echo
  cp .env.example .env
  sed -i "s|^COMPANY_NAME=.*|COMPANY_NAME=${company}|" .env
  sed -i "s|^ADMIN_EMAIL=.*|ADMIN_EMAIL=${admin_email}|" .env
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${admin_pw}|" .env
  sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${api_key}|" .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env
  sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$(openssl rand -hex 16)|" .env
  chmod 600 .env
  echo "Settings saved in .env"
fi

mkdir -p data/sources/_company
sudo docker compose up -d --build
echo
echo "KnowledgeDesk is starting. The first start downloads the AI search and speech models (a few GB),"
echo "so it can take several minutes. Then open:  http://$(hostname -I | awk '{print $1}'):${PORT:-8000}"
echo "Logs:  sudo docker compose logs -f app"
