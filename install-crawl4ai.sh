#!/usr/bin/env bash
# Optional: install Crawl4AI + headless Chromium (Linux/macOS). Run once after ./run.sh created .venv
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-crawl4ai.txt

if [ -f /etc/arch-release ]; then
  # Arch / Manjaro / EndeavourOS: Playwright's --with-deps only knows apt, so install libs with pacman
  echo ">> Arch detected: installing Chromium runtime libraries with pacman (needs sudo)"
  sudo pacman -S --needed --noconfirm nss atk at-spi2-core libcups libdrm libxkbcommon libxcomposite \
       libxdamage libxrandr libxfixes libxshmfence mesa pango cairo alsa-lib
  python -m playwright install chromium          # browser only, no apt
  crawl4ai-setup || true                          # finishes Crawl4AI's own setup; its apt step may warn - fine
else
  crawl4ai-setup                                  # Debian/Ubuntu/macOS: installs browser + deps
fi

crwl --help >/dev/null && echo ">> Crawl4AI ready. Restart ./run.sh - the sidebar will show crawl4ai as available."
