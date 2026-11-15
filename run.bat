@echo off
REM One-command start (Windows)
cd /d %~dp0
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if not exist .env copy .env.example .env
echo Open http://localhost:8000
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
