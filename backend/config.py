"""Central configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Tiny .env loader (no extra dependency). Existing env vars win."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---- LLM -------------------------------------------------------------------
# LLM_PROVIDER: "gemini", "openai" (any OpenAI-compatible API: OpenAI, Groq,
# OpenRouter, Ollama...), or "none" (rule-based intent + extractive summary).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_S = _int("LLM_TIMEOUT_S", 45)
# For reasoning models on OpenAI-compatible APIs (e.g. Groq openai/gpt-oss-*): low | medium | high | "" (off)
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "").strip().lower()

if LLM_PROVIDER == "auto":
    LLM_PROVIDER = "gemini" if GEMINI_API_KEY else ("openai" if OPENAI_API_KEY else "none")

# ---- Execution limits ------------------------------------------------------
CRAWL_DEFAULT_PAGES = _int("CRAWL_DEFAULT_PAGES", 5)
CRAWL_MAX_PAGES = min(_int("CRAWL_MAX_PAGES", 20), 50)
CRAWL_DEFAULT_DEPTH = _int("CRAWL_DEFAULT_DEPTH", 1)
CRAWL_MAX_DEPTH = min(_int("CRAWL_MAX_DEPTH", 2), 3)
PROCESS_TIMEOUT_S = _int("PROCESS_TIMEOUT_S", 90)       # whole CLI process wall-clock limit
MAX_CONCURRENT_JOBS = _int("MAX_CONCURRENT_JOBS", 3)
MAX_PROMPT_CHARS = _int("MAX_PROMPT_CHARS", 2000)

# Which crawler CLI to use for crawl tasks:
#   auto      -> Crawl4AI (`crwl`, headless Chromium) if installed, else sitecrawl
#   crawl4ai  -> Crawl4AI only (falls back to sitecrawl if it fails)
#   sitecrawl -> our lightweight crawler only
CRAWLER = os.getenv("CRAWLER", "auto").strip().lower()
CRAWL4AI_FALLBACK = _bool("CRAWL4AI_FALLBACK", True)

# Skip the intent LLM call when the request is unambiguous (URL + clear verb). Saves ~1 s.
INTENT_FAST_PATH = _bool("INTENT_FAST_PATH", False)

# Only for local testing against 127.0.0.1 fixtures. Never enable in production.
ALLOW_PRIVATE_HOSTS = _bool("ALLOW_PRIVATE_HOSTS", False)

RUNS_DIR = Path(os.getenv("RUNS_DIR", ROOT / "runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)
CLI_DIR = ROOT / "cli"


def llm_label() -> str:
    if LLM_PROVIDER == "gemini":
        return f"Gemini ({GEMINI_MODEL})"
    if LLM_PROVIDER == "openai":
        return f"OpenAI-compatible ({OPENAI_MODEL})"
    return "rule-based (no LLM key configured)"
