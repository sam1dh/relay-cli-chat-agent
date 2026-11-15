@echo off
REM Optional: install Crawl4AI + headless Chromium (Windows). Run once after run.bat created .venv
cd /d %~dp0
call .venv\Scripts\activate.bat
pip install -r requirements-crawl4ai.txt
crawl4ai-setup
crwl config set VERBOSE true
crwl --help >nul 2>&1 && echo Crawl4AI ready. Restart run.bat - the sidebar will show crawl4ai as available.
