"""Tests: run with  `pytest -q`  from the project root.

They spin up a local HTTP server serving tests/fixture_site, so no internet is needed.
ALLOW_PRIVATE_HOSTS is enabled ONLY inside the tests so 127.0.0.1 can be crawled.
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

import pytest

os.environ["ALLOW_PRIVATE_HOSTS"] = "1"
os.environ["LLM_PROVIDER"] = "none"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import config, executor, intent, tools  # noqa: E402
from backend.app import app  # noqa: E402
from backend.validators import ValidationError, extract_url, validate_url  # noqa: E402


class SlowHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(30)
        return super().do_GET()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="session")
def site():
    handler = partial(SlowHandler, directory=str(ROOT / "tests" / "fixture_site"))
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def chat(client, msg):
    r = client.post("/api/chat", json={"message": msg})
    assert r.status_code == 200
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    final = next(e for e in events if e["type"] == "final")
    return events, final


# ---------------------------------------------------------------- validators
def test_validate_url_rules():
    assert validate_url("http://127.0.0.1:9/x").startswith("http://")
    with pytest.raises(ValidationError):
        validate_url("ftp://example.com")
    with pytest.raises(ValidationError):
        validate_url("http://user:pass@example.com")
    with pytest.raises(ValidationError):
        validate_url("http://exa mple.com")


def test_extract_url():
    assert extract_url("please crawl https://example.com/about, thanks") == "https://example.com/about"
    assert extract_url("crawl ftp://x.org") == "ftp://x.org"
    assert extract_url("mail me at a@b.com") is None


# ---------------------------------------------------------------- intent
def test_intent_rules():
    p = intent.parse("Crawl https://example.com, summarise it, 3 pages, depth 2, return JSON")
    assert p.intent == "crawl_site" and p.max_pages == 3 and p.max_depth == 2
    assert intent.parse("is https://example.com up?").intent == "check_site"
    assert intent.parse("run rm -rf / now").intent == "unsupported"
    assert intent.parse("crawl the website please").intent == "chat"
    assert intent.parse("scrape 999 pages of https://example.com").max_pages == config.CRAWL_MAX_PAGES


def test_no_shell_and_argv_list():
    argv = tools.TOOLS["crawl_site"].build_argv({"url": "https://a.com/;rm -rf /", "max_pages": 2, "max_depth": 1}, Path("o.json"))
    assert isinstance(argv, list) and "https://a.com/;rm -rf /" in argv  # passed as ONE literal argument


def test_secrets_stripped_from_child_env():
    os.environ["GEMINI_API_KEY"] = "super-secret"
    assert "GEMINI_API_KEY" not in executor.safe_env()


# ---------------------------------------------------------------- CLI directly
def test_cli_crawl(site, tmp_path):
    out = tmp_path / "r.json"
    p = subprocess.run([sys.executable, str(ROOT / "cli" / "sitecrawl.py"), "--url", site + "/", "--max-pages", "5",
                        "--allow-private", "--delay", "0", "--output", str(out)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0
    data = json.loads(out.read_text())
    assert data["stats"]["pages_crawled"] == 4
    assert "sales@acmerobotics.example" in data["site"]["emails"]
    assert any(e["error"] == "HTTP 404" for e in data["errors"])


def test_cli_blocks_private_without_flag(site, tmp_path):
    p = subprocess.run([sys.executable, str(ROOT / "cli" / "sitecrawl.py"), "--url", site + "/", "--output", str(tmp_path / "x.json")],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 2


def test_cli_robots(site, tmp_path):
    p = subprocess.run([sys.executable, str(ROOT / "cli" / "sitecrawl.py"), "--url", site + "/private/a", "--allow-private",
                        "--output", str(tmp_path / "x.json")], capture_output=True, text=True, timeout=30)
    assert p.returncode == 4


# ---------------------------------------------------------------- end-to-end API
def test_e2e_crawl(site):
    c = TestClient(app)
    events, final = chat(c, f"Crawl {site}/, extract the important information, give me a summary, and return JSON")
    types = [e["type"] for e in events]
    assert types[0] == "plan" and "exec_start" in types and "exec_end" in types
    assert final["status"] == "partial"
    env = final["json"]
    assert env["execution"]["exit_code"] == 0
    assert env["data"]["stats"]["pages_crawled"] == 4
    assert final["summary"]["suspicious_content_detected"] is True
    json.dumps(env)


def test_e2e_check_site(site):
    _, final = chat(TestClient(app), f"is {site}/ up? show headers")
    assert final["status"] == "success" and final["json"]["data"]["http_status"] == 200


def test_e2e_unreachable():
    _, final = chat(TestClient(app), "crawl http://127.0.0.1:1/ and give json")
    assert final["status"] == "failed" and final["json"]["execution"]["exit_code"] == 3


def test_timeout(site, monkeypatch):
    monkeypatch.setattr(tools.TOOLS["crawl_site"], "timeout_s", 3)
    _, final = chat(TestClient(app), f"crawl {site}/slow/page")
    assert final["status"] == "timeout" and final["json"]["execution"]["timed_out"] is True


def test_cancel(site):
    job = executor.new_job()
    spec = tools.TOOLS["crawl_site"]
    threading.Timer(1.5, lambda: executor.cancel(job.run_id)).start()
    end = None
    for ev in executor.run(spec, {"url": site + "/slow/x", "max_pages": 1, "max_depth": 0}, job):
        if ev["type"] == "exec_end":
            end = ev
    assert end["cancelled"] is True and end["ok"] is False


# ---------------------------------------------------------------- LLM path (mocked, no network)
def test_llm_intent_and_summary_path(site, monkeypatch):
    from backend import llm, summarizer
    monkeypatch.setattr(llm, "available", lambda: True)
    calls = []

    def fake_json(system, user):
        calls.append(system[:40])
        if "intent parser" in system:
            # model tries to sneak in a shell command - must be ignored, only the tool name is used
            return {"intent": "crawl_site", "url": f"{site}/", "max_pages": 99, "max_depth": 9,
                    "want_summary": True, "want_json": True, "focus": "products", "command": "rm -rf /"}
        assert "<untrusted_website_content>" in user
        return {"summary": "Acme makes warehouse robots.", "key_points": ["M1 mover", "P2 picker"],
                "site_type": "company", "focus_findings": "M1 and P2", "suspicious_content": True}

    monkeypatch.setattr(llm, "complete_json", fake_json)

    def fake_stream(system, user):
        calls.append("stream")
        assert "<untrusted_website_content>" in user
        for chunk in ["Acme makes ", "warehouse robots.\n\nKEY POINTS\n- M1 mover\n- P2 picker\n",
                      "SITE TYPE: company\nFOCUS: M1 and P2\nSUSPICIOUS: yes\n"]:
            yield chunk
    monkeypatch.setattr(llm, "stream", fake_stream)
    events, final = chat(TestClient(app), "tell me what this company sells " + site)
    assert sum(1 for e in events if e["type"] == "summary_delta") == 3
    assert final["status"] == "partial"
    assert final["json"]["request"]["parameters"]["max_pages"] == config.CRAWL_MAX_PAGES  # clamped
    assert final["json"]["request"]["parameters"]["max_depth"] == config.CRAWL_MAX_DEPTH
    assert final["summary"]["generated_by"] == "llm" and len(calls) == 2
    assert final["summary"]["key_points"] == ["M1 mover", "P2 picker"] and final["summary"]["site_type"] == "company"


def test_llm_failure_falls_back(site, monkeypatch):
    from backend import llm
    monkeypatch.setattr(llm, "available", lambda: True)

    def boom(*a, **k):
        raise llm.LLMError("HTTP 429")
    monkeypatch.setattr(llm, "complete_json", boom)
    monkeypatch.setattr(llm, "stream", boom)
    _, final = chat(TestClient(app), f"crawl {site}/ and summarise")
    assert final["status"] == "partial"
    assert "rule-based" in final["json"]["request"]["interpreted_by"]
    assert "extractive" in final["summary"]["generated_by"]



# ---------------------------------------------------------------- Crawl4AI (fake `crwl` binary, no browser needed)
FAKE_CRWL = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_CRWL_MODE", "ok")
print("[INIT].... crawl4ai fake", flush=True)
if mode == "fail":
    print("browser launch failed", file=sys.stderr); sys.exit(1)
out = args[args.index("-O") + 1]
url = args[0]
data = [
  {"url": url, "success": True, "status_code": 200,
   "metadata": {"title": "Fake Site", "description": "A JS-rendered site", "og:site_name": "Fake"},
   "markdown": {"raw_markdown": "# Fake Site\n\nWe build rendered pages for testing the adapter end to end.\n\nContact hello@fake.example now."},
   "links": {"internal": [{"href": url + "about"}], "external": [{"href": "https://github.com/fake"}]}},
  {"url": url + "missing", "success": False, "status_code": 404, "error_message": "HTTP 404"}
]
open(out, "w").write(json.dumps(data if mode != "garbage" else "not a result"))
"""


