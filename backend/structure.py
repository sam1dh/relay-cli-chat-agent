"""Deterministic structuring of crawler output (no LLM involved).

Turns per-page "items" like  "Tipping the Velvet — £53.74 · In stock"  into records
{name, price, price_value, details, page} and computes price statistics.
"""
from __future__ import annotations

import re

PRICE_RE = re.compile(r"(?:[£$\u20ac\u20b9]\s?\d[\d,]*(?:\.\d{1,2})?|\b\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP|INR|Rs\.?)\b)")
CURRENCY_RE = re.compile(r"[£$\u20ac\u20b9]|USD|EUR|GBP|INR|Rs")


def _value(price):
    if not price:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", price.replace(",", "")))
    except ValueError:
        return None


def extract_items(pages, limit=100):
    out, seen = [], set()
    for p in pages:
        for it in p.get("items") or []:
            name, _, rest = it.partition(" \u2014 ")
            name = name.strip()
            parts = [x.strip() for x in rest.split(" \u00b7 ") if x.strip()]
            price = next((x for x in parts if PRICE_RE.fullmatch(x) or (PRICE_RE.search(x) and len(x) <= 20)), None)
            details = [x for x in parts if x != price]
            key = (name.lower(), price)
            if not name or key in seen:
                continue
            seen.add(key)
            out.append({
                "name": name,
                "price": PRICE_RE.search(price).group(0) if price else None,
                "price_value": _value(PRICE_RE.search(price).group(0)) if price else None,
                "details": " \u00b7 ".join(details) or None,
                "page": p.get("final_url") or p.get("url"),
            })
            if len(out) >= limit:
                return out
    return out


def price_stats(items):
    priced = [i for i in items if i.get("price_value") is not None]
    if not priced:
        return None
    cur = CURRENCY_RE.search(priced[0]["price"] or "")
    cheapest = min(priced, key=lambda i: i["price_value"])
    dearest = max(priced, key=lambda i: i["price_value"])
    avg = sum(i["price_value"] for i in priced) / len(priced)
    return {
        "currency": cur.group(0) if cur else None,
        "items_with_price": len(priced),
        "min": cheapest["price_value"], "max": dearest["price_value"], "average": round(avg, 2),
        "cheapest": {"name": cheapest["name"], "price": cheapest["price"]},
        "most_expensive": {"name": dearest["name"], "price": dearest["price"]},
    }
