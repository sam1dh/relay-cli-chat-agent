#!/usr/bin/env bash
# One-command start (macOS / Linux)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Open http://localhost:8000"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