@pytest.fixture
def fake_crwl(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("fake crwl uses a POSIX shebang")
    b = tmp_path / "bin"; b.mkdir()
    f = b / "crwl"; f.write_text(FAKE_CRWL); f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(config, "CRAWLER", "auto")
    return f


def test_crawl4ai_primary(site, fake_crwl, monkeypatch):
    monkeypatch.setenv("FAKE_CRWL_MODE", "ok")
    events, final = chat(TestClient(app), f"crawl {site}/ and give me json")
    env = final["json"]
    assert env["execution"]["tool"] == "crawl4ai" and env["execution"]["command"][1:3] == [site + "/", "-o"]
    assert final["status"] == "partial" and env["data"]["stats"]["pages_crawled"] == 1
    assert env["data"]["site"]["emails"] == ["hello@fake.example"]
    assert any(e["type"] == "log" and "crawl4ai fake" in e["line"] for e in events)  # stdout streamed live


def test_crawl4ai_failure_falls_back_to_sitecrawl(site, fake_crwl, monkeypatch):
    monkeypatch.setenv("FAKE_CRWL_MODE", "fail")
    events, final = chat(TestClient(app), f"crawl {site}/ and give me json")
    env = final["json"]
    assert env["execution"]["tool"] == "crawl_site" and final["status"] == "partial"
    assert [a["tool"] for a in env["execution"]["attempts"]] == ["crawl4ai", "crawl_site"]
    assert any(e["type"] == "status" and "falling back" in e["message"] for e in events)


def test_crawl4ai_garbage_output_falls_back(site, fake_crwl, monkeypatch):
    monkeypatch.setenv("FAKE_CRWL_MODE", "garbage")
    _, final = chat(TestClient(app), f"crawl {site}/")
    assert final["json"]["execution"]["tool"] == "crawl_site"


def test_structured_items_and_price_stats():
    from backend import structure
    pages = [{"url": "u1", "items": ["Tipping the Velvet — £53.74 · In stock", "A Light in the Attic — £51.77 · In stock",
                                     "Cheap Book — £10.00", "No Price Heading — In stock"]}]
    items = structure.extract_items(pages)
    assert items[0] == {"name": "Tipping the Velvet", "price": "£53.74", "price_value": 53.74, "details": "In stock", "page": "u1"}
    ps = structure.price_stats(items)
    assert ps["items_with_price"] == 3 and ps["cheapest"]["name"] == "Cheap Book" and ps["most_expensive"]["price"] == "£53.74"


def test_parse_stream_text_tolerant_of_one_line_output():
    from backend.summarizer import parse_stream_text
    out = parse_stream_text("Overview text. - Point A here. - Point B. SITE TYPE: blog FOCUS: none SUSPICIOUS: no")
    assert out["text"] == "Overview text."
    assert len(out["key_points"]) == 2
    assert out["site_type"] == "blog"
    assert out["focus_findings"] is None


def test_phoneish_rejects_year_ranges():
    from backend.crawl4ai_adapter import phoneish
    assert phoneish("(1800-1849") is False
    assert phoneish("+91 98765 43210") is True


def test_adapter_items_reach_the_summary_digest():
    from backend import crawl4ai_adapter, summarizer
    md = "# All products\n**1000** results - showing **1** to **20**.\n### [Tipping the Velvet](u)\n£53.74\nIn stock\n"
    raw = crawl4ai_adapter.normalise(
        [{"url": "https://shop.example/all", "success": True, "status_code": 200, "markdown": {"raw_markdown": md}}],
        "https://shop.example/all", 10, {"max_pages": 5, "max_depth": 1})
    assert "Tipping the Velvet — £53.74 · In stock" in raw["pages"][0]["items"]
    text = summarizer.digest(raw)
    assert "Tipping the Velvet — £53.74 · In stock" in text
    assert "1000 results" in text


def test_cli_extracts_shop_items_and_prices(site, tmp_path):
    out = tmp_path / "shop.json"
    p = subprocess.run([sys.executable, str(ROOT / "cli" / "sitecrawl.py"), "--url", site + "/shop.html",
                        "--max-pages", "1", "--max-depth", "0", "--allow-private", "--delay", "0",
                        "--output", str(out)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0
    page = json.loads(out.read_text(encoding="utf-8"))["pages"][0]
    assert page["items"][0].startswith("A Light in the Attic — £51.77")
    assert "£51.77" in page["prices"]
    assert page["contacts"]["phones"] == []


def test_followup_reuses_previous_site(site):
    r = TestClient(app).post("/api/chat", json={"message": "i want summary", "last_url": site + "/"})
    assert r.status_code == 200
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    plan = next(e["plan"] for e in events if e["type"] == "plan")
    assert plan["intent"] == "crawl_site" and plan["url"] == site + "/"


def test_nat64_and_mapped_addresses_judged_by_embedded_ipv4():
    import ipaddress
    from backend.validators import _effective_ip
    assert str(_effective_ip(ipaddress.ip_address("64:ff9b::23d3:7a6d"))) == "35.211.122.109"   # public -> allowed
    assert _effective_ip(ipaddress.ip_address("64:ff9b::a00:1")).is_private                       # 10.0.0.1 -> still blocked
    assert _effective_ip(ipaddress.ip_address("::ffff:127.0.0.1")).is_loopback                     # mapped loopback blocked
    assert _effective_ip(ipaddress.ip_address("64:ff9b::a9fe:a9fe")).is_link_local                 # 169.254.169.254 blocked
