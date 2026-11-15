"""Orchestrator: message -> plan -> approved CLI -> validated result -> chat response.

    plan   = intent.parse(message)                 # LLM or rules, then validated
    events = executor.run(tool, params, job)       # real child process, streamed
    result = validate_and_parse(stdout/file, exit) # never trust, always verify
    reply  = build_response(result, summary)       # summary + structured JSON
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterator

from . import config, crawl4ai_adapter, executor, intent, summarizer, tools

LABEL = {"crawl_site": "sitecrawl", "crawl4ai": "crawl4ai", "check_site": "curl"}
REQUIRED_CRAWL_KEYS = {"tool", "status", "start_url", "stats", "site", "pages", "errors"}


def handle(message: str) -> Iterator[dict]:
    plan = intent.parse(message)
    yield {"type": "plan", "plan": plan.to_dict()}

    if plan.intent in ("chat", "unsupported"):
        yield {"type": "final", "status": "info" if plan.intent == "chat" else "refused", "text": plan.reply}
        return
    if plan.error:
        yield {"type": "final", "status": "invalid_input", "text": f"I couldn't start the task: {plan.error}",
               "json": _envelope(message, plan, "invalid_input", None, None, None, error=plan.error)}
        return

    params = {"url": plan.url, "max_pages": plan.max_pages, "max_depth": plan.max_depth}

    if plan.intent == "check_site":
        spec = tools.get("check_site")
        if not spec.available():
            yield {"type": "final", "status": "failed", "text": "curl is not installed on this server."}
            return
        end, exec_info = yield from _run(spec, params)
        if (bad := _process_problem(end, exec_info, spec)):
            yield _fail(message, plan, exec_info, *bad)
            return
        yield from _finish_check(message, plan, end, exec_info)
        return

    # ---- crawl: pick crawler CLI(s) -------------------------------------------------
    attempts_plan, note = _crawler_chain()
    if note:
        yield {"type": "status", "message": note}
    if attempts_plan[0] == "crawl4ai" and not _robots_allows(plan.url):
        yield _fail(message, plan, None, "blocked",
                    "The site's robots.txt does not allow crawling this URL, so no crawler was started.")
        return

    attempts = []
    raw = end = exec_info = None
    for i, name in enumerate(attempts_plan):
        spec = tools.get(name)
        end, exec_info = yield from _run(spec, params)
        if (bad := _process_problem(end, exec_info, spec)):
            if end.get("timed_out") or end.get("cancelled") or i == len(attempts_plan) - 1:
                exec_info["attempts"] = attempts + [{"tool": name, "exit_code": end.get("exit_code"), "outcome": bad[0]}]
                yield _fail(message, plan, exec_info, *bad)
                return
            reason = bad[1]
        else:
            raw, reason = _load_result(spec, end, plan)
            if raw is not None and raw.get("pages"):
                attempts.append({"tool": name, "exit_code": end.get("exit_code"), "outcome": raw["status"]})
                break
            if raw is not None and raw.get("status") in ("blocked_by_robots", "invalid_url"):
                break  # deterministic refusal: a second crawler would not change it
        attempts.append({"tool": name, "exit_code": end.get("exit_code"), "outcome": "failed", "reason": reason})
        if i < len(attempts_plan) - 1:
            yield {"type": "status", "message": f"{LABEL[name]} failed ({reason}) - falling back to {LABEL[attempts_plan[i + 1]]}"}
    exec_info["attempts"] = attempts
    yield from _finish_crawl(message, plan, end, exec_info, raw)


def _crawler_chain() -> tuple[list[str], str | None]:
    c4 = tools.get("crawl4ai")
    if config.CRAWLER == "sitecrawl":
        return ["crawl_site"], None
    if c4.available():
        return (["crawl4ai", "crawl_site"] if config.CRAWL4AI_FALLBACK else ["crawl4ai"]), None
    note = "Crawl4AI (crwl) is not installed - using the sitecrawl CLI" if config.CRAWLER in ("auto", "crawl4ai") else None
    return ["crawl_site"], note


def _robots_allows(url: str) -> bool:
    """Pre-flight robots.txt check for crawlers that don't enforce it themselves."""
    from urllib import robotparser
    from urllib.parse import urlparse
    import requests
    p = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=5,
                         headers={"User-Agent": "relay-cli-chat-agent/1.0"})
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
            return rp.can_fetch("*", url)
    except requests.RequestException:
        pass
    return True


