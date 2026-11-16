#!/usr/bin/env python3
"""
sitecrawl - a small, safe, standalone website crawler CLI.

This is the CLI tool that the chat backend invokes as a separate OS process.
It can also be used by hand from a terminal:

    python cli/sitecrawl.py --url https://example.com --max-pages 5 --max-depth 1 --output out.json

Contract with the caller (the backend):
  * Arguments are passed as an argv list (never through a shell).
  * Progress is streamed to STDERR as one JSON object per line ("JSON lines").
  * The final structured result is written to the --output file (UTF-8 JSON).
    If --output is omitted, the result is printed to STDOUT instead.
  * Exit codes:
        0  success (at least one page crawled; some pages may have failed)
        2  invalid arguments / invalid URL
        3  start URL unreachable or returned no usable HTML
        4  blocked by robots.txt
        5  unexpected internal error

Safety features:
  * Only http/https URLs; private, loopback, link-local and reserved IPs are
    refused (SSRF protection), including after every redirect.
  * robots.txt is respected by default.
  * Hard limits on pages, depth, response size, per-request timeout, redirects.
  * Only text/html responses are parsed. Page content is treated as data only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.stderr.write(json.dumps({"event": "error", "message": "Missing dependency: pip install requests beautifulsoup4"}) + "\n")
    sys.exit(5)

_NAT64 = ipaddress.ip_network("64:ff9b::/96")   # RFC 6052 well-known NAT64 prefix


def _effective_ip(ip):
    """NAT64 / IPv4-mapped IPv6 addresses embed a real IPv4 address: judge that one."""
    if ip.version == 6:
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


TOOL_NAME = "sitecrawl"
TOOL_VERSION = "1.0.0"

EXIT_OK, EXIT_BAD_ARGS, EXIT_UNREACHABLE, EXIT_ROBOTS, EXIT_INTERNAL = 0, 2, 3, 4, 5

HARD_MAX_PAGES = 50
HARD_MAX_DEPTH = 3
MAX_BYTES = 2_000_000          # 2 MB per page
MAX_REDIRECTS = 5
MAX_TEXT_CHARS = 4000          # per page main text kept in output

# Pages that usually carry the "important information" of a site get crawled first.
PRIORITY_KEYWORDS = [
    "about", "company", "who-we-are", "team", "contact", "services", "service",
    "products", "product", "solutions", "features", "pricing", "plans", "faq",
    "careers", "blog", "news", "docs", "documentation", "mission",
]
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip", ".rar",
    ".gz", ".tar", ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".css", ".js", ".json",
    ".xml", ".rss", ".woff", ".woff2", ".ttf", ".eot", ".exe", ".dmg", ".apk", ".doc",
    ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)
SOCIAL_DOMAINS = (
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "github.com", "tiktok.com", "pinterest.com", "medium.com", "t.me",
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
PRICE_RE = re.compile(r"(?:[£$\u20ac\u20b9]\s?\d[\d,]*(?:\.\d{1,2})?|\b\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP|INR|Rs\.?)\b)")
UTILITY_URL = re.compile(r"(/(login|log-in|signin|sign-in|signup|sign-up|register|enter|logout|log-out|cart|basket|checkout|"
                         r"account|my-account|password|auth|oauth|wp-login\.php|wp-admin)(/|\?|$|\.))|[?&](signup|state=new-user|redirect_to=)", re.I)


def phoneish(s: str) -> bool:
    """Real phone numbers have 9-15 digits; reject year ranges like 1800-1849."""
    digits = re.sub(r"\D", "", s)
    if not 9 <= len(digits) <= 15:
        return False
    return not re.fullmatch(r"\(?\s*(1[5-9]|20)\d{2}\s*[-\u2013\u2014]\s*(1[5-9]|20)\d{2}\s*\)?", s.strip())


PHONE_RE = re.compile(r"(?<![\w.])\+?\(?\d[\d\s().-]{6,18}\d(?![\w.])")


# --------------------------------------------------------------------------- utils

def log(event: str, **data) -> None:
    """Progress line on stderr (ASCII-safe JSON so any console can print it)."""
    payload = {"event": event, "ts": round(time.time(), 3), **data}
    sys.stderr.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stderr.flush()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def host_is_public(host: str) -> tuple[bool, str]:
    """Resolve a hostname and make sure every address is a public IP."""
    if not host:
        return False, "missing host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"DNS lookup failed for '{host}'"
    for info in infos:
        ip = _effective_ip(ipaddress.ip_address(info[4][0].split("%")[0]))
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return False, f"'{host}' resolves to a non-public address ({ip})"
    return True, "ok"


def normalise_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    path = p.path or "/"
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=path).geturl()


def same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).hostname or "", urlparse(b).hostname or ""
    strip = lambda h: h[4:] if h.startswith("www.") else h
    return strip(ha) == strip(hb)


def link_priority(url: str) -> int:
    path = urlparse(url).path.lower()
    for i, kw in enumerate(PRIORITY_KEYWORDS):
        if kw in path:
            return i
    return len(PRIORITY_KEYWORDS) + path.count("/")


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# --------------------------------------------------------------------------- crawler

class Crawler:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": args.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en;q=0.9,*;q=0.5",
        })
        self.robots: robotparser.RobotFileParser | None = None
        self.errors: list[dict] = []
        self.pages: list[dict] = []

    # ---- safety ---------------------------------------------------------
    def check_url_allowed(self, url: str) -> tuple[bool, str]:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, f"scheme '{p.scheme}' not allowed"
        if p.username or p.password:
            return False, "credentials in URL are not allowed"
        if not self.args.allow_private:
            ok, why = host_is_public(p.hostname or "")
            if not ok:
                return False, why
        return True, "ok"

    def load_robots(self, start_url: str) -> None:
        if self.args.ignore_robots:
            log("robots", status="ignored")
            return
        p = urlparse(start_url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        rp = robotparser.RobotFileParser()
        try:
            r = self.session.get(robots_url, timeout=self.args.timeout, allow_redirects=True)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                log("robots", status="loaded", url=robots_url)
            elif r.status_code in (401, 403):
                rp.disallow_all = True
                log("robots", status="access_denied_treated_as_disallow", code=r.status_code)
            else:
                rp.allow_all = True
                log("robots", status="not_found_allow_all", code=r.status_code)
        except requests.RequestException as e:
            rp.allow_all = True
            log("robots", status="unavailable_allow_all", error=type(e).__name__)
        self.robots = rp

    def robots_allows(self, url: str) -> bool:
        return True if self.robots is None else self.robots.can_fetch(self.args.user_agent, url)

    # ---- fetching -------------------------------------------------------
    def fetch(self, url: str) -> tuple[requests.Response | None, str, str | None]:
        """GET with manual redirect handling so every hop is SSRF-checked."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            ok, why = self.check_url_allowed(current)
            if not ok:
                return None, current, f"blocked: {why}"
            try:
                r = self.session.get(current, timeout=self.args.timeout, allow_redirects=False, stream=True)
            except requests.exceptions.Timeout:
                return None, current, "timeout"
            except requests.exceptions.SSLError:
                return None, current, "TLS/SSL error"
            except requests.exceptions.ConnectionError:
                return None, current, "connection failed"
            except requests.RequestException as e:
                return None, current, f"request error: {type(e).__name__}"
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                r.close()
                if not loc:
                    return None, current, "redirect without Location"
                current = normalise_url(urljoin(current, loc))
                log("redirect", to=current)
                continue
            # bounded body read
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                r.close()
                return r, current, f"skipped non-HTML content ({ctype.split(';')[0] or 'unknown'})"
            chunks, size = [], 0
            try:
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        r.close()
                        return None, current, "response too large"
                    chunks.append(chunk)
            except requests.RequestException as e:
                return None, current, f"read error: {type(e).__name__}"
            r._content = b"".join(chunks)
            return r, current, None
        return None, current, "too many redirects"

    # ---- extraction -----------------------------------------------------
    def extract(self, url: str, resp: requests.Response, depth: int) -> tuple[dict, list[str]]:
        if "charset=" in resp.headers.get("Content-Type", "").lower():
            soup = BeautifulSoup(resp.content.decode(resp.encoding, errors="replace"), "html.parser")
        else:
            soup = BeautifulSoup(resp.content, "html.parser")   # reads <meta charset>, avoids "£"

        def meta(name=None, prop=None):
            tag = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop})
            return clean_text(tag.get("content", "")) if tag and tag.get("content") else None

        title = clean_text(soup.title.get_text()) if soup.title else None
        html_tag = soup.find("html")
        lang = html_tag.get("lang") if html_tag else None
        canonical = soup.find("link", rel=lambda v: v and "canonical" in v)

        # links (collect before removing nav/footer so we can still follow them)
        internal, external, social = [], [], set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absu = normalise_url(urljoin(url, href))
            if urlparse(absu).scheme not in ("http", "https"):
                continue
            host = urlparse(absu).hostname or ""
            if any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS):
                social.add(absu)
            if same_site(absu, self.args.url):
                internal.append(absu)
            else:
                external.append(absu)

        mailtos = {a["href"][7:].split("?")[0] for a in soup.find_all("a", href=True) if a["href"].lower().startswith("mailto:")}
        tels = {clean_text(a["href"][4:]) for a in soup.find_all("a", href=True) if a["href"].lower().startswith("tel:")}

        for tag in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
            tag.decompose()

        items = []
        for card in soup.find_all(["article", "li"]):
            heads = card.find_all(["h2", "h3", "h4"])
            if len(heads) != 1 or len(card.get_text(" ", strip=True)) > 400:
                continue
            a = heads[0].find("a")
            name = clean_text((a.get("title") if a and a.get("title") else heads[0].get_text(" ")))
            data = []
            for el in card.find_all(["p", "span", "div"]):
                t = clean_text(el.get_text(" "))
                if t and t != name and len(t) <= 40 and t not in data and not el.find(["p", "div"]):
                    data.append(t)
            if name and data:
                item = name + " \u2014 " + " \u00b7 ".join(data[:4])
                if item not in items:
                    items.append(item)

        headings = {}
        for level in ("h1", "h2", "h3"):
            vals = [clean_text(h.get_text()) for h in soup.find_all(level)]
            headings[level] = [v for v in dict.fromkeys(vals) if v][:15]

        main = soup.find("main") or soup.find("article") or soup.body or soup
        for tag in main.find_all(["nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        paragraphs = []
        for p in main.find_all(["p", "li"]):
            t = clean_text(p.get_text(" "))
            if len(t) >= 40 and t not in paragraphs:
                paragraphs.append(t)
        full_text = clean_text(soup.get_text(" "))
        main_text = " ".join(paragraphs)[:MAX_TEXT_CHARS]

        emails = sorted({e for e in EMAIL_RE.findall(full_text) if not e.lower().endswith((".png", ".jpg", ".gif", ".webp"))} | mailtos)[:20]
        phones = sorted({clean_text(p) for p in PHONE_RE.findall(full_text) if phoneish(p)} | tels)[:10]
        prices = list(dict.fromkeys(PRICE_RE.findall(full_text)))[:50]
        facts = []
        for el in soup.find_all(["p", "span", "strong", "li", "div"]):
            if el.find(["p", "div", "li"]):
                continue
            t = clean_text(el.get_text(" "))
            if 4 <= len(t) <= 80 and re.search(r"\d|[£$\u20ac\u20b9]", t) and t not in facts:
                facts.append(t)
            if len(facts) >= 20:
                break

        page = {
            "url": url,
            "final_url": resp.url if resp.url else url,
            "status_code": resp.status_code,
            "depth": depth,
            "title": title,
            "meta_description": meta(name="description") or meta(prop="og:description"),
            "language": lang,
            "canonical": canonical.get("href") if canonical else None,
            "open_graph": {k: v for k, v in {
                "title": meta(prop="og:title"), "type": meta(prop="og:type"),
                "site_name": meta(prop="og:site_name"), "image": meta(prop="og:image"),
            }.items() if v},
            "headings": headings,
            "key_paragraphs": paragraphs[:8],
            "items": items[:40], "facts": facts[:20], "prices": prices,
            "main_text": main_text,
            "word_count": len(full_text.split()),
            "links": {
                "internal_count": len(set(internal)),
                "external_count": len(set(external)),
                "sample_external": sorted(set(external))[:10],
            },
            "images_count": len(soup.find_all("img")),
            "contacts": {"emails": emails, "phones": phones},
            "social_links": sorted(social)[:15],
        }
        return page, list(dict.fromkeys(internal))

    # ---- main loop ------------------------------------------------------
    def run(self) -> tuple[dict, int]:
        a = self.args
        started = time.time()
        start_url = normalise_url(a.url)
        ok, why = self.check_url_allowed(start_url)
        if not ok:
            log("error", message=f"start URL refused: {why}")
            return self.result(start_url, started, "invalid_url", why), EXIT_BAD_ARGS

        self.load_robots(start_url)
        if not self.robots_allows(start_url):
            log("error", message="robots.txt disallows the start URL")
            return self.result(start_url, started, "blocked_by_robots", "robots.txt disallows crawling this URL"), EXIT_ROBOTS

        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        seen = {start_url}
        workers = max(1, min(a.concurrency, 5))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while queue and len(self.pages) < a.max_pages:
                # take a batch of URLs (never more than the remaining page budget)
                batch = []
                while queue and len(batch) < min(workers, a.max_pages - len(self.pages)):
                    url, depth = queue.popleft()
                    if not self.robots_allows(url):
                        self.errors.append({"url": url, "error": "disallowed by robots.txt"})
                        log("skip", url=url, reason="robots")
                        continue
                    batch.append((url, depth))
                if not batch:
                    continue
                for i, (url, depth) in enumerate(batch):
                    log("fetch", url=url, depth=depth, n=len(self.pages) + i + 1, max=a.max_pages)
                t0 = time.time()
                results = list(pool.map(lambda ud: (ud, self.fetch(ud[0]), time.time()), batch))
                for (url, depth), (resp, final_url, err), t_done in results:
                    if err or resp is None:
                        self.errors.append({"url": url, "error": err or "unknown"})
                        log("page_error", url=url, error=err)
                        continue
                    if resp.status_code >= 400:
                        self.errors.append({"url": url, "error": f"HTTP {resp.status_code}"})
                        log("page_error", url=url, error=f"HTTP {resp.status_code}")
                        continue
                    try:
                        page, links = self.extract(final_url, resp, depth)
                    except Exception as e:  # parsing should never kill the crawl
                        self.errors.append({"url": url, "error": f"parse error: {type(e).__name__}"})
                        log("page_error", url=url, error="parse error")
                        continue
                    page["fetch_ms"] = int((t_done - t0) * 1000)
                    self.pages.append(page)
                    log("page_ok", url=final_url, title=(page["title"] or "")[:80], words=page["word_count"], ms=page["fetch_ms"])
                    if depth < a.max_depth:
                        candidates = [
                            l for l in links
                            if l not in seen and same_site(l, start_url)
                            and not urlparse(l).path.lower().endswith(SKIP_EXTENSIONS)
                            and not UTILITY_URL.search(l)
                        ]
                        candidates.sort(key=link_priority)
                        for l in candidates:
                            seen.add(l)
                            queue.append((l, depth + 1))
                if a.delay and queue:
                    time.sleep(a.delay)

        if not self.pages:
            reason = self.errors[0]["error"] if self.errors else "no pages"
            log("error", message=f"no page could be crawled: {reason}")
            return self.result(start_url, started, "unreachable", reason), EXIT_UNREACHABLE

        status = "partial" if self.errors else "success"
        log("done", pages=len(self.pages), errors=len(self.errors), status=status)
        return self.result(start_url, started, status, None), EXIT_OK

    def result(self, start_url: str, started: float, status: str, error: str | None) -> dict:
        pages = self.pages
        first = pages[0] if pages else {}
        og = first.get("open_graph", {}) if first else {}
        all_emails = sorted({e for p in pages for e in p["contacts"]["emails"]})[:20]
        all_phones = sorted({ph for p in pages for ph in p["contacts"]["phones"]})[:10]
        all_social = sorted({s for p in pages for s in p["social_links"]})[:20]
        return {
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "status": status,
            "error": error,
            "start_url": start_url,
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
            "finished_at": now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "limits": {
                "max_pages": self.args.max_pages, "max_depth": self.args.max_depth,
                "timeout_s": self.args.timeout, "robots_respected": not self.args.ignore_robots,
            },
            "stats": {"pages_crawled": len(pages), "pages_failed": len(self.errors),
                      "total_words": sum(p["word_count"] for p in pages)},
            "site": {
                "name": og.get("site_name") or first.get("title"),
                "title": first.get("title"),
                "description": first.get("meta_description"),
                "language": first.get("language"),
                "emails": all_emails,
                "phones": all_phones,
                "social_links": all_social,
            },
            "pages": pages,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=TOOL_NAME, description="Safe website crawler that outputs structured JSON.")
    p.add_argument("--url", required=True, help="Start URL (http/https)")
    p.add_argument("--max-pages", type=int, default=5, help=f"Pages to crawl (1-{HARD_MAX_PAGES})")
    p.add_argument("--max-depth", type=int, default=1, help=f"Link depth from start URL (0-{HARD_MAX_DEPTH})")
    p.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds (1-30)")
    p.add_argument("--delay", type=float, default=0.2, help="Politeness delay between request batches (0-5 s)")
    p.add_argument("--concurrency", type=int, default=3, help="Parallel requests per batch (1-5)")
    p.add_argument("--output", help="Write result JSON to this file (default: stdout)")
    p.add_argument("--user-agent", default=f"{TOOL_NAME}/{TOOL_VERSION} (+educational crawler; respects robots.txt)")
    p.add_argument("--ignore-robots", action="store_true", help="Do not check robots.txt (not recommended)")
    p.add_argument("--allow-private", action="store_true", help="Allow private/loopback hosts (local testing only)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_BAD_ARGS if e.code else EXIT_OK

    problems = []
    if not 1 <= args.max_pages <= HARD_MAX_PAGES:
        problems.append(f"--max-pages must be 1-{HARD_MAX_PAGES}")
    if not 0 <= args.max_depth <= HARD_MAX_DEPTH:
        problems.append(f"--max-depth must be 0-{HARD_MAX_DEPTH}")
    if not 1 <= args.timeout <= 30:
        problems.append("--timeout must be 1-30")
    if not 0 <= args.delay <= 5:
        problems.append("--delay must be 0-5")
    if not 1 <= args.concurrency <= 5:
        problems.append("--concurrency must be 1-5")
    if problems:
        log("error", message="; ".join(problems))
        return EXIT_BAD_ARGS

    log("start", tool=TOOL_NAME, version=TOOL_VERSION, url=args.url,
        max_pages=args.max_pages, max_depth=args.max_depth)
    try:
        result, code = Crawler(args).run()
    except Exception as e:  # last-resort guard
        log("error", message=f"internal error: {type(e).__name__}: {e}")
        return EXIT_INTERNAL

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        log("output", path=args.output, bytes=len(text.encode("utf-8")))
    else:
        sys.stdout.write(text + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
