"""Registry of APPROVED CLI tools.

The model never writes commands. It can only pick a tool *name* from this
registry; the backend then builds the argv list itself from validated, typed
parameters. Nothing is ever passed through a shell.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config


@dataclass
class ToolSpec:
    name: str
    description: str
    binary: str                                   # what is executed
    build_argv: Callable[[dict, Path], list[str]] # params, output_file -> argv
    output: str                                   # "file" (result JSON written to file) or "stdout"
    timeout_s: int
    why: str                                      # why this tool was chosen (shown in UI / README)
    exit_codes: dict[int, str] = field(default_factory=dict)
    output_name: str = "result.json"
    stream_stdout: bool = False                   # show stdout lines live in the UI log

    def available(self) -> bool:
        return shutil.which(self.binary) is not None or Path(self.binary).exists()


def _crawl_argv(p: dict, out: Path) -> list[str]:
    argv = [
        sys.executable, "-u", str(config.CLI_DIR / "sitecrawl.py"),
        "--url", p["url"],
        "--max-pages", str(p["max_pages"]),
        "--max-depth", str(p["max_depth"]),
        "--timeout", "10",
        "--delay", "0.2",
        "--concurrency", "3",
        "--output", str(out),
    ]
    if config.ALLOW_PRIVATE_HOSTS:
        argv.append("--allow-private")
    return argv


def _crawl4ai_argv(p: dict, out: Path) -> list[str]:
    # `crwl` resolved to an absolute path so Windows venvs work too.
    argv = [shutil.which("crwl") or "crwl", p["url"], "-o", "all", "-O", str(out), "--bypass-cache"]
    if p["max_pages"] > 1 and p["max_depth"] > 0:
        argv += ["--deep-crawl", "bfs", "--max-pages", str(p["max_pages"])]
    return argv


def _curl_argv(p: dict, out: Path) -> list[str]:
    # -I: HEAD request only, -sS: quiet but show errors, no -L (we report redirects instead
    # of following them, so a redirect can't bounce us to an internal address).
    return [
        "curl", "-sS", "-I",
        "--max-time", "15", "--connect-timeout", "8",
        "--proto", "=http,https",
        "-A", "cli-chat-agent/1.0 (site check)",
        "-w", "\n__CURL_META__ http_code=%{http_code} time_total=%{time_total} remote_ip=%{remote_ip} url=%{url_effective}\n",
        "--", p["url"],
    ]


TOOLS: dict[str, ToolSpec] = {
    "crawl_site": ToolSpec(
        name="crawl_site",
        description="Crawl a website (BFS, same domain), extract titles, headings, text, contacts, links; output JSON.",
        binary=sys.executable,
        build_argv=_crawl_argv,
        output="file",
        timeout_s=config.PROCESS_TIMEOUT_S,
        why="sitecrawl is our own sandboxed Python CLI: respects robots.txt, blocks private IPs on every "
            "redirect, enforces page/depth/size limits, streams progress on stderr and writes a JSON result file.",
        exit_codes={0: "success", 2: "invalid arguments / URL refused", 3: "site unreachable",
                    4: "blocked by robots.txt", 5: "internal crawler error"},
    ),
    "crawl4ai": ToolSpec(
        name="crawl4ai",
        description="Crawl4AI CLI (crwl): headless-Chromium crawler that renders JavaScript and outputs clean markdown + metadata.",
        binary="crwl",
        build_argv=_crawl4ai_argv,
        output="file",
        output_name="crawl4ai.json",
        timeout_s=config.PROCESS_TIMEOUT_S,
        why="Crawl4AI is an open-source, LLM-oriented crawler with a native CLI (crwl). It drives headless "
            "Chromium, so JavaScript-rendered sites work, and emits clean markdown + metadata that is ideal "
            "for summarisation. If it is missing or fails, the backend falls back to sitecrawl.",
        exit_codes={0: "success", 1: "crawl4ai error", 2: "invalid arguments"},
        stream_stdout=True,
    ),
    "check_site": ToolSpec(
        name="check_site",
        description="Quick availability/headers check of a URL using curl (HEAD request).",
        binary="curl",
        build_argv=_curl_argv,
        output="stdout",
        timeout_s=25,
        why="curl is the standard, battle-tested HTTP CLI; a HEAD request is the cheapest way to check "
            "if a site is up, its status code, server and redirects.",
        exit_codes={0: "success", 6: "could not resolve host", 7: "failed to connect", 28: "timeout",
                    35: "TLS handshake failed", 60: "TLS certificate problem"},
    ),
}


def get(name: str) -> ToolSpec | None:
    return TOOLS.get(name)