def _run(spec, params):
    """Run one approved CLI, re-yielding its live events. Returns (exec_end, exec_info)."""
    job = executor.new_job()
    yield {"type": "job", "run_id": job.run_id, "tool": spec.name}
    end = None
    try:
        for ev in executor.run(spec, params, job):
            if ev["type"] == "exec_end":
                end = ev
                yield {k: v for k, v in ev.items() if k not in ("stdout", "stderr")}
            else:
                yield ev
    finally:
        executor.finish(job)
    exec_info = {
        "run_id": job.run_id, "tool": spec.name, "command": end.get("argv"),
        "exit_code": end.get("exit_code"), "exit_meaning": end.get("exit_meaning"),
        "duration_ms": end.get("duration_ms"), "timed_out": end.get("timed_out"), "cancelled": end.get("cancelled"),
    }
    return end, exec_info


def _process_problem(end, exec_info, spec):
    if end.get("error"):
        exec_info["error"] = end["error"]
        return "failed", end["error"]
    if end.get("timed_out"):
        return "timeout", f"The CLI did not finish within {spec.timeout_s}s and was stopped. No result is claimed."
    if end.get("cancelled"):
        return "cancelled", "You cancelled the task; the process was killed. No result is claimed."
    return None


def _load_result(spec, end, plan):
    """Return (normalised_result | None, reason)."""
    if spec.name == "crawl4ai":
        if end.get("exit_code") != 0:
            return None, f"crwl exited with code {end.get('exit_code')}"
        file_text = None
        if end.get("result_file"):
            try:
                with open(end["result_file"], encoding="utf-8", errors="replace") as f:
                    file_text = f.read()
            except OSError:
                pass
        try:
            items = crawl4ai_adapter.load_output(file_text, end.get("stdout", ""))
            raw = crawl4ai_adapter.normalise(items, plan.url, end.get("duration_ms") or 0,
                                             {"max_pages": plan.max_pages, "max_depth": plan.max_depth,
                                              "timeout_s": spec.timeout_s, "robots_respected": True})
        except crawl4ai_adapter.AdapterError as e:
            return None, str(e)
        return raw, None if raw["pages"] else (raw.get("error") or "no pages")
    raw = None
    if end.get("result_file"):
        try:
            with open(end["result_file"], encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            raw = None
    if raw is None or not REQUIRED_CRAWL_KEYS.issubset(raw):
        return None, _last_stderr_error(end.get("stderr", "")) or f"exit code {end.get('exit_code')} ({end.get('exit_meaning')}), no valid JSON"
    if not raw.get("pages"):
        return raw, raw.get("error") or _last_stderr_error(end.get("stderr", "")) or "no pages"
    return raw, None


# --------------------------------------------------------------------------- crawl

def _finish_crawl(message, plan, end, exec_info, raw) -> Iterator[dict]:
    if raw is None or not raw.get("pages"):
        attempts = exec_info.get("attempts", [])
        reason = (raw or {}).get("error") or (attempts[-1].get("reason") if attempts else None) or end.get("exit_meaning")
        status = {"blocked_by_robots": "blocked", "invalid_url": "invalid_input"}.get((raw or {}).get("status"), "failed")
        yield _fail(message, plan, exec_info, status, _friendly_crawl_error((raw or {}).get("status"), reason),
                    raw_errors=(raw or {}).get("errors"))
        return

    status = "partial" if raw.get("errors") else "success"
    yield {"type": "status", "message": f"Crawl verified ({raw['stats']['pages_crawled']} pages via {LABEL.get(exec_info['tool'], exec_info['tool'])}) - writing summary"}
    yield {"type": "summary_start", "llm": summarizer.llm.available()}
    gen = summarizer.summarise_crawl_stream(raw, plan.focus, message)
    try:
        while True:
            yield {"type": "summary_delta", "text": next(gen)}
    except StopIteration as stop:
        summary = stop.value

    data = {"site": raw["site"], "stats": raw["stats"], "pages": [_slim_page(p) for p in raw["pages"]],
            "errors": raw["errors"], "limits": raw.get("limits"), "crawler": raw.get("tool")}
    envelope = _envelope(message, plan, status, exec_info, summary, data)
    json.loads(json.dumps(envelope))  # guarantee syntactically valid JSON
    (config.RUNS_DIR / exec_info["run_id"] / "response.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    headline = (f"Crawl completed · {raw['stats']['pages_crawled']} page(s) verified" if status == "success" else
                f"Crawl completed · {raw['stats']['pages_crawled']} of {raw['stats']['pages_crawled'] + len(raw['errors'])} pages succeeded")
    yield {"type": "final", "status": status, "headline": headline, "summary": summary,
           "json": envelope if plan.want_json else None, "json_requested": plan.want_json,
           "show_summary": plan.want_summary}


def _slim_page(p: dict) -> dict:
    return {
        "url": p.get("final_url") or p.get("url"),
        "status_code": p.get("status_code"),
        "depth": p.get("depth"),
        "title": p.get("title"),
        "meta_description": p.get("meta_description"),
        "headings": {k: v[:8] for k, v in (p.get("headings") or {}).items() if v},
        "key_paragraphs": [x[:500] for x in (p.get("key_paragraphs") or [])[:4]],
        "word_count": p.get("word_count"),
        "contacts": p.get("contacts"),
        "links": {"internal": p.get("links", {}).get("internal_count"),
                  "external": p.get("links", {}).get("external_count")},
        "fetch_ms": p.get("fetch_ms"),
    }


def _friendly_crawl_error(status: str | None, reason: str | None) -> str:
    if status == "blocked_by_robots":
        return "The site's robots.txt does not allow crawling this URL, so I did not crawl it."
    if status == "invalid_url":
        return f"The crawler refused the URL: {reason}."
    return f"I couldn't crawl the site: {reason}. Check that the URL is correct and publicly accessible."


def _last_stderr_error(stderr: str) -> str | None:
    for line in reversed(stderr.splitlines()):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "error":
            return ev.get("message")
    return None


# --------------------------------------------------------------------------- check

def _finish_check(message, plan, end, exec_info) -> Iterator[dict]:
    info = parse_curl(end.get("stdout", ""), end.get("stderr", ""), plan.url, end["exit_code"])
    summary = summarizer.summarise_check(info)
    status = "success" if info["reachable"] else "failed"
    envelope = _envelope(message, plan, status, exec_info, summary, info)
    yield {"type": "final", "status": status, "headline": "Site check completed" if info["reachable"] else "Site check failed",
           "summary": summary, "json": envelope, "json_requested": True, "show_summary": True}


def parse_curl(stdout: str, stderr: str, url: str, exit_code: int | None) -> dict:
    info = {"url": url, "reachable": False, "http_status": None, "reason": None, "headers": {}}
    meta = re.search(r"__CURL_META__ (.*)", stdout)
    if meta:
        for kv in meta.group(1).split():
            k, _, v = kv.partition("=")
            if k == "time_total":
                try:
                    info["time_total_s"] = round(float(v), 3)
                except ValueError:
                    pass
            elif k == "remote_ip" and v:
                info["remote_ip"] = v
    blocks = [b for b in re.split(r"\r?\n\r?\n", stdout.split("__CURL_META__")[0]) if b.strip().startswith("HTTP/")]
    if blocks:
        lines = blocks[-1].strip().splitlines()
        m = re.match(r"HTTP/[\d.]+\s+(\d{3})\s*(.*)", lines[0])
        if m:
            info["http_status"] = int(m.group(1))
            info["reason"] = m.group(2).strip() or None
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip().lower()
                if k in ("set-cookie",):
                    continue  # never echo cookies
                info["headers"][k] = v.strip()[:300]
        info["reachable"] = info["http_status"] is not None
        info["server"] = info["headers"].get("server")
        info["content_type"] = info["headers"].get("content-type")
        info["location"] = info["headers"].get("location")
    if exit_code not in (0, None) or not info["reachable"]:
        info["error"] = (stderr.strip().splitlines() or [f"curl exit code {exit_code}"])[-1][:300]
    return info


# --------------------------------------------------------------------------- shared

def _fail(message, plan, exec_info, status, text, raw_errors=None) -> dict:
    env = _envelope(message, plan, status, exec_info, None, {"errors": raw_errors} if raw_errors else None, error=text)
    return {"type": "final", "status": status, "text": text, "json": env, "json_requested": True}


def _envelope(message, plan, status, exec_info, summary, data, error=None) -> dict:
    return {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {
            "message": message[:500],
            "intent": plan.intent,
            "url": plan.url,
            "parameters": {"max_pages": plan.max_pages, "max_depth": plan.max_depth, "focus": plan.focus},
            "interpreted_by": plan.parser_note,
        },
        "execution": exec_info,
        "summary": summary,
        "data": data,
        "error": error,
    }
