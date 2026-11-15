"""Normalise Crawl4AI CLI output into the same result schema `sitecrawl` produces.

We run:   crwl <url> -o all -O <file> [--deep-crawl bfs --max-pages N] --bypass-cache
`-o all` dumps the full CrawlResult (url, success, status_code, metadata, markdown,
links, error_message...). We use it instead of `-o json` because in Crawl4AI `-o json`
prints `extracted_content`, which is only populated when an extraction schema is given.

Crawl4AI's JSON shape can vary between versions (markdown may be a string or an
object, deep crawls return a list), so everything here is defensive: unknown shapes
raise AdapterError and the orchestrator falls back to sitecrawl honestly.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
PHONE_RE = re.compile(r"(?<![\w.])\+?\(?\d[\d\s().-]{6,18}\d(?![\w.])")
MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
SOCIAL = ("facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "github.com", "tiktok.com")


class AdapterError(ValueError):
    pass


def load_output(file_text: str | None, stdout: str) -> list[dict]:
    """Find the CrawlResult JSON in the output file, or failing that in stdout."""
    for text in (file_text, stdout):
        if not text:
            continue
        data = _first_json(text)
        if data is None:
            continue
        if isinstance(data, dict) and "results" in data and isinstance(data["results"], list):
            data = data["results"]
        items = data if isinstance(data, list) else [data]
        items = [i for i in items if isinstance(i, dict) and ("url" in i or "markdown" in i)]
        if items:
            return items
    raise AdapterError("no CrawlResult JSON found in Crawl4AI output")


def _first_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener in ("[", "{"):
        i = text.find(opener)
        while i != -1:
            try:
                return json.JSONDecoder().raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                i = text.find(opener, i + 1)
    return None


def _markdown_text(md) -> str:
    if isinstance(md, str):
        return md
    if isinstance(md, dict):
        return md.get("fit_markdown") or md.get("raw_markdown") or md.get("markdown_with_citations") or ""
    return ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", MD_LINK_RE.sub(r"\1", s or "")).strip()


def to_page(item: dict, depth: int) -> dict:
    md = _markdown_text(item.get("markdown"))
    meta = item.get("metadata") or {}
    lines = md.splitlines()
    headings = {"h1": [], "h2": [], "h3": []}
    for ln in lines:
        m = re.match(r"^(#{1,3})\s+(.*)", ln.strip())
        if m:
            h = _clean(m.group(2))
            if h and h not in headings[f"h{len(m.group(1))}"]:
                headings[f"h{len(m.group(1))}"].append(h)
    paragraphs = []
    for block in re.split(r"\n\s*\n", md):
        b = block.strip()
        if not b or b.startswith("#") or b.startswith("|") or b.startswith("```"):
            continue
        t = _clean(re.sub(r"^[-*]\s+", "", b))
        if len(t) >= 40 and t not in paragraphs:
            paragraphs.append(t)
    links = item.get("links") or {}
    internal = links.get("internal") or []
    external = links.get("external") or []
    hrefs = [l.get("href", "") if isinstance(l, dict) else str(l) for l in internal + external]
    social = sorted({h for h in hrefs if any((urlparse(h).hostname or "").endswith(d) for d in SOCIAL)})[:15]
    text = _clean(md)
    emails = sorted(set(EMAIL_RE.findall(text)))[:20]
    phones = sorted({p.strip() for p in PHONE_RE.findall(text) if 8 <= len(re.sub(r"\D", "", p)) <= 15})[:10]
    title = meta.get("title") or meta.get("og:title") or (headings["h1"][0] if headings["h1"] else None)
    return {
        "url": item.get("url"),
        "final_url": item.get("redirected_url") or item.get("url"),
        "status_code": item.get("status_code"),
        "depth": (item.get("metadata") or {}).get("depth", depth),
        "title": title,
        "meta_description": meta.get("description") or meta.get("og:description"),
        "language": meta.get("language") or meta.get("lang"),
        "canonical": None,
        "open_graph": {k.split(":", 1)[1]: v for k, v in meta.items() if isinstance(k, str) and k.startswith("og:") and isinstance(v, str)},
        "headings": {k: v[:15] for k, v in headings.items()},
        "key_paragraphs": paragraphs[:8],
        "main_text": " ".join(paragraphs)[:4000],
        "word_count": len(text.split()),
        "links": {"internal_count": len(internal), "external_count": len(external),
                  "sample_external": [h for h in hrefs[len(internal):]][:10]},
        "images_count": len(((item.get("media") or {}).get("images")) or []),
        "contacts": {"emails": emails, "phones": phones},
        "social_links": social,
        "fetch_ms": None,
    }


def normalise(items: list[dict], start_url: str, duration_ms: int, limits: dict) -> dict:
    pages, errors = [], []
    for i, it in enumerate(items):
        ok = it.get("success", True) and (it.get("status_code") or 200) < 400
        if ok:
            pages.append(to_page(it, 0 if i == 0 else 1))
        else:
            errors.append({"url": it.get("url"), "error": it.get("error_message") or f"HTTP {it.get('status_code')}"})
    first = pages[0] if pages else {}
    status = "unreachable" if not pages else ("partial" if errors else "success")
    return {
        "tool": "crawl4ai",
        "tool_version": "crwl",
        "status": status,
        "error": errors[0]["error"] if not pages and errors else None,
        "start_url": start_url,
        "started_at": None,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_ms": duration_ms,
        "limits": limits,
        "stats": {"pages_crawled": len(pages), "pages_failed": len(errors), "total_words": sum(p["word_count"] for p in pages)},
        "site": {
            "name": (first.get("open_graph") or {}).get("site_name") or first.get("title"),
            "title": first.get("title"),
            "description": first.get("meta_description"),
            "language": first.get("language"),
            "emails": sorted({e for p in pages for e in p["contacts"]["emails"]})[:20],
            "phones": sorted({x for p in pages for x in p["contacts"]["phones"]})[:10],
            "social_links": sorted({s for p in pages for s in p["social_links"]})[:20],
        },
        "pages": pages,
        "errors": errors,
    }
