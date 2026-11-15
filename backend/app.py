"""FastAPI entry point.

Run:  uvicorn backend.app:app --reload --port 8000
Then open http://localhost:8000
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, executor, orchestrator, tools

app = FastAPI(title="CLI Chat Agent", version="1.0.0")
FRONTEND = config.ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
_rate: dict[str, deque] = defaultdict(deque)
RATE_LIMIT = 20  # requests per minute per client IP


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=config.MAX_PROMPT_CHARS)


def _rate_ok(ip: str) -> bool:
    now, q = time.time(), _rate[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return False
    q.append(now)
    return True


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "llm": config.llm_label(),
        "tools": {name: {"available": t.available(), "description": t.description, "binary": t.binary if name != "crawl_site" else "sitecrawl"}
                  for name, t in tools.TOOLS.items()},
        "crawler_chain": orchestrator._crawler_chain()[0],
        "limits": {"max_pages": config.CRAWL_MAX_PAGES, "max_depth": config.CRAWL_MAX_DEPTH,
                   "process_timeout_s": config.PROCESS_TIMEOUT_S, "rate_per_min": RATE_LIMIT},
    }


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip):
        raise HTTPException(429, "Too many requests - please wait a minute.")

    def stream():
        try:
            for event in orchestrator.handle(req.message):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:  # never leak a traceback / secrets to the client
            err = {"type": "final", "status": "failed", "text": f"Internal error ({type(e).__name__}). The task did not complete."}
            yield f"data: {json.dumps(err)}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/cancel/{run_id}")
def cancel(run_id: str):
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(400, "bad run id")
    return {"cancelled": executor.cancel(run_id)}


@app.get("/api/runs/{run_id}/{name}")
def run_file(run_id: str, name: str):
    """Download evidence of a run: raw CLI output, logs, final response."""
    files = {"raw": "result.json", "stderr": "stderr.log", "stdout": "stdout.log",
             "response": "response.json", "command": "command.json", "exit": "exit.json"}
    if not RUN_ID_RE.match(run_id) or name not in files:
        raise HTTPException(404)
    path = config.RUNS_DIR / run_id / files[name]
    if name == "raw" and not path.exists():
        path = config.RUNS_DIR / run_id / "crawl4ai.json"   # Crawl4AI writes its own output file
    if not path.exists():
        raise HTTPException(404, "not found")
    media = "application/json" if path.suffix == ".json" else "text/plain"
    return FileResponse(path, media_type=media, filename=f"{run_id}-{files[name]}")


@app.exception_handler(Exception)
async def unhandled(_, exc: Exception):
    return JSONResponse({"error": f"Internal error ({type(exc).__name__})"}, status_code=500)
