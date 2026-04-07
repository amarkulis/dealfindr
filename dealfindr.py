#!/usr/bin/env python3
"""
DealFindr — Find the best prices across multiple shopping platforms.

Searches eBay, Craigslist, Amazon, Walmart, Best Buy, Target, Newegg,
Mercari, Swappa, AliExpress, and Google Shopping locally without any API
keys. Results are sorted cheapest → most expensive.
"""

import argparse
import csv
import io
import json
from rapidfuzz import fuzz as _fuzz
import logging
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote_plus, unquote

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

console = Console()
log = logging.getLogger("dealfindr")

__version__ = "1.1.0"

# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def _headers(referer: str = "") -> dict:
    h = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }
    if referer:
        h["Referer"] = referer
    return h


_thread_local = threading.local()


def _get_session() -> requests.Session:
    """Return a per-thread requests.Session for thread-safe HTTP."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


def _get(url: str, timeout: int = 14, retries: int = 2, **kwargs) -> Optional[requests.Response]:
    """GET with random UA, short retry loop, and graceful failure."""
    headers = kwargs.pop("headers", None)
    # Use separate connect/read timeouts to avoid long hangs on blocked hosts.
    req_timeout = kwargs.pop("timeout", (6, timeout))

    for attempt in range(max(1, retries + 1)):
        try:
            resp = _get_session().get(
                url,
                headers=headers or _headers(),
                timeout=req_timeout,
                **kwargs,
            )
            if resp.status_code == 200:
                return resp
            log.debug("GET %s returned status %d", url, resp.status_code)
        except Exception as exc:
            log.debug("GET %s attempt %d failed: %s", url, attempt + 1, exc)
            time.sleep(0.2)
    log.debug("GET %s gave up after %d attempts", url, retries + 1)
    return None


def _parse_price(text: str) -> Optional[float]:
    """Extract the first dollar-amount from a string."""
    if not text:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", text.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Weight / unit conversion
# ──────────────────────────────────────────────────────────────────────────────

_WEIGHT_TO_OZ = {
    "lb": 16.0, "lbs": 16.0, "pound": 16.0, "pounds": 16.0,
    "oz": 1.0, "ounce": 1.0, "ounces": 1.0,
    "kg": 35.274, "g": 0.03527, "gram": 0.03527, "grams": 0.03527,
}

_WEIGHT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(lb|lbs|pound|pounds|oz|ounce|ounces|kg|g|gram|grams)\b",
    re.IGNORECASE,
)


def _fmt_num(v: float) -> str:
    """Format a number: drop decimals if whole, otherwise up to 2 decimal places."""
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _extract_size(title: str) -> Optional[str]:
    """Extract weight/size string from a product title and show conversion."""
    m = _WEIGHT_RE.search(title)
    if m:
        val = float(m.group(1))
        unit = m.group(2).lower()
        short = {
            "pound": "lb", "pounds": "lb", "lbs": "lb",
            "ounce": "oz", "ounces": "oz",
            "gram": "g", "grams": "g",
        }.get(unit, unit)
        oz_val = val * _WEIGHT_TO_OZ.get(unit, 1.0)
        if short == "oz" and oz_val >= 4:
            lb_val = oz_val / 16
            return f"{_fmt_num(val)} oz ({_fmt_num(lb_val)} lb)"
        if short == "lb":
            return f"{_fmt_num(val)} lb ({_fmt_num(val * 16)} oz)"
        if short == "kg":
            return f"{_fmt_num(val)} kg ({_fmt_num(oz_val)} oz)"
        return f"{_fmt_num(val)} {short}"
    m2 = re.search(r"(\d+)\s*(?:pack|count|ct)\b", title, re.IGNORECASE)
    if m2:
        return f"{m2.group(1)} ct"
    return None


def _title_to_oz(title: str) -> Optional[float]:
    """Extract weight from *title* and normalise to ounces."""
    m = _WEIGHT_RE.search(title)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    return val * _WEIGHT_TO_OZ.get(unit, 1.0)


def _generate_alt_queries(query: str) -> List[str]:
    """Generate alternate search queries with unit-converted weights.

    e.g. '2lb shelled pistachios' → ['32oz shelled pistachios']
    """
    m = _WEIGHT_RE.search(query)
    if not m:
        return []
    val = float(m.group(1))
    unit = m.group(2).lower()
    original = m.group(0)
    alts = []
    if unit in ("lb", "lbs", "pound", "pounds"):
        alts.append(query.replace(original, f"{_fmt_num(val * 16)}oz", 1))
    elif unit in ("oz", "ounce", "ounces"):
        lb_val = val / 16
        if lb_val >= 0.25:
            alts.append(query.replace(original, f"{_fmt_num(lb_val)}lb", 1))
    elif unit == "kg":
        alts.append(query.replace(original, f"{_fmt_num(val * 2.205)}lb", 1))
    elif unit in ("g", "gram", "grams"):
        oz_val = val * 0.03527
        if oz_val >= 1:
            alts.append(query.replace(original, f"{_fmt_num(oz_val)}oz", 1))
    return alts


# ──────────────────────────────────────────────────────────────────────────────
# JSON utilities (shared by Walmart, Target, Mercari, etc.)
# ──────────────────────────────────────────────────────────────────────────────

def _walk_json_products(data, depth=0):
    """Walk JSON tree and find dicts that look like product entries."""
    if depth > 12:
        return []
    found = []
    if isinstance(data, dict):
        has_name = "name" in data or "title" in data
        has_price = any(
            k in data
            for k in ("price", "currentPrice", "salePrice", "priceInfo", "current_retail")
        )
        if has_name and has_price:
            found.append(data)
        for v in data.values():
            found.extend(_walk_json_products(v, depth + 1))
    elif isinstance(data, list):
        for item in data:
            found.extend(_walk_json_products(item, depth + 1))
    return found


def _extract_json_price(item, keys=("price", "currentPrice", "salePrice")):
    """Extract a numeric price from a JSON product dict."""
    for key in keys:
        val = item.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
        if isinstance(val, str):
            p = _parse_price(val)
            if p and p > 0:
                return p
        if isinstance(val, dict):
            for sk in ("price", "min", "current", "value"):
                sv = val.get(sk)
                if isinstance(sv, (int, float)) and sv > 0:
                    return float(sv)
    pi = item.get("priceInfo", {})
    if isinstance(pi, dict):
        for sk in ("currentPrice", "price"):
            sv = pi.get(sk)
            if isinstance(sv, (int, float)) and sv > 0:
                return float(sv)
    return None


_STOP_TOKENS = {
    "a", "an", "the", "for", "with", "to", "from", "and", "or", "on", "in", "of",
    "pack", "new", "used", "sale", "inch", "ft",
}

_INTENT_TERMS = {
    "cable": {"cable", "cord", "wire"},
    "adapter": {"adapter", "adaptor", "converter", "dongle"},
    "display": {"display", "monitor", "screen"},
    "dock": {"dock", "docking"},
    "hub": {"hub"},
    "charger": {"charger", "charging"},
}

_NOISE_CATEGORIES = {"display", "dock", "hub", "charger"}


def _query_tokens(query: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9]+", query.lower())
    return [t for t in tokens if (len(t) >= 2 or t.isdigit()) and t not in _STOP_TOKENS]


def _intent_categories(text: str) -> set:
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    categories = set()
    for category, words in _INTENT_TERMS.items():
        if tokens & words:
            categories.add(category)
    return categories


def _passes_intent_filters(title: str, query: str) -> bool:
    query_categories = _intent_categories(query)
    title_categories = _intent_categories(title)

    if not query_categories:
        return True

    if "cable" in query_categories and "cable" not in title_categories:
        return False

    # When the title satisfies the primary query intent (e.g., both are
    # "cable"), noise categories like "display" are harmless — a
    # "DisplayPort cable" is still a cable.  Only reject noise when the
    # title does NOT share the primary intent.
    primary_match = bool(query_categories & title_categories)

    if not primary_match:
        for category in _NOISE_CATEGORIES:
            if category in title_categories and category not in query_categories:
                return False

    if "adapter" in title_categories and "adapter" not in query_categories and "cable" in query_categories:
        return False

    return True


_UNIT_WORDS = {
    "lb", "lbs", "pound", "pounds", "oz", "ounce", "ounces",
    "kg", "gram", "grams", "ct", "count", "pk", "mm", "cm",
    "qt", "gal", "gallon", "ml", "liter",
}

# Regex for a number glued to a unit suffix — e.g. "2lb", "24oz", "0.5m", "16gb".
# These are quantity/size modifiers, NOT model numbers.
_QTY_UNIT_RE = re.compile(
    r"^\d+(?:\.\d+)?"               # leading digits (possibly decimal)
    r"(?:lb|lbs|oz|kg|g|mm|cm|m|ct|pk|pt|qt|gal|ml|gb|tb|ft|in)$",
    re.IGNORECASE,
)


def _is_modifier_token(token: str) -> bool:
    """True if *token* is a quantity+unit like '2lb' or '24oz'.

    Standalone numbers ('15', '2', '3') are treated as **core** tokens because
    they are usually model/version numbers (iPhone 15, Thunderbolt 2, PS5).
    """
    if _QTY_UNIT_RE.match(token):
        return True                        # "2lb", "24oz", "16gb"
    return token in _UNIT_WORDS


def _fuzzy_token_in_title(token: str, title_words: List[str]) -> bool:
    """Check if *token* fuzzy-matches any word in the title (handles typos)."""
    if len(token) < 4:
        return False
    for word in title_words:
        if len(word) < 3:
            continue
        if _fuzz.ratio(token, word) >= 75:
            return True
    return False


def _token_hits_title(token: str, title_l: str, title_words: List[str]) -> bool:
    """Check whether *token* appears in *title_l* (exact substring or fuzzy)."""
    if token.isdigit() and len(token) <= 2:
        # Short numbers must match as whole words — "2" must not match "D2" or "ADR6225".
        return token in title_words
    if token in title_l:
        return True
    return _fuzzy_token_in_title(token, title_words)


# Regex to find "word number" version pairs like "thunderbolt 2", "iphone 15", "usb 3.0".
_VERSION_PHRASE_RE = re.compile(
    r'\b([a-z]{2,})\s+(\d+(?:\.\d+)?)\b'
)


def _version_phrases(text: str) -> List[Tuple[str, str]]:
    """Extract product-version pairs like ('thunderbolt', '2'), ('iphone', '15')."""
    pairs = []
    for m in _VERSION_PHRASE_RE.finditer(text.lower()):
        word, num = m.group(1), m.group(2)
        if word not in _STOP_TOKENS:
            pairs.append((word, num))
    return pairs


def _is_relevant_title(title: str, query: str) -> bool:
    """Relevance gate — filters obvious mismatches.

    Tokens are classified as **core** (product descriptors like 'shelled',
    'pistachios') or **modifier** (quantities/units like '2lb').  All core
    tokens must match (with fuzzy typo tolerance).  Modifier tokens are
    optional — they improve ranking but don't disqualify a result.
    """
    if not title:
        return False

    title_l = title.lower()
    query_l = query.lower().strip()
    if query_l and query_l in title_l:
        return True

    if not _passes_intent_filters(title, query):
        return False

    if _has_contradiction(title, query):
        return False

    # Version phrases (e.g. "thunderbolt 2", "iphone 15") must appear as
    # adjacent pairs in the title, not just as scattered individual tokens.
    for word, num in _version_phrases(query):
        pattern = rf'\b{re.escape(word)}\W*{re.escape(num)}\b'
        if not re.search(pattern, title_l):
            return False

    tokens = _query_tokens(query)
    if not tokens:
        return True

    core   = [t for t in tokens if not _is_modifier_token(t)]
    modifiers = [t for t in tokens if _is_modifier_token(t)]
    title_words = re.findall(r"[a-z0-9]+", title_l)

    # All core tokens must match (product name / key descriptors).
    core_hits = sum(1 for t in core if _token_hits_title(t, title_l, title_words))
    if core:
        # For 1-3 core tokens require all.  For 4+ allow one miss.
        needed = len(core) if len(core) <= 3 else len(core) - 1
        if core_hits < needed:
            return False

    # If there are ONLY modifiers and no core tokens, require at least one.
    if not core:
        mod_hits = sum(1 for t in modifiers if _token_hits_title(t, title_l, title_words))
        return mod_hits >= 1

    return True


# Pairs where the query term and the title term are contradictory.
# Each tuple is (query_pattern, title_pattern) — if the query matches the
# first regex and the title matches the second, the result is rejected.
_CONTRADICTIONS = [
    # "shelled" (without "in-shell") vs "in-shell" / "in shell" / "inshell"
    (r"\bshelled\b(?!.*\bin[- ]?shell)", r"\bin[- ]?shell"),
    # "in-shell" vs bare "shelled" (without "in-shell")
    (r"\bin[- ]?shell\b", r"(?<!\bin)\bshelled\b"),
    # "unsalted" vs "salted" (without "unsalted")
    (r"\bunsalted\b", r"(?<!un)\bsalted\b(?!.*\bunsalted\b)"),
    # "salted" (without "unsalted") vs "unsalted"
    (r"(?<!un)\bsalted\b(?!.*\bunsalted\b)", r"\bunsalted\b"),
    # "raw" vs "roasted"
    (r"\braw\b", r"\broasted\b"),
    (r"\broasted\b", r"\braw\b"),
    # "wireless" vs "wired"
    (r"\bwireless\b", r"(?<!wire)\bwired\b"),
    (r"(?<!wire)\bwired\b", r"\bwireless\b"),
    # "new" vs "refurbished"/"renewed"
    (r"\bnew\b", r"\b(?:refurbished|renewed)\b"),
    (r"\b(?:refurbished|renewed)\b", r"\bnew\b"),
]


def _has_contradiction(title: str, query: str) -> bool:
    """Return True if the title contradicts a specific term in the query."""
    q = query.lower()
    t = title.lower()
    for q_pat, t_pat in _CONTRADICTIONS:
        if re.search(q_pat, q) and re.search(t_pat, t):
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Deal:
    title: str
    price: float
    url: str
    source: str
    condition: str = "Unknown"
    shipping: Optional[float] = None
    location: Optional[str] = None
    size: Optional[str] = None
    unit_oz: Optional[float] = None

    @property
    def total_price(self) -> float:
        return self.price + (self.shipping or 0.0)

    @property
    def unit_price(self) -> Optional[float]:
        """Price per ounce (total including shipping)."""
        if self.unit_oz and self.unit_oz > 0:
            return self.total_price / self.unit_oz
        return None


# ──────────────────────────────────────────────────────────────────────────────
# eBay scraper
# ──────────────────────────────────────────────────────────────────────────────

def search_ebay(query: str, max_results: int = 20) -> List[Deal]:
    """Buy-It-Now listings sorted by price + shipping (lowest first).

    Uses Playwright headless browser to bypass eBay bot detection.
    Falls back to requests if Playwright is unavailable.
    """
    deals: List[Deal] = []
    url = (
        f"https://www.ebay.com/sch/i.html"
        f"?_nkw={quote_plus(query)}&_sop=15&LH_BIN=1&_ipg=50"
    )

    html: Optional[str] = None

    # ── Playwright path (preferred) ───────────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright  # noqa: E402

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="en-US",
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
            )
            page = ctx.new_page()
            page.goto("https://www.ebay.com/", wait_until="domcontentloaded", timeout=12000)
            time.sleep(1)
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_selector("div.s-card, li.s-item", timeout=8000)
            time.sleep(1)
            html = page.content()
            browser.close()
    except Exception as exc:
        log.debug("Playwright eBay path failed: %s", exc)

    # ── requests fallback ─────────────────────────────────────────────────────
    if html is None:
        hdrs = _headers("https://www.ebay.com/")
        hdrs.update({
            "sec-ch-ua": '"Chromium";v="123", "Google Chrome";v="123"',
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
        })
        resp = _get(url, headers=hdrs)
        if resp:
            html = resp.text

    if not html:
        return deals

    soup = BeautifulSoup(html, "lxml")

    # eBay uses both old (li.s-item) and new (div.s-card) layouts.
    card_items = soup.select("div.s-card") or soup.select("li[data-view]") or soup.select(".s-item")

    for item in card_items:
        if len(deals) >= max_results:
            break
        try:
            title_el = item.select_one(
                ".s-card__title, .su-styled-text.primary.default, .s-item__title"
            )
            price_el = item.select_one(
                ".s-card__price, .su-styled-text.s-card__price, .s-item__price"
            )
            link_el = item.select_one("a.s-card__link[href], a.s-item__link[href]")
            ship_el = item.select_one(
                ".su-styled-text.secondary.large, .s-item__shipping, .s-item__freeXDays"
            )
            cond_el = item.select_one(
                ".su-styled-text.secondary.default, .SECONDARY_INFO"
            )

            if not (title_el and price_el and link_el):
                continue

            title = title_el.get_text(strip=True)
            if title in ("Shop on eBay", ""):
                continue
            title = re.sub(r"\s*Opens in a new window or tab\s*", "", title, flags=re.IGNORECASE).strip()
            title = re.sub(r"\s+", " ", title)
            if not _is_relevant_title(title, query):
                continue

            # Price ranges → take lower bound
            raw_price = price_el.get_text(strip=True)
            if " to " in raw_price:
                raw_price = raw_price.split(" to ")[0]

            price = _parse_price(raw_price)
            if not price or price <= 0:
                continue

            # Strip eBay tracking from URL
            link = link_el.get("href", "")
            if link.startswith("//"):
                link = f"https:{link}"
            m = re.search(r"https?://(?:www\.)?ebay\.com/itm/(\d+)", link)
            if m:
                link = f"https://www.ebay.com/itm/{m.group(1)}"
            else:
                link = link.split("?")[0].rstrip("/")

            shipping: Optional[float] = None
            if ship_el:
                st = ship_el.get_text(strip=True).lower()
                if "free" in st:
                    shipping = 0.0
                else:
                    sv = _parse_price(st)
                    if sv is not None:
                        shipping = sv

            condition = "Used"
            if cond_el:
                ct = cond_el.get_text(strip=True).lower()
                if "new" in ct:
                    condition = "New"
                elif "refurbish" in ct or "renew" in ct or "certif" in ct:
                    condition = "Refurbished"

            deals.append(Deal(title[:90], price, link, "eBay", condition, shipping))
        except Exception:
            continue

    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Craigslist scraper  (RSS — no bot detection)
# ──────────────────────────────────────────────────────────────────────────────

_CL_CITIES = [
    "losangeles", "sfbay", "newyork", "chicago",
    "seattle",    "miami",  "denver", "atlanta",
    "boston",     "dallas", "phoenix","houston",
]

_SOURCE_LABELS = {
    "ebay": "eBay",
    "craigslist": "Craigslist",
    "amazon": "Amazon",
    "walmart": "Walmart",
    "bestbuy": "Best Buy",
    "target": "Target",
    "newegg": "Newegg",
    "mercari": "Mercari",
    "swappa": "Swappa",
    "aliexpress": "AliExpress",
    "google": "Google Shopping",
}


def _parse_source_selection(value: str) -> List[str]:
    raw = value.strip().lower()
    if not raw or raw == "all":
        return list(_SOURCE_LABELS.keys())

    options = list(_SOURCE_LABELS.keys())
    selected: List[str] = []
    for part in re.split(r"[\s,]+", raw):
        if not part:
            continue
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(options):
                selected.append(options[idx])
        elif part in _SOURCE_LABELS:
            selected.append(part)

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(selected))


def _interactive_setup(args: argparse.Namespace) -> Tuple[str, argparse.Namespace]:
    console.print()
    console.print(Panel("[bold cyan]Interactive DealFindr Setup[/bold cyan]", border_style="cyan"))

    query = " ".join(args.query).strip() if args.query else ""
    if not query:
        query = Prompt.ask("What do you want to search for").strip()

    console.print("[bold cyan]Available sources:[/bold cyan]")
    for idx, (key, label) in enumerate(_SOURCE_LABELS.items(), 1):
        console.print(f"  [dim]{idx}.[/dim] {label} [dim]({key})[/dim]")

    source_answer = Prompt.ask(
        "Choose sources by number or name, comma-separated",
        default="all",
    )
    args.source = _parse_source_selection(source_answer)

    if not args.source:
        args.source = list(_SOURCE_LABELS.keys())

    if "craigslist" in args.source and not args.cities:
        city_answer = Prompt.ask(
            "Craigslist cities (comma-separated)",
            default="chicago",
        )
        args.cities = [part.strip().lower() for part in city_answer.split(",") if part.strip()]

    max_results_answer = Prompt.ask("Max results per source", default=str(args.max_results))
    try:
        args.max_results = max(1, int(max_results_answer))
    except ValueError:
        args.max_results = 20

    return query, args


def search_craigslist(
    query: str,
    cities: Optional[List[str]] = None,
    max_results: int = 20,
) -> List[Deal]:
    """Craigslist for-sale search via HTML + JSON-LD listing data."""
    cities = cities or _CL_CITIES
    deals: List[Deal] = []

    for city in cities[:8]:
        if len(deals) >= max_results:
            break
        try:
            url = (
                f"https://{city}.craigslist.org/search/sss"
                f"?query={quote_plus(query)}&sort=date"
            )
            resp = _get(url, timeout=10)
            if not resp:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Primary source: JSON-LD block with structured search results.
            ld_script = soup.find("script", id="ld_searchpage_results")
            if not ld_script or not ld_script.string:
                continue

            data = json.loads(ld_script.string)
            items = data.get("itemListElement", [])

            # Build a searchable list of visible listing links with their displayed titles.
            listing_nodes = soup.select("li.cl-static-search-result a[href*='/d/']")
            listing_candidates = []
            for node in listing_nodes:
                href = node.get("href", "")
                shown_title = ""
                title_div = node.select_one(".title")
                if title_div:
                    shown_title = title_div.get_text(" ", strip=True)
                if not shown_title:
                    shown_title = node.get_text(" ", strip=True)
                if href and shown_title:
                    listing_candidates.append((shown_title, href))

            used_links: set = set()
            for item in items[:10]:
                try:
                    product = item.get("item") or {}
                    title = str(product.get("name") or "").strip()
                    if not title:
                        continue
                    if not _is_relevant_title(title, query):
                        continue

                    offers = product.get("offers") or {}
                    price = _parse_price(str(offers.get("price") or ""))
                    if not price or price <= 0:
                        continue

                    link = ""

                    # Match listing by title-token overlap (more robust than positional index).
                    title_tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
                    best_score = 0
                    best_link = ""
                    for shown_title, href in listing_candidates:
                        if href in used_links:
                            continue
                        shown_tokens = set(re.findall(r"[a-z0-9]+", shown_title.lower()))
                        if not shown_tokens:
                            continue
                        overlap = len(title_tokens & shown_tokens)
                        if overlap > best_score:
                            best_score = overlap
                            best_link = href

                    if best_score >= 2:
                        link = best_link
                        used_links.add(best_link)

                    if not link:
                        continue

                    deals.append(
                        Deal(title[:90], price, link, "Craigslist",
                             "Used", None, city.title())
                    )
                except Exception:
                    continue

            time.sleep(0.2)
        except Exception:
            continue

    return deals[:max_results]


# ──────────────────────────────────────────────────────────────────────────────
# Amazon scraper
# ──────────────────────────────────────────────────────────────────────────────

def _parse_amazon_results(
    html: str, query: str, default_condition: str, max_results: int,
) -> List[Deal]:
    """Parse Amazon search-result HTML into Deal objects."""
    deals: List[Deal] = []
    soup = BeautifulSoup(html, "html.parser")
    for result in soup.select('[data-component-type="s-search-result"]'):
        if len(deals) >= max_results:
            break
        try:
            title_el  = result.select_one("h2 span, h2 .a-text-normal")
            link_el   = result.select_one("a.a-link-normal.s-no-outline, h2 a")
            whole     = result.select_one(".a-price-whole")
            frac      = result.select_one(".a-price-fraction")

            if not (title_el and whole and link_el):
                continue

            title = title_el.get_text(strip=True)
            if not _is_relevant_title(title, query):
                continue
            price_str = whole.get_text(strip=True).replace(",", "")
            if frac:
                price_str += "." + frac.get_text(strip=True)

            price = _parse_price(price_str)
            if not price or price <= 0:
                continue

            href = link_el.get("href", "") if link_el else ""
            link = f"https://www.amazon.com{href}" if href.startswith("/") else href
            # Keep only the ASIN path, strip tracking
            link = re.sub(r"/ref=.*", "", link)

            shipping: Optional[float] = None
            free_ship = result.select_one('[aria-label*="FREE delivery"], .s-free-delivery-text')
            if free_ship:
                shipping = 0.0

            # Detect condition from title keywords
            title_lower = title.lower()
            if "renewed" in title_lower or "refurbished" in title_lower:
                condition = "Renewed"
            elif default_condition == "Used" and "renewed" not in title_lower:
                condition = "Used"
            else:
                condition = default_condition

            deals.append(Deal(title[:90], price, link, "Amazon", condition, shipping))
        except Exception:
            continue
    return deals


def search_amazon(query: str, max_results: int = 20) -> List[Deal]:
    """Amazon search — new items plus used/renewed offers."""
    deals: List[Deal] = []
    seen_urls: set = set()
    hdrs = _headers("https://www.amazon.com/")

    for condition_filter, default_cond in (("", "New"), ("&condition=used", "Used")):
        url = f"https://www.amazon.com/s?k={quote_plus(query)}{condition_filter}"
        resp = _get(url, headers=hdrs, timeout=16)
        if not resp:
            continue
        lower_text = resp.text.lower()
        if "captcha" in lower_text or "enter the characters you see below" in lower_text:
            continue
        batch = _parse_amazon_results(resp.text, query, default_cond, max_results)
        for deal in batch:
            if deal.url not in seen_urls:
                seen_urls.add(deal.url)
                deals.append(deal)

    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Walmart scraper  (Next.js __NEXT_DATA__ JSON payload)
# ──────────────────────────────────────────────────────────────────────────────

def search_walmart(query: str, max_results: int = 20) -> List[Deal]:
    """Walmart search via the embedded Next.js data blob."""
    deals: List[Deal] = []
    url = f"https://www.walmart.com/search?q={quote_plus(query)}&sort=price_low"
    resp = _get(url, headers=_headers("https://www.walmart.com/"), timeout=16)
    if not resp:
        return deals

    soup = BeautifulSoup(resp.text, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return deals

    try:
        data = json.loads(script.string)
    except Exception:
        return deals

    # Navigate: props → pageProps → initialData → searchResult → itemStacks → items
    try:
        stacks = (
            data["props"]["pageProps"]["initialData"]
                ["searchResult"]["itemStacks"]
        )
        raw_items = []
        for stack in stacks:
            raw_items.extend(stack.get("items", []))
    except (KeyError, TypeError):
        raw_items = []

    # Fallback: generic tree walk
    if not raw_items:
        def _walk(obj, depth=0):
            found = []
            if depth > 12:
                return found
            if isinstance(obj, list):
                for el in obj:
                    found.extend(_walk(el, depth + 1))
            elif isinstance(obj, dict):
                if ("name" in obj or "title" in obj) and (
                    "price" in obj or "priceInfo" in obj
                ):
                    found.append(obj)
                for v in obj.values():
                    found.extend(_walk(v, depth + 1))
            return found

        raw_items = _walk(data)

    seen: set = set()
    for item in raw_items:
        if len(deals) >= max_results:
            break
        try:
            title = str(item.get("name") or item.get("title") or "").strip()
            if not title or title in seen:
                continue
            if not _is_relevant_title(title, query):
                continue
            seen.add(title)

            # Price extraction — try common keys
            price: Optional[float] = None
            for key in ("price", "currentPrice", "salePrice"):
                val = item.get(key)
                if isinstance(val, (int, float)):
                    price = float(val)
                    break
                if isinstance(val, str):
                    price = _parse_price(val)
                    if price:
                        break
                if isinstance(val, dict):
                    for sk in ("price", "min", "current"):
                        sv = val.get(sk)
                        if isinstance(sv, (int, float)):
                            price = float(sv)
                            break
                    if price:
                        break

            if price is None:
                pi = item.get("priceInfo", {})
                if isinstance(pi, dict):
                    for sk in ("currentPrice", "price"):
                        sv = pi.get(sk)
                        if isinstance(sv, (int, float)):
                            price = float(sv)
                            break

            if not price or price <= 0:
                continue

            canon = item.get("canonicalUrl") or item.get("url") or ""
            link = f"https://www.walmart.com{canon}" if canon.startswith("/") else canon

            deals.append(Deal(title[:90], price, link, "Walmart", "New"))
        except Exception:
            continue

    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Best Buy scraper
# ──────────────────────────────────────────────────────────────────────────────

def search_bestbuy(query: str, max_results: int = 20) -> List[Deal]:
    """Best Buy search sorted by price ascending."""
    deals: List[Deal] = []
    url = (
        f"https://www.bestbuy.com/site/searchpage.jsp"
        f"?st={quote_plus(query)}&sort=pricelow&_dyncharset=UTF-8"
    )
    resp = _get(url, headers=_headers("https://www.bestbuy.com/"), timeout=16)
    if not resp:
        return deals

    soup = BeautifulSoup(resp.text, "html.parser")

    for item in soup.select(".sku-item"):
        if len(deals) >= max_results:
            break
        try:
            title_el = item.select_one(".sku-title a, h4.sku-header a")
            price_el = item.select_one(
                "[data-testid='customer-price'] span:first-child, "
                ".priceView-customer-price span"
            )
            if not (title_el and price_el):
                continue

            title = title_el.get_text(strip=True)
            if not _is_relevant_title(title, query):
                continue
            price = _parse_price(price_el.get_text(strip=True))
            if not price or price <= 0:
                continue

            href = title_el.get("href", "")
            link = f"https://www.bestbuy.com{href}" if href.startswith("/") else href

            condition = "New"
            cond_el = item.select_one(".item-condition, .open-box")
            if cond_el and "open" in cond_el.get_text(strip=True).lower():
                condition = "Open Box"

            deals.append(Deal(title[:90], price, link, "Best Buy", condition))
        except Exception:
            continue

    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Google Shopping scraper
# ──────────────────────────────────────────────────────────────────────────────

def search_google_shopping(query: str, max_results: int = 20) -> List[Deal]:
    """Google Shopping results via HTML scraping."""
    deals: List[Deal] = []
    url = f"https://www.google.com/search?q={quote_plus(query)}&tbm=shop&hl=en&gl=us"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.google.com/",
    }
    resp = _get(url, headers=headers, timeout=16)
    if not resp:
        return deals

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try several selector patterns Google has used
    items: list = []
    for sel in (".sh-dgr__content", ".sh-dlr__list-result", "div[data-sh-d]", ".mnr-c"):
        items = soup.select(sel)
        if items:
            break

    for item in items:
        if len(deals) >= max_results:
            break
        try:
            title_el = item.select_one("h3, .tAxDx, .EI11Pd, .sh-np__product-title")
            price_el = item.select_one(".a8Pemb, .kHxwFf, .HRLxBb, .T14wmb")
            link_el  = item.select_one("a[href]")
            store_el = item.select_one(".aULzUe, .LbUacb, .shntl, .IuHnof")

            if not (title_el and price_el):
                continue

            title = title_el.get_text(strip=True)
            if not _is_relevant_title(title, query):
                continue
            price = _parse_price(price_el.get_text(strip=True))
            if not price or price <= 0:
                continue

            link = ""
            if link_el:
                href = link_el.get("href", "")
                if href.startswith("/url?"):
                    m = re.search(r"[?&]q=([^&]+)", href)
                    link = unquote(m.group(1)) if m else href
                elif href.startswith("http"):
                    link = href
                else:
                    link = f"https://www.google.com{href}"

            store = store_el.get_text(strip=True) if store_el else ""
            source = f"Google Shopping ({store})" if store else "Google Shopping"

            deals.append(Deal(title[:90], price, link, source, "New"))
        except Exception:
            continue

    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Target scraper
# ──────────────────────────────────────────────────────────────────────────────

def search_target(query: str, max_results: int = 20) -> List[Deal]:
    """Target via Redsky API — returns structured JSON, no scraping needed."""
    deals: List[Deal] = []
    api_url = "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
    params = {
        "key": "9f36aeafbe60771e321a7cc95a78140772ab3e96",
        "channel": "WEB",
        "count": str(max_results),
        "default_purchasability_filter": "true",
        "keyword": query,
        "offset": "0",
        "page": f"/s/{query}",
        "pricing_store_id": "926",
        "scheduled_delivery_store_id": "926",
        "store_ids": "926,328,1792,2788",
        "visitor_id": "018E0000000000000000000000",
        "zip": "60601",
    }
    hdrs = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json",
        "Origin": "https://www.target.com",
        "Referer": "https://www.target.com/",
    }
    resp = _get(api_url, headers=hdrs, params=params, timeout=14)
    if not resp:
        return deals
    try:
        data = resp.json()
    except Exception:
        return deals

    products = (
        data.get("data", {}).get("search", {}).get("products", [])
    )
    for p in products:
        if len(deals) >= max_results:
            break
        try:
            item = p.get("item", {})
            price_info = p.get("price", {})
            desc = item.get("product_description", {})
            title = (desc.get("title") or "").strip()
            # Unescape HTML entities (&#38; etc.)
            title = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), title)
            if not title or not _is_relevant_title(title, query):
                continue
            price = price_info.get("current_retail") or price_info.get(
                "current_retail_min"
            )
            if not price:
                fmt = str(price_info.get("formatted_current_price", ""))
                price = _parse_price(fmt)
            if not price or price <= 0:
                continue
            enrichment = item.get("enrichment", {})
            buy_url = enrichment.get("buy_url", "")
            link = buy_url or ""
            deals.append(Deal(title[:90], float(price), link, "Target", "New"))
        except Exception:
            continue
    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Newegg scraper
# ──────────────────────────────────────────────────────────────────────────────

def search_newegg(query: str, max_results: int = 20) -> List[Deal]:
    """Newegg search sorted by price ascending."""
    deals: List[Deal] = []
    url = f"https://www.newegg.com/p/pl?d={quote_plus(query)}&Order=PRICE"
    resp = _get(url, headers=_headers("https://www.newegg.com/"), timeout=16)
    if not resp:
        return deals
    soup = BeautifulSoup(resp.text, "html.parser")

    for item in soup.select(".item-cell, .item-container"):
        if len(deals) >= max_results:
            break
        try:
            title_el = item.select_one(".item-title, a.item-title")
            price_el = item.select_one(".price-current, li.price-current")
            if not (title_el and price_el):
                continue
            title = title_el.get_text(strip=True)
            if not _is_relevant_title(title, query):
                continue
            strong = price_el.select_one("strong")
            sup = price_el.select_one("sup")
            if strong:
                price_str = strong.get_text(strip=True).replace(",", "")
                if sup:
                    price_str += sup.get_text(strip=True)
                price = _parse_price(price_str)
            else:
                price = _parse_price(price_el.get_text(strip=True))
            if not price or price <= 0:
                continue
            href = title_el.get("href", "")
            link = href if href.startswith("http") else f"https://www.newegg.com{href}"
            shipping = None
            ship_el = item.select_one(".price-ship, .free-ship")
            if ship_el:
                st = ship_el.get_text(strip=True).lower()
                if "free" in st:
                    shipping = 0.0
                else:
                    sv = _parse_price(st)
                    if sv is not None:
                        shipping = sv
            deals.append(Deal(title[:90], price, link, "Newegg", "New", shipping))
        except Exception:
            continue
    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Mercari scraper  (used marketplace)
# ──────────────────────────────────────────────────────────────────────────────

def search_mercari(query: str, max_results: int = 20) -> List[Deal]:
    """Mercari search — Next.js JSON or HTML fallback."""
    deals: List[Deal] = []
    url = f"https://www.mercari.com/search/?keyword={quote_plus(query)}&sortBy=2"
    resp = _get(url, headers=_headers("https://www.mercari.com/"), timeout=16)
    if not resp:
        return deals
    soup = BeautifulSoup(resp.text, "html.parser")

    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            data = json.loads(script.string)
            items = _walk_json_products(data)
            for item in items:
                if len(deals) >= max_results:
                    break
                title = str(item.get("name") or item.get("title") or "").strip()
                if not title or not _is_relevant_title(title, query):
                    continue
                price = _extract_json_price(item)
                if not price:
                    continue
                item_id = item.get("id") or ""
                link = f"https://www.mercari.com/us/item/{item_id}/" if item_id else ""
                status = str(item.get("status") or "").lower()
                if "sold" in status:
                    continue
                deals.append(Deal(title[:90], price, link, "Mercari", "Used"))
        except Exception:
            pass

    if not deals:
        for card in soup.select("[data-testid='ItemCell'], [data-testid='SearchResults'] a"):
            if len(deals) >= max_results:
                break
            try:
                title_el = card.select_one("[data-testid='ItemName'], [role='heading']")
                price_el = card.select_one("[data-testid='ItemPrice'], .price")
                if not (title_el and price_el):
                    continue
                title = title_el.get_text(strip=True)
                if not _is_relevant_title(title, query):
                    continue
                price = _parse_price(price_el.get_text(strip=True))
                if not price or price <= 0:
                    continue
                href = card.get("href") or ""
                if not href:
                    link_el = card.select_one("a[href]")
                    href = link_el.get("href", "") if link_el else ""
                link = f"https://www.mercari.com{href}" if href.startswith("/") else href
                deals.append(Deal(title[:90], price, link, "Mercari", "Used"))
            except Exception:
                continue
    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Swappa scraper  (verified used electronics)
# ──────────────────────────────────────────────────────────────────────────────

def search_swappa(query: str, max_results: int = 20) -> List[Deal]:
    """Swappa search results."""
    deals: List[Deal] = []
    url = f"https://swappa.com/search?q={quote_plus(query)}"
    resp = _get(url, headers=_headers("https://swappa.com/"), timeout=16)
    if not resp:
        return deals
    soup = BeautifulSoup(resp.text, "html.parser")

    for item in soup.select(".search_result, .listing_row, .search-result-item"):
        if len(deals) >= max_results:
            break
        try:
            title_el = item.select_one("h3 a, .listing_title a, .item-title a")
            price_el = item.select_one(".price, .listing_price, .item-price")
            if not (title_el and price_el):
                continue
            title = title_el.get_text(strip=True)
            if not _is_relevant_title(title, query):
                continue
            price = _parse_price(price_el.get_text(strip=True))
            if not price or price <= 0:
                continue
            href = title_el.get("href", "")
            link = f"https://swappa.com{href}" if href.startswith("/") else href
            condition = "Used"
            cond_el = item.select_one(".condition, .listing_condition")
            if cond_el:
                ct = cond_el.get_text(strip=True).lower()
                if "new" in ct or "mint" in ct:
                    condition = "New"
            deals.append(Deal(title[:90], price, link, "Swappa", condition))
        except Exception:
            continue
    return deals


# ──────────────────────────────────────────────────────────────────────────────
# AliExpress scraper  (best-effort, rate-limited)
# ──────────────────────────────────────────────────────────────────────────────

def search_aliexpress(query: str, max_results: int = 20) -> List[Deal]:
    """AliExpress search sorted by price ascending."""
    deals: List[Deal] = []
    url = f"https://www.aliexpress.com/wholesale?SearchText={quote_plus(query)}&SortType=price_asc"
    resp = _get(url, headers=_headers("https://www.aliexpress.com/"), timeout=20)
    if not resp:
        return deals
    soup = BeautifulSoup(resp.text, "html.parser")

    for script in soup.find_all("script"):
        text = script.string or ""
        if "searchResult" not in text and "items" not in text:
            continue
        for m in re.finditer(r'\{[^{}]*"title"[^{}]*"price"[^{}]*\}', text):
            if len(deals) >= max_results:
                break
            try:
                item = json.loads(m.group())
                title = str(item.get("title") or "").strip()
                if not title or not _is_relevant_title(title, query):
                    continue
                price = _extract_json_price(item)
                if not price:
                    continue
                item_id = item.get("productId") or item.get("id") or ""
                link = f"https://www.aliexpress.com/item/{item_id}.html" if item_id else ""
                deals.append(Deal(title[:90], price, link, "AliExpress", "New"))
            except Exception:
                continue

    if not deals:
        for card in soup.select(".search-item-card-wrapper-gallery, .list--gallery--C2f2tvm a"):
            if len(deals) >= max_results:
                break
            try:
                title_el = card.select_one("h3, .multi--titleText--nXeOvyr")
                price_el = card.select_one(".multi--price-sale--U-S0jtj, .search-card-e-price-main")
                if not (title_el and price_el):
                    continue
                title = title_el.get_text(strip=True)
                if not _is_relevant_title(title, query):
                    continue
                price = _parse_price(price_el.get_text(strip=True))
                if not price or price <= 0:
                    continue
                href = card.get("href") or ""
                if not href:
                    link_el = card.select_one("a[href]")
                    href = link_el.get("href", "") if link_el else ""
                link = href if href.startswith("http") else f"https://www.aliexpress.com{href}"
                deals.append(Deal(title[:90], price, link, "AliExpress", "New"))
            except Exception:
                continue
    return deals


# ──────────────────────────────────────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────────────────────────────────────

_RANK = {1: "[bold gold1]1st[/bold gold1]", 2: "[bold grey74]2nd[/bold grey74]", 3: "[bold orange3]3rd[/bold orange3]"}
_COND_STYLE = {
    "New":         "bright_green",
    "Refurbished": "cyan",
    "Used":        "yellow",
    "Open Box":    "magenta",
    "Unknown":     "dim",
}


def _dedupe_and_sort(deals: List[Deal]) -> List[Deal]:
    seen_urls: set = set()
    unique: List[Deal] = []
    for d in deals:
        key = d.url.rstrip("/") if d.url else f"{d.source}::{d.title}::{d.price}"
        if key not in seen_urls:
            seen_urls.add(key)
            unique.append(d)
    unique.sort(key=lambda d: (0, d.unit_price) if d.unit_price is not None else (1, d.total_price))
    return unique


def display_results(deals: List[Deal], query: str) -> None:
    unique = _dedupe_and_sort(deals)

    if not unique:
        console.print(
            Panel(
                f"[red]No results found for:[/red] [bold]{query}[/bold]\n"
                "[dim]Try a different search term or enable more sources.[/dim]",
                title="[bold cyan]DealFindr[/bold cyan]",
                border_style="red",
            )
        )
        return

    header = Text()
    header.append("DealFindr", style="bold cyan")
    header.append("  ·  ", style="dim")
    header.append(str(len(unique)), style="bold white")
    header.append(" deals for  ", style="dim")
    header.append(query, style="bold yellow")
    console.print()
    console.print(Panel(header, border_style="cyan"))
    console.print()

    table = Table(
        show_header=True,
        header_style="bold cyan",
        show_lines=True,
        expand=True,
        border_style="bright_black",
    )
    table.add_column("#",        width=4,  justify="center")
    table.add_column("Source",   min_width=10, style="cyan")
    table.add_column("Cond.",    width=6)
    table.add_column("Price",    min_width=8, justify="right", style="bold green")
    table.add_column("Shipping", min_width=8, justify="right")
    table.add_column("Size",     min_width=10, max_width=22)
    table.add_column("$/oz",     min_width=7,  justify="right", style="bold yellow")
    table.add_column("Title",    min_width=30, ratio=1, style="white")

    cheapest = unique[0].total_price

    for i, d in enumerate(unique, 1):
        rank = _RANK.get(i, f"[dim]{i}[/dim]")

        price_str = f"${d.price:.2f}"
        if d.location:
            price_str += f"\n[dim]{d.location}[/dim]"

        cond_style = _COND_STYLE.get(d.condition, "white")
        cond_str = f"[{cond_style}]{d.condition}[/{cond_style}]"

        if d.shipping is None:
            ship_str = "[dim]—[/dim]"
        elif d.shipping == 0.0:
            ship_str = "[bold green]FREE[/bold green]"
        else:
            ship_str = f"[yellow]+${d.shipping:.2f}[/yellow]"

        size_str = f"[dim]{d.size}[/dim]" if d.size else "[dim]—[/dim]"

        if d.unit_price is not None:
            up_str = f"${d.unit_price:.2f}"
        else:
            up_str = "[dim]—[/dim]"

        table.add_row(rank, d.source, cond_str, price_str, ship_str, size_str, up_str, d.title)

    console.print(table)

    # ── Clickable purchase links ──────────────────────────────────────────────
    console.print()
    console.print("[bold cyan]Purchase Links -- best value to most expensive:[/bold cyan]")
    console.print()
    for i, d in enumerate(unique, 1):
        total_str = f"${d.total_price:.2f}"
        if d.shipping == 0.0:
            total_str += " (free ship)"
        elif d.shipping:
            total_str += f" (+${d.shipping:.2f})"

        console.print(
            f"  [dim]{i:>2}.[/dim]  "
            f"[bold green]{total_str:<18}[/bold green]"
            f"[cyan]{d.source:<28}[/cyan]"
            f"[blue][link={d.url}]{d.url}[/link][/blue]"
        )
    console.print()


def export_csv(deals: List[Deal], query: str) -> str:
    """Return CSV string of results sorted cheapest → most expensive."""
    unique = _dedupe_and_sort(deals)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Rank", "Source", "Condition", "Price", "Shipping", "Total", "Size", "$/oz", "Title", "URL"])
    for i, d in enumerate(unique, 1):
        writer.writerow([
            i, d.source, d.condition,
            f"{d.price:.2f}",
            f"{d.shipping:.2f}" if d.shipping is not None else "",
            f"{d.total_price:.2f}",
            d.size or "",
            f"{d.unit_price:.2f}" if d.unit_price is not None else "",
            d.title, d.url,
        ])
    return buf.getvalue()


def export_json(deals: List[Deal], query: str) -> str:
    """Return JSON string of results sorted cheapest → most expensive."""
    unique = _dedupe_and_sort(deals)
    return json.dumps(
        {
            "query": query,
            "count": len(unique),
            "deals": [
                {
                    "rank": i,
                    "title": d.title,
                    "price": d.price,
                    "shipping": d.shipping,
                    "total_price": d.total_price,
                    "source": d.source,
                    "condition": d.condition,
                    "url": d.url,
                    "location": d.location,
                    "size": d.size,
                    "unit_oz": d.unit_oz,
                    "unit_price": d.unit_price,
                }
                for i, d in enumerate(unique, 1)
            ],
        },
        indent=2,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _build_scrapers(
    query: str, args: argparse.Namespace
) -> List[Tuple[str, Callable]]:
    scrapers: List[Tuple[str, Callable]] = []
    m = args.max_results
    c = args.cities or ["chicago"]
    selected = set(args.source or _SOURCE_LABELS.keys())

    if "ebay" in selected and not args.no_ebay:
        scrapers.append(("eBay",            lambda q=query, n=m: search_ebay(q, n)))
    if "craigslist" in selected and not args.no_craigslist:
        scrapers.append(("Craigslist",      lambda q=query, cities=c, n=m: search_craigslist(q, cities, n)))
    if "amazon" in selected and not args.no_amazon:
        scrapers.append(("Amazon",          lambda q=query, n=m: search_amazon(q, n)))
    if "walmart" in selected and not args.no_walmart:
        scrapers.append(("Walmart",         lambda q=query, n=m: search_walmart(q, n)))
    if "bestbuy" in selected and not args.no_bestbuy:
        scrapers.append(("Best Buy",        lambda q=query, n=m: search_bestbuy(q, n)))
    if "target" in selected and not getattr(args, 'no_target', False):
        scrapers.append(("Target",          lambda q=query, n=m: search_target(q, n)))
    if "newegg" in selected and not getattr(args, 'no_newegg', False):
        scrapers.append(("Newegg",          lambda q=query, n=m: search_newegg(q, n)))
    if "mercari" in selected and not getattr(args, 'no_mercari', False):
        scrapers.append(("Mercari",         lambda q=query, n=m: search_mercari(q, n)))
    if "swappa" in selected and not getattr(args, 'no_swappa', False):
        scrapers.append(("Swappa",          lambda q=query, n=m: search_swappa(q, n)))
    if "aliexpress" in selected and not getattr(args, 'no_aliexpress', False):
        scrapers.append(("AliExpress",      lambda q=query, n=m: search_aliexpress(q, n)))
    if "google" in selected and not args.no_google:
        scrapers.append(("Google Shopping", lambda q=query, n=m: search_google_shopping(q, n)))

    return scrapers


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dealfindr",
        description="DealFindr — Find the best prices across multiple shopping platforms.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python dealfindr.py "apple thunderbolt 2 cable"
  python dealfindr.py --interactive
  python dealfindr.py macbook pro --no-craigslist --max-results 10
  python dealfindr.py "nintendo switch" --cities chicago
  python dealfindr.py "apple thunderbolt 2 cable" --source amazon
  python dealfindr.py "macbook pro" --source amazon ebay craigslist
  python dealfindr.py "iphone 15" --export results.csv
        """,
    )
    parser.add_argument("query", nargs="*", help="Item to search for")
    parser.add_argument("--interactive", action="store_true", help="Launch interactive setup")
    parser.add_argument(
        "--source",
        nargs="+",
        choices=sorted(_SOURCE_LABELS.keys()),
        help="Only search specific sources",
    )
    parser.add_argument("--no-ebay", action="store_true", help="Skip eBay")
    parser.add_argument("--no-amazon", action="store_true", help="Skip Amazon")
    parser.add_argument("--no-craigslist", action="store_true", help="Skip Craigslist")
    parser.add_argument("--no-walmart", action="store_true", help="Skip Walmart")
    parser.add_argument("--no-bestbuy", action="store_true", help="Skip Best Buy")
    parser.add_argument("--no-google", action="store_true", help="Skip Google Shopping")
    parser.add_argument("--no-target", action="store_true", help="Skip Target")
    parser.add_argument("--no-newegg", action="store_true", help="Skip Newegg")
    parser.add_argument("--no-mercari", action="store_true", help="Skip Mercari")
    parser.add_argument("--no-swappa", action="store_true", help="Skip Swappa")
    parser.add_argument("--no-aliexpress", action="store_true", help="Skip AliExpress")
    parser.add_argument(
        "--cities", nargs="+", metavar="CITY",
        help="Craigslist city slugs to search (default: chicago)",
    )
    parser.add_argument(
        "--max-results", type=int, default=20, metavar="N",
        help="Max results per source (default: 20)",
    )
    parser.add_argument(
        "--export", metavar="FILE",
        help="Export results to a CSV file",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON instead of the Rich table",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose logging output",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Debug-level logging (very detailed)",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {__version__}",
    )

    args = parser.parse_args()

    # Configure logging based on verbosity flags.
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
        )
    elif args.verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s: %(message)s",
        )
    else:
        logging.basicConfig(level=logging.WARNING)

    query = " ".join(args.query).strip()
    if args.interactive or not query:
        query, args = _interactive_setup(args)

    if not query:
        parser.error("Please provide a search query or run with --interactive.")

    scrapers = _build_scrapers(query, args)

    # Generate unit-converted alternate queries (e.g. 2lb → 32oz)
    alt_queries = _generate_alt_queries(query)
    for alt_q in alt_queries:
        alt_m = _WEIGHT_RE.search(alt_q)
        alt_tag = alt_m.group(0) if alt_m else "alt"
        for name, fn in _build_scrapers(alt_q, args):
            scrapers.append((f"{name} ({alt_tag})", fn))

    if not scrapers:
        parser.error("No sources selected. Remove --no-* flags or use --source with at least one source.")

    active_sources = " · ".join(name for name, _ in scrapers)

    console.print()
    console.print(
        Panel(
            f"[bold cyan]DealFindr[/bold cyan]  [dim]|[/dim]  "
            f"Searching for [bold yellow]{query}[/bold yellow]\n"
            f"[dim]Scraping {active_sources}[/dim]",
            border_style="cyan",
        )
    )
    console.print()
    all_deals: List[Deal] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task_map = {
            name: progress.add_task(f"  [cyan]{name}[/cyan]...", total=None)
            for name, _ in scrapers
        }

        with ThreadPoolExecutor(max_workers=min(len(scrapers), 12)) as executor:
            futures = {executor.submit(fn): name for name, fn in scrapers}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results = future.result()
                    all_deals.extend(results)
                    progress.update(
                        task_map[name],
                        description=(
                            f"  [green]✓[/green] {name} "
                            f"[dim]({len(results)} result{'s' if len(results) != 1 else ''})[/dim]"
                        ),
                    )
                except Exception as exc:
                    log.warning("Scraper %s failed: %s", name, exc)
                    progress.update(
                        task_map[name],
                        description=f"  [red]✗[/red] {name} [dim](failed)[/dim]",
                    )

    # Post-process: extract size/weight and unit price from titles
    for d in all_deals:
        if not d.size:
            d.size = _extract_size(d.title)
        if d.unit_oz is None:
            d.unit_oz = _title_to_oz(d.title)

    if args.json:
        print(export_json(all_deals, query))
    else:
        display_results(all_deals, query)

    if args.export:
        csv_data = export_csv(all_deals, query)
        with open(args.export, "w", newline="", encoding="utf-8") as f:
            f.write(csv_data)
        console.print(f"[green]✓[/green] Results exported to [bold]{args.export}[/bold]\n")


if __name__ == "__main__":
    main()
