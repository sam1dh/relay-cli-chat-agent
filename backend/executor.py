"""Process executor: starts an approved CLI as a child process and monitors it.

* argv list + shell=False  -> no shell injection possible
* stdin closed, minimal environment (API keys / secrets stripped)
* stdout and stderr captured line-by-line on reader threads and streamed live
* wall-clock timeout and user cancellation both kill the process
* every run is recorded under runs/<run_id>/ (argv, stdout, stderr, exit code)
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import config
from .tools import ToolSpec

SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)
MAX_CAPTURE_LINES = 2000
JSONISH = re.compile(r'^\s|^[{}\]"]|^\[\s*($|[{\["\d-])')   # JSON dump fragments, not log lines


@dataclass
class Job:
    run_id: str
    dir: Path
    cancel: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()
_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_JOBS)


def new_job() -> Job:
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = config.RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    job = Job(run_id=run_id, dir=d)
    with _jobs_lock:
        _jobs[run_id] = job
    return job


def cancel(run_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(run_id)
    if not job:
        return False
    job.cancel.set()
    return True


def finish(job: Job) -> None:
    with _jobs_lock:
        _jobs.pop(job.run_id, None)


def safe_env() -> dict[str, str]:
    """Child gets the parent env minus anything that looks like a secret."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["CRAWL4AI_NO_CLOUD_NOTICE"] = "1"   # keep crwl's promo banner out of the logs            # Windows: let child CLIs (e.g. Crawl4AI's rich logs) print unicode safely
    env["PYTHONUNBUFFERED"] = "1"
    return env


def display_argv(argv: list[str]) -> list[str]:
    """Readable command for the UI (hide the absolute interpreter/project paths)."""
    out = []
    for a in argv:
        if a == sys.executable:
            out.append("python")
        elif a.startswith(str(config.ROOT)):
            out.append(os.path.relpath(a, config.ROOT).replace("\\", "/"))
        else:
            out.append(a)
    return out


IS_WINDOWS = os.name == "nt"


def _limits(cpu_seconds: int):
    """POSIX only: cap CPU time and output file size of the child (and its children)."""
    def apply():
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
            resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))  # 64 MB per file
        except Exception:
            pass
    return apply


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process AND everything it spawned (e.g. headless Chromium)."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _reader(stream, name: str, q: queue.Queue, sink: list[str], log_path: Path) -> None:
    try:
        with open(log_path, "w", encoding="utf-8") as fh:   # full stream always saved as evidence
            for line in iter(stream.readline, ""):
                line = line.rstrip("\r\n")
                fh.write(line + "\n")
                if len(sink) < MAX_CAPTURE_LINES:
                    sink.append(line)
                q.put((name, line))
    finally:
        q.put((name, None))


def run(spec: ToolSpec, params: dict, job: Job) -> Iterator[dict]:
    """Run the tool, yielding events. The last event is always {'type': 'exec_end', ...}."""
    out_file = job.dir / spec.output_name
    argv = spec.build_argv(params, out_file)
    shown = display_argv(argv)
    (job.dir / "command.json").write_text(json.dumps({"tool": spec.name, "argv": shown, "params": params}, indent=2), encoding="utf-8")

    if not _slots.acquire(timeout=5):
        yield {"type": "exec_end", "ok": False, "exit_code": None, "error": "Server busy: too many concurrent tasks, try again shortly.",
               "duration_ms": 0, "timed_out": False, "cancelled": False, "stdout": "", "stderr": "", "result_file": None}
        return

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    started = time.time()
    timed_out = cancelled = False
    exit_code = None
    spawn_error = None
    try:
        try:
            extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
                     else {"start_new_session": True, "preexec_fn": _limits(spec.timeout_s + 30)})
            proc = subprocess.Popen(
                argv,
                shell=False,                 # never a shell: argv list only
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(job.dir),
                env=safe_env(),              # secrets stripped
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **extra,                     # own process group (+ CPU/file limits on POSIX)
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            spawn_error = f"Could not start '{spec.binary}': {type(e).__name__}"
            proc = None

        if proc is None:
            yield {"type": "exec_end", "ok": False, "exit_code": None, "error": spawn_error, "duration_ms": 0,
                   "timed_out": False, "cancelled": False, "stdout": "", "stderr": "", "result_file": None,
                   "argv": shown}
            return

        job.proc = proc
        yield {"type": "exec_start", "tool": spec.name, "argv": shown, "pid": proc.pid,
               "timeout_s": spec.timeout_s, "why": spec.why}

        q: queue.Queue = queue.Queue()
        threads = [
            threading.Thread(target=_reader, args=(proc.stdout, "stdout", q, stdout_lines, job.dir / "stdout.log"), daemon=True),
            threading.Thread(target=_reader, args=(proc.stderr, "stderr", q, stderr_lines, job.dir / "stderr.log"), daemon=True),
        ]
        for t in threads:
            t.start()

        open_streams = 2
        last_heartbeat = time.time()
        while open_streams:
            if time.time() - started > spec.timeout_s and not timed_out:
                timed_out = True
                kill_tree(proc)
                yield {"type": "log", "stream": "system", "line": f"Process exceeded {spec.timeout_s}s limit - killed."}
            if job.cancel.is_set() and not cancelled and not timed_out:
                cancelled = True
                kill_tree(proc)
                yield {"type": "log", "stream": "system", "line": "Cancelled by user - process killed."}
            try:
                name, line = q.get(timeout=0.25)
            except queue.Empty:
                if time.time() - last_heartbeat > 3:
                    last_heartbeat = time.time()
                    yield {"type": "heartbeat", "elapsed_ms": int((time.time() - started) * 1000)}
                continue
            if line is None:
                open_streams -= 1
                continue
            event = {"type": "log", "stream": name, "line": line[:1000]}
            if name == "stderr" and line.startswith("{"):
                try:
                    event["parsed"] = json.loads(line)
                except json.JSONDecodeError:
                    pass
            if name == "stdout":
                # curl's stdout IS the result; JSON dumps are not log lines
                if not spec.stream_stdout or not line.strip() or len(line) > 400 or JSONISH.match(line):
                    continue
            yield event

        try:
            exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            exit_code = proc.wait()
    finally:
        _slots.release()

    duration_ms = int((time.time() - started) * 1000)
    stdout_text = "\n".join(stdout_lines)
    stderr_text = "\n".join(stderr_lines)
    for t in threads:
        t.join(timeout=2)
    if (job.dir / "stdout.log").exists():
        stdout_text = (job.dir / "stdout.log").read_text(encoding="utf-8", errors="replace")
    meta = {"tool": spec.name, "argv": shown, "exit_code": exit_code, "duration_ms": duration_ms,
            "timed_out": timed_out, "cancelled": cancelled}
    (job.dir / "exit.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    yield {
        "type": "exec_end",
        "ok": exit_code == 0 and not timed_out and not cancelled,
        "exit_code": exit_code,
        "exit_meaning": spec.exit_codes.get(exit_code, "killed" if (timed_out or cancelled) else "unknown"),
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "stdout": stdout_text,
        "tool": spec.name,
        "stderr": stderr_text,
        "result_file": str(out_file) if out_file.exists() else None,
        "argv": shown,
    }
