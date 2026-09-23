#!/usr/bin/env python3
"""
dealfindr_cron.py — Cron entry point for daily cold-brew coffee deal alerts.

Runs as a Kubernetes CronJob on the TC cluster. Searches Amazon, Walmart,
Target, Google Shopping, and Craigslist (Chicago metro / 60480) for cold-brew
coffee, filters by brand / size / price, and posts any matches to a Discord
webhook.

Environment variables (see k8s/configmap.yaml + secret.yaml):
  DEALFINDR_QUERY         Base search query            (default: "cold brew coffee")
  DEALFINDR_BRANDS        Comma-separated brand allowlist
                          (default: "La Colombe,Bizzy,Starbucks,Califia")
  DEALFINDR_MIN_SIZE_OZ   Minimum total volume in fl oz (default: 48)
  DEALFINDR_MAX_PRICE     Bottled price cap (price + shipping) in USD; a deal
                          must be strictly below it (default: 4.50 — $4.99 is
                          regular shelf price for 48oz, not a deal)
  DEALFINDR_MAX_RESULTS   Max results per source        (default: 40)
  DISCORD_WEBHOOK_URL     Discord webhook for the dealfindr channel (secret)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
from urllib.parse import quote_plus

import requests
from playwright.sync_api import sync_playwright, Browser

# Import the deal-finding machinery from the main module.
from dealfindr import (
    Deal,
    _extract_size,
    _get,
    _headers,
    _parse_amazon_results,
    _parse_price,
    _title_to_oz,
    search_craigslist,
    search_walmart,
)

# --------------------------------------------------------------------------- #
# Flipp weekly ad scraper (for local grocery stores)
# --------------------------------------------------------------------------- #

# Tony's Fresh Market (Countryside, IL) and Brookhaven Markets (Burr Ridge)
# are on Flipp. The search API returns flyer items with prices.
_FLIPP_SEARCH_URL = "https://backflipp.wishabi.com/flipp/items/search"
_FLIPP_FLYER_URL = "https://flipp.com/flyer/{flyer_id}"
_FLIPP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://flipp.com",
    "Referer": "https://flipp.com/",
}

# Local grocery stores to track on Flipp.
_FLIPP_STORES = {
    "Tony's Fresh Market": 8070327,
    "Jewel-Osco": 8080940,
    # Brookhaven Markets (flyer_id=8073332) has no searchable items on Flipp.
}

# Flipp search queries — brand-specific queries return more results than
# generic "cold brew coffee" for local grocery stores.
_FLIPP_QUERIES = [
    "cold brew coffee",
    "Starbucks",
    "La Colombe",
    "Califia",
    "Bizzy",
    "coffee",
]


def search_flipp(query: str = "", postal_code: str = "60108", max_results: int = 40) -> List[Deal]:
    """Search Flipp weekly ads for cold brew deals at local grocery stores.

    Uses broad brand-specific queries instead of a single generic query since
    Flipp's API returns very few results for "cold brew coffee" (0-5 items)
    but many more for brand names like "Starbucks" (10+ items including Tony's).

    The Flipp API ignores merchant_id/flyer_id filters, so we search broadly
    and filter client-side by merchant_name.
    """
    deals: List[Deal] = []
    queries_to_try = _FLIPP_QUERIES if not query else [query]

    for q in queries_to_try:
        params = {
            "q": q,
            "postal_code": postal_code,
            "locale": "en",
        }
        try:
            r = requests.get(_FLIPP_SEARCH_URL, params=params, headers=_FLIPP_HEADERS, timeout=15)
            if r.status_code != 200:
                _log(f"  search_flipp({q!r}) HTTP {r.status_code}")
                continue
            data = r.json()
        except Exception as exc:
            _log(f"  search_flipp({q!r}) error: {exc}")
            continue

        items = data.get("items", [])
        store_names = set(_FLIPP_STORES.keys())

        for item in items:
            if len(deals) >= max_results:
                break

            merchant = (item.get("merchant_name") or "").strip()
            if merchant not in store_names:
                continue

            name = (item.get("name") or "").strip()
            if not name:
                continue

            price = item.get("current_price")
            if price is None or price <= 0:
                continue

            # Build the flyer URL.
            flyer_id = item.get("flyer_id")
            url = _FLIPP_FLYER_URL.format(flyer_id=flyer_id) if flyer_id else "https://flipp.com"

            # Include sale story in title for context (e.g., "BUY 3 SAVE $3").
            sale_story = (item.get("sale_story") or "").strip()
            post_price = (item.get("post_price_text") or "").strip()
            title = name
            if sale_story:
                title = f"{name} [{sale_story}]"
            if post_price:
                title = f"{title} {post_price}"

            source = f"Flipp ({merchant})"
            deals.append(Deal(title[:120], float(price), url, source, "New", shipping=0.0))

    return deals


# --------------------------------------------------------------------------- #
# Amazon coupon extraction
# --------------------------------------------------------------------------- #

# Amazon embeds coupon data in search results via coupon-component divs.
# We extract ASIN → coupon text to flag deals that have available discounts.

_COUPON_ASIN_RE = re.compile(
    r'data-component-props="{[^"]*&quot;asin&quot;:&quot;([^&]+)&quot;',
    re.DOTALL,
)
_COUPON_TEXT_RE = re.compile(r's-coupon-highlight-color[^>]*>([^<]+)<', re.DOTALL)
# Subscribe & Save: "Extra 30% off when you subscribe"
_SS_PCT_RE = re.compile(r'Extra\s+(\d+)%\s+off', re.IGNORECASE)


def _extract_amazon_coupons(html: str) -> dict:
    """Parse Amazon search HTML for ASIN-specific coupon data.

    Returns a dict mapping ASIN → dict with keys:
      - label: human-readable coupon description
      - pct_off: percentage off (for Subscribe & Save), or None
    """
    coupons: dict = {}

    # Subscribe & Save percentage coupons (coupon-component blocks)
    blocks = re.split(r'coupon-component', html)
    for block in blocks[1:]:
        asin_match = _COUPON_ASIN_RE.search(block)
        text_match = _COUPON_TEXT_RE.search(block)
        if asin_match and text_match:
            asin = asin_match.group(1)
            text = text_match.group(1).strip()
            if asin in coupons:
                continue
            pct_match = _SS_PCT_RE.search(text)
            if pct_match:
                pct = int(pct_match.group(1))
                coupons[asin] = {
                    "label": f"Extra {pct}% off w/ Subscribe & Save",
                    "pct_off": pct,
                }
            elif text:
                coupons[asin] = {
                    "label": text,
                    "pct_off": None,
                }

    return coupons


def _parse_amazon_full(html: str, max_results: int = 40) -> List[Deal]:
    """Custom Amazon parser that uses img[alt] for full titles.

    The upstream _parse_amazon_results uses h2 span which gives truncated
    titles on brand-specific searches (e.g. "La Colombe" instead of the
    full product name). This parser uses the image alt text which always
    contains the complete title.
    """
    from bs4 import BeautifulSoup

    deals: List[Deal] = []
    soup = BeautifulSoup(html, "html.parser")

    for result in soup.select('[data-component-type="s-search-result"]'):
        if len(deals) >= max_results:
            break
        try:
            # Full title from image alt text (much more reliable than h2 span).
            img = result.select_one("img[alt]")
            title = (img.get("alt") or "").strip() if img else ""
            # Strip "Sponsored Ad - " prefix if present.
            if title.lower().startswith("sponsored ad - "):
                title = title[15:].strip()
            if not title:
                continue

            # Price
            whole = result.select_one(".a-price-whole")
            frac = result.select_one(".a-price-fraction")
            if not whole:
                continue
            price_str = whole.get_text(strip=True).replace(",", "").rstrip(".")
            if frac:
                price_str += "." + frac.get_text(strip=True).strip()
            price = _parse_price(price_str)
            if not price or price <= 0:
                continue

            # Link
            link_el = result.select_one("a.a-link-normal.s-no-outline, h2 a")
            href = link_el.get("href", "") if link_el else ""
            if href.startswith("/"):
                link = f"https://www.amazon.com{href}"
            elif href.startswith("http"):
                link = href
            else:
                continue
            # Strip tracking params
            link = re.sub(r"/ref=.*", "", link)

            # Shipping
            shipping: Optional[float] = None
            free_ship = result.select_one('[aria-label*="FREE delivery"], .s-free-delivery-text')
            if free_ship:
                shipping = 0.0

            deals.append(Deal(title[:120], price, link, "Amazon", "New", shipping=shipping))
        except Exception:
            continue

    # Filter to only cold-brew-relevant results (the upstream parser does this
    # via _is_relevant_title, but we bypass it for full titles from img[alt]).
    deals = [d for d in deals if "cold brew" in d.title.lower()]

    return deals


def _search_amazon_with_coupons(query: str, max_results: int = 40) -> tuple:
    """Search Amazon and return (deals, coupons_dict).

    Uses our custom parser (_parse_amazon_full) for full titles from img[alt],
    then falls back to the upstream parser if that returns nothing.
    """
    deals: list = []
    coupons: dict = {}
    seen_urls: set = set()
    hdrs = _headers("https://www.amazon.com/")

    for condition_filter, default_cond in (("", "New"), ("&condition=used", "Used")):
        url = f"https://www.amazon.com/s?k={quote_plus(query)}{condition_filter}"
        resp = None
        batch: list = []
        for attempt in range(2):
            if attempt:
                time.sleep(2.0)
            resp = _get(url, headers=_headers("https://www.amazon.com/"), timeout=16)
            if not resp:
                continue
            lower_text = resp.text.lower()
            if "captcha" in lower_text or "enter the characters you see below" in lower_text:
                _log(f"  [amazon] {query!r} {default_cond} bot page; retry {attempt + 1}/2")
                continue

            batch = _parse_amazon_full(resp.text, max_results)
            if not batch:
                batch = _parse_amazon_results(resp.text, query, default_cond, max_results)
            if batch:
                break

        if not resp or not batch:
            _log(f"  [amazon] {query!r} {default_cond}: no usable results after retries")
            continue

        # Extract coupons from this page's HTML
        page_coupons = _extract_amazon_coupons(resp.text)
        coupons.update(page_coupons)

        for deal in batch:
            if deal.url not in seen_urls:
                seen_urls.add(deal.url)
                deals.append(deal)

    return deals, coupons


# Regex to extract ASIN from Amazon URLs.
_ASIN_FROM_URL = re.compile(r'(?:/dp/|%2Fdp%2F|ASIN%3D|asin=)([A-Z0-9]{10})', re.IGNORECASE)

# Patterns for discounts on Amazon product pages (not always visible on search).
_PRODUCT_SS_RE = re.compile(
    r'(?:snsDiscountPercent[^:]*:\s*(\d+)|'
    r'Save\s+(\d+)%|'
    r'Extra\s+(\d+)%\s+(?:off|on)\s*(?:with|when|on)?\s*(?:[Ss]ubscribe|[Ss]ubscription|your\s+first))',
    re.IGNORECASE | re.DOTALL,
)
_PRODUCT_CLIP_RE = re.compile(
    r'(?:couponBadge[^>]*>\s*[Yy]ou\s+[Pp]ay\s+\$(\d+\.?\d*)\s+with\s+coupon|'
    r'[Cc]lip.{0,30}[Cc]oupon.{0,100}[Ss]ave\s+\$(\d+\.?\d*)|'
    r'[Cc]lip.{0,30}[Cc]oupon.{0,100}(\d+)%\s*[Oo]ff)',
    re.DOTALL,
)


def _check_amazon_product_discount(deal: Deal) -> Optional[float]:
    """Check an Amazon product page for the real price and any discounts.

    Search page prices are often stale/rounded. The product page has the
    authoritative price. We also check for S&S and clip coupons.

    Returns the best available price (real price minus any discounts), or None.
    """
    try:
        asin_match = _ASIN_FROM_URL.search(deal.url or "")
        asin = asin_match.group(1) if asin_match else None
        if not asin:
            return None

        url = f"https://www.amazon.com/dp/{asin}"
        resp = _get(url, headers=_headers("https://www.amazon.com/"), timeout=12)
        if not resp:
            _log(f"  [amazon] {asin}: no response")
            return None

        # Amazon rate-limits bursts of product-page requests after a search sweep.
        # A bot/challenge page has no ".a-offscreen" price, so detect it and retry
        # once after a short backoff before giving up.
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        if not soup.select_one(".a-offscreen") and not soup.select_one(".a-price-whole"):
            _log(f"  [amazon] {asin}: bot page (len={len(resp.text)}), retrying")
            time.sleep(1.5)
            resp = _get(url, headers=_headers("https://www.amazon.com/"), timeout=12)
            if not resp:
                _log(f"  [amazon] {asin}: no response on retry")
                return None
            soup = BeautifulSoup(resp.text, "html.parser")

        page_lower = resp.text.lower()
        location_warning = (
            "cannot be shipped to your selected delivery location" in page_lower
            or "this item cannot be shipped" in page_lower
        )
        if location_warning:
            # The pod's Amazon session has no user's ZIP/address cookie. This
            # message reflects the cluster's default location, not the buyer's.
            _log(f"  [amazon] {asin}: delivery location not verified; keeping listed price")

        if (
            "currently unavailable" in page_lower
            or "we don't know when or if this item will be back in stock" in page_lower
        ):
            # Product pages can select a different variant or inherit the
            # cluster's location. Preserve the search-card price, which is the
            # offer the user actually saw, unless a newer price was parsed.
            _log(f"  [amazon] {asin}: product availability not verified; keeping listed price")
            return None

        # The main price is the first ".a-price-whole" + ".a-price-fraction" pair
        # (the default/selected variant). The whole part already contains the
        # decimal point (e.g. "3."), so we strip it before appending the fraction.
        # This avoids the ".a-offscreen" trap where per-ounce ("$0.08") and
        # smaller-variant prices appear before the real 48oz price.
        real_price = None
        whole = soup.select_one(".a-price-whole")
        frac = soup.select_one(".a-price-fraction")
        if whole:
            price_str = whole.get_text(strip=True).replace(",", "").rstrip(".")
            if frac:
                price_str += "." + frac.get_text(strip=True)
            try:
                real_price = float(price_str)
            except ValueError:
                real_price = None

        # Fallback: the clean ".priceToPay" element (e.g. "$3.99").
        if not real_price:
            price_to_pay = soup.select_one(".priceToPay")
            if price_to_pay:
                real_price = _parse_price(price_to_pay.get_text(strip=True))

        if not real_price or real_price <= 0:
            offscreen = soup.select_one(".a-offscreen")
            _log(
                "  [amazon] %s: no price parsed "
                "(offscreen=%s)" % (
                    asin,
                    offscreen.get_text(strip=True) if offscreen else "none",
                )
            )
            return None

        # NOTE: We deliberately do NOT apply Subscribe & Save discounts. S&S is a
        # recurring subscription (not a one-time deal), so the "5% off" price is
        # misleading — the real one-time price is the regular list price.

        # Check for clip coupon.
        clip_m = _PRODUCT_CLIP_RE.search(resp.text)
        if clip_m:
            if clip_m.group(1):  # "You pay $X.XX with coupon"
                return float(clip_m.group(1))
            if clip_m.group(2):  # "Save $X.XX"
                return real_price - float(clip_m.group(2))
            if clip_m.group(3):  # "X% off"
                pct = int(clip_m.group(3))
                return round(real_price * (1 - pct / 100), 2)

        # No discount, but return the real price so we don't use stale search price.
        return real_price
    except Exception as exc:
        _log(f"  [amazon] error checking product discount: {exc}")
        return None


def _apply_coupons_to_deals(deals: list, coupons: dict) -> list:
    """Apply ASIN-specific coupon discounts to deals.

    Applies only clip coupons ("You pay $23.99", "Extra $3.00 off") tied to
    specific ASINs. Subscribe & Save percentage-off coupons are NOT applied —
    S&S is a recurring subscription, not a one-time deal, so its discounted
    price is misleading.

    Site-wide spend-based promos ("Save $X when you spend $Y of select items")
    are NOT applied since we can't verify which products qualify.
    """
    for deal in deals:
        asin_match = _ASIN_FROM_URL.search(deal.url or "")
        asin = asin_match.group(1) if asin_match else None

        if asin and asin in coupons:
            c = coupons[asin]
            if c["pct_off"]:
                # Subscribe & Save — skip (recurring subscription, not a deal).
                continue
            # Dollar-based clip coupons ("You pay $X" or "Extra $X off")
            you_pay_m = re.search(r'You pay \$(\d+(?:\.\d+)?)', c.get("label", ""), re.IGNORECASE)
            dollar_off_m = re.search(r'\$(\d+(?:\.\d+)?)\s*off', c.get("label", ""), re.IGNORECASE)
            if you_pay_m:
                # Absolute coupon price — set deal.price directly
                coupon_price = float(you_pay_m.group(1))
                if coupon_price > 0 and (deal.price is None or coupon_price < deal.price):
                    deal.price = coupon_price
                    deal.title = f"[Clip ${coupon_price:.2f}] {deal.title}"
            elif dollar_off_m and deal.price:
                # Dollar-off coupon — subtract from price
                dollars_off = float(dollar_off_m.group(1))
                if dollars_off > 0:
                    deal.price = round(max(0.0, deal.price - dollars_off), 2)
                    deal.title = f"[${dollars_off:.2f} off] {deal.title}"
            elif c.get("label"):
                # Just annotate — no price change
                deal.title = f"[{c['label']}] {deal.title}"

    return deals

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

QUERY = os.getenv("DEALFINDR_QUERY", "cold brew coffee").strip()
BRANDS_RAW = os.getenv("DEALFINDR_BRANDS", "La Colombe,Bizzy,Starbucks,Califia,Stok,Stōk").strip()
BRANDS = [b.strip().lower() for b in BRANDS_RAW.split(",") if b.strip()]
try:
    MIN_SIZE_OZ = float(os.getenv("DEALFINDR_MIN_SIZE_OZ", "48"))
except ValueError:
    MIN_SIZE_OZ = 48.0
try:
    MAX_PRICE = float(os.getenv("DEALFINDR_MAX_PRICE", "4.50"))
except ValueError:
    MAX_PRICE = 4.50
try:
    MAX_PRICE_PER_CAN = float(os.getenv("DEALFINDR_MAX_PRICE_PER_CAN", "1.0"))
except ValueError:
    MAX_PRICE_PER_CAN = 1.0
try:
    MAX_RESULTS = int(os.getenv("DEALFINDR_MAX_RESULTS", "40"))
except ValueError:
    MAX_RESULTS = 40

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# When true, post a short "no deals found" heartbeat to Discord on quiet days
# so you know the job ran (instead of silence).
HEARTBEAT = os.getenv("DEALFINDR_HEARTBEAT", "false").lower() in ("1", "true", "yes")

# Additional brand-specific search terms to widen the net. Each brand gets a
# dedicated query appended to the base query so we catch listings that don't
# include the generic "cold brew" phrase.
BRAND_QUERIES = {
    "la colombe": "La Colombe cold brew",
    "bizzy": "Bizzy cold brew",
    "starbucks": "Starbucks cold brew",
    "califia": "Califia cold brew",
}

# Extra queries for specific product formats.
EXTRA_QUERIES = [
    "canned cold brew coffee",
]

# Craigslist cities near 60480 (Willow Springs, IL — Chicago metro).
CL_CITIES = ["chicago"]

# --------------------------------------------------------------------------- #
# Playwright-based scrapers (for JS-rendered sites)
# --------------------------------------------------------------------------- #

# Shared browser instance — launched once per run, reused across searches.
_browser: Optional[Browser] = None


def _get_browser() -> Browser:
    """Return a shared headless Chromium browser, launching it on first call."""
    global _browser
    if _browser is None:
        pw = sync_playwright().start()
        _browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
    return _browser


def _close_browser() -> None:
    """Close the shared browser if it was launched."""
    global _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None


def search_target_playwright(query: str, max_results: int = 20) -> List[Deal]:
    """Target.com search via headless Chromium — renders JS product cards."""
    deals: List[Deal] = []
    url = f"https://www.target.com/s?searchTerm={requests.utils.quote(query)}&sortBy=PriceLow"
    try:
        browser = _get_browser()
        page = browser.new_page()
        try:
            page.set_default_timeout(20000)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

            # Target product cards use data-test="product-details" wrapper.
            cards = page.query_selector_all('[data-test="product-details"]')
            _log(f"    [target] cards: {len(cards)}")

            for card in cards[:max_results]:
                try:
                    # Title: <a data-test="@web/ProductCard/title">
                    title_el = card.query_selector('a[data-test="@web/ProductCard/title"]')
                    # Price: <span data-test="current-price">
                    price_el = card.query_selector('span[data-test="current-price"]')

                    if not title_el or not price_el:
                        continue
                    title = (title_el.inner_text() or "").strip()
                    price = _parse_price((price_el.inner_text() or "").strip())
                    if not price or price <= 0:
                        continue
                    href = title_el.get_attribute("href") or ""
                    link = f"https://www.target.com{href}" if href.startswith("/") else href

                    deals.append(Deal(title[:90], price, link, "Target", "New"))
                except Exception:
                    continue
        finally:
            page.close()
    except Exception as exc:
        _log(f"  search_target_playwright({query!r}) error: {exc}")
    return deals


def search_google_shopping_playwright(query: str, max_results: int = 20) -> List[Deal]:
    """Google Shopping via headless Chromium.

    NOTE: Google frequently serves reCAPTCHA to headless browsers, especially
    from datacenter/container IPs. When blocked, this returns 0 results silently.
    """
    deals: List[Deal] = []
    url = f"https://www.google.com/search?q={requests.utils.quote(query)}&tbm=shop&hl=en&gl=us"
    try:
        browser = _get_browser()
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )
        try:
            page = context.new_page()
            try:
                page.set_default_timeout(20000)
                page.goto(url, wait_until="networkidle")
                page.wait_for_timeout(3000)

                # Check for CAPTCHA
                body_text = (page.inner_text("body") or "").lower()
                if "captcha" in body_text or "not a robot" in body_text:
                    _log(f"    [google] CAPTCHA detected — skipping")
                    return deals

                # Try multiple selector patterns for product cards
                cards = page.query_selector_all(".sh-dgr__content, .sh-dlr__list-result, div[data-sh-d]")
                if not cards:
                    cards = page.query_selector_all(".mnr-c")
                if not cards:
                    cards = page.query_selector_all("div[data-sh-dgr]")
                _log(f"    [google] cards: {len(cards)}")

                for card in cards[:max_results]:
                    try:
                        title_el = card.query_selector("h3, .tAxDx, .EI11Pd, .sh-np__product-title")
                        price_el = card.query_selector(".a8Pemb, .kHxwFf, .HRLxBb, .T14wmb")
                        link_el = card.query_selector("a[href]")
                        store_el = card.query_selector(".aULzUe, .LbUacb, .shntl, .IuHnof")

                        if not title_el or not price_el:
                            continue
                        title = (title_el.inner_text() or "").strip()
                        price = _parse_price((price_el.inner_text() or "").strip())
                        if not price or price <= 0:
                            continue

                        href = link_el.get_attribute("href") if link_el else ""
                        link = ""
                        if href:
                            if href.startswith("/url?"):
                                m = re.search(r"[?&]q=([^&]+)", href)
                                link = requests.utils.unquote(m.group(1)) if m else href
                            elif href.startswith("http"):
                                link = href
                            else:
                                link = f"https://www.google.com{href}"

                        store = (store_el.inner_text() or "").strip() if store_el else ""
                        source = f"Google Shopping ({store})" if store else "Google Shopping"

                        deals.append(Deal(title[:90], price, link, source, "New"))
                    except Exception:
                        continue
            finally:
                page.close()
        finally:
            context.close()
    except Exception as exc:
        _log(f"  search_google_shopping_playwright({query!r}) error: {exc}")
    return deals

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _log(msg: str) -> None:
    """Print with a flush so K8s captures logs immediately."""
    print(msg, flush=True)


def _build_queries() -> List[str]:
    """Return the set of queries to run (base + brand-specific + format-specific, de-duplicated)."""
    queries = [QUERY]
    for brand in BRANDS:
        bq = BRAND_QUERIES.get(brand)
        if bq and bq.lower() not in [q.lower() for q in queries]:
            queries.append(bq)
    for eq in EXTRA_QUERIES:
        if eq.lower() not in [q.lower() for q in queries]:
            queries.append(eq)
    return queries


# Exclude non-ready-to-drink coffee (pods, grounds, beans, equipment, creamers).
_NON_RTD_RE = re.compile(
    r'\b(?:k[- ]?cups?|pods?|capsules?|discs?|ground\b|grounds\b|whole\s*beans?|beans\b|'
    r'pitcher\s*packs?|filter\s*packs?|brew\s*bags?|cold\s*brew\s*maker|'
    r'creamer|creamers)\b',
    re.IGNORECASE,
)


def _is_non_rtd(title: str) -> bool:
    """True if the item is not ready-to-drink coffee (pods, grounds, creamer, equipment)."""
    return bool(_NON_RTD_RE.search(title))


# Regex to detect canned formats (requires actual can/canned terminology).
_CAN_KEYWORD_RE = re.compile(
    r'\b(?:cans?|canned)\b',
    re.IGNORECASE,
)


def _is_canned(title: str) -> bool:
    """True if the title explicitly mentions cans or canned coffee."""
    return bool(_CAN_KEYWORD_RE.search(title))


def _extract_can_count(title: str) -> Optional[int]:
    """Extract the number of cans from a product title.

    Handles patterns like:
      - '12 cans', 'Pack of 12 cans', '12-pack ... cans', '12 pack 11 fl oz cans'
    """
    if not _is_canned(title):
        return None
    # "12 cans", "12 Cans", "Pack of 12 cans", "12-pack 11 fl oz cans"
    m = re.search(r'(\d+)\s*[-/]?\s*(?:pack|pk)?\s*(?:of\s*)?(\d+)?\s*(?:fl\.?\s*oz\s*)?cans?\b', title, re.IGNORECASE)
    if m:
        return int(m.group(2) or m.group(1))
    m = re.search(r'Pack\s*(?:of|size)?\s*(\d+)', title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*[-/]?\s*(?:can|ct|count|pack)s?\b', title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _matches_brand(title: str) -> bool:
    """True if the title mentions one of the allowed brands."""
    title_lower = title.lower()
    # Normalize common Unicode chars (e.g., Stōk → Stok).
    title_normalized = title_lower.replace("ō", "o").replace("é", "e").replace("è", "e")
    return any(brand in title_normalized for brand in BRANDS)


# Terms that disqualify a canned cold brew (milk, sweeteners, flavored).
_BLACK_COFFEE_BLACKLIST = [
    "latte", "mocha", "vanilla", "caramel", "sweet cream",
    "oatmilk", "oat milk", "almondmilk", "almond milk", "chocolate", "s'mores", "smores",
    "lightly sweetened", "sweetened", "honey", "maple", "cinnamon",
    "protein", "cream", "creamer", "frappuccino", "macchiato",
    "cappuccino", "cortado", "flat white",
]


def _is_black_coffee(title: str) -> bool:
    """True if the title is pure black cold brew (no milk, sweeteners, flavors)."""
    title_lower = title.lower()
    for term in _BLACK_COFFEE_BLACKLIST:
        # Use word-boundary matching so "sweetened" doesn't catch "unsweetened".
        if re.search(r'\b' + re.escape(term) + r'\b', title_lower):
            return False
    return True


# Regex for fluid ounce patterns that _WEIGHT_RE misses.
_FL_OZ_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:fl\.?\s*oz|fluid\s*ounces?)\b', re.IGNORECASE)


def _extract_fl_oz(title: str) -> Optional[float]:
    """Extract fluid ounces from a title (handles 'fl oz', 'Fluid Ounces')."""
    m = _FL_OZ_RE.search(title)
    if m:
        return float(m.group(1))
    return None


def _post_process(deal: Deal) -> Deal:
    """Replicate dealfindr.py main() lines 1761-1763 — fill size/unit_oz."""
    # Prefer "fl oz" (the authoritative bottle size) over bare "oz", which can
    # match a serving size (e.g. "250mg caffeine/12oz serving" → 12oz instead
    # of the real "48 fl oz" bottle).
    if deal.unit_oz is None:
        deal.unit_oz = _extract_fl_oz(deal.title)
    if deal.unit_oz is None:
        deal.unit_oz = _title_to_oz(deal.title)
    # Flipp fallback: some Flipp items don't include size in the name.
    # Infer from known products (e.g., Stōk cold brew = 48oz standard).
    if deal.unit_oz is None and "Flipp" in (deal.source or ""):
        deal.unit_oz = _flipp_infer_oz(deal.title)

    # Keep deal.size aligned with unit_oz so serving sizes like "12 oz"
    # from "12oz serving" don't override the actual bottle size.
    if deal.unit_oz is not None and deal.unit_oz > 0:
        deal.size = f"{deal.unit_oz:.0f} fl oz"
    elif not deal.size:
        deal.size = _extract_size(deal.title)

    return deal


def _flipp_infer_oz(title: str) -> Optional[float]:
    """Infer fluid ounces for Flipp items that don't include size in the name."""
    title_lower = title.lower()
    # Starbucks Iced Coffee — standard multi-serve is 48 fl oz.
    if "starbucks" in title_lower and "iced coffee" in title_lower:
        return 48.0
    # Stōk / Stok cold brew — standard bottle is 48 fl oz.
    if "stōk" in title_lower or "stok" in title_lower:
        if "cold brew" in title_lower:
            return 48.0
    # La Colombe cold brew — standard is 42 fl oz multi-serve.
    if "la colombe" in title_lower:
        return 42.0
    # Bizzy cold brew — standard is 48 fl oz.
    if "bizzy" in title_lower:
        return 48.0
    # Starbucks cold brew multi-serve — 48 fl oz.
    if "starbucks" in title_lower and "cold brew" in title_lower:
        return 48.0
    # Califia cold brew only — NOT almond/oat milk (those aren't coffee).
    if "califia" in title_lower and ("cold brew" in title_lower or "coffee" in title_lower):
        return 48.0
    return None


def _filter(deal: Deal) -> bool:
    """Apply the brand + size/price filter.

    Two deal types are supported:
    1. Bottled/jug: must match an allowed brand, be >= MIN_SIZE_OZ, and total_price < MAX_PRICE.
    2. Canned/pack: must match an allowed brand, be black coffee, have can count, and price_per_can < MAX_PRICE_PER_CAN.
    """
    if deal.price is None or deal.total_price is None:
        return False

    # Universal exclusions: pods, k-cups, capsules, grounds, whole beans, brew bags, creamers.
    if _is_non_rtd(deal.title):
        return False

    # Both bottled and canned deals must match an allowed brand.
    if not _matches_brand(deal.title):
        return False

    # Do not alert flavored, milk-based, or latte products just because the
    # title also contains the brand and "cold brew".
    if not _is_black_coffee(deal.title):
        return False

    # Check for canned/pack deals — must be explicit cans and < $1/can.
    if _is_canned(deal.title):
        can_count = _extract_can_count(deal.title)
        if can_count and can_count > 0:
            price_per_can = deal.total_price / can_count
            return price_per_can < MAX_PRICE_PER_CAN
        return False

    # Bottled/jug check — must match a brand, be >= MIN_SIZE_OZ, and total_price < MAX_PRICE.
    if deal.unit_oz is None or deal.unit_oz < MIN_SIZE_OZ:
        return False
    # Amazon search pages frequently omit shipping until the buyer selects a
    # delivery address. Keep the listed item price; the Discord message shows
    # shipping as N/A rather than treating the pod's location as authoritative.
    if deal.total_price >= MAX_PRICE:
        return False
    return True


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

# Discord webhook payloads are capped at 2000 chars per message. We chunk
# the results to stay under that limit.
DISCORD_CHAR_LIMIT = 1900


def _format_deal_block(d: Deal) -> str:
    """Format a single matching deal for a Discord message."""
    price_str = f"${d.total_price:.2f}" if d.total_price is not None else "?"
    unit_str = f"${d.unit_price:.3f}/oz" if d.unit_price is not None else "?/oz"

    # Add can-specific info if it's a canned deal
    can_info = ""
    if _is_canned(d.title):
        can_count = _extract_can_count(d.title)
        if can_count:
            ppc = d.total_price / can_count if d.total_price else 0
            can_info = f" | 🥫 {can_count} cans @ ${ppc:.2f}/can"

    return (
        f"**{d.title}**\n"
        f"💰 Total: {price_str} ({unit_str}) | {d.size or '?'}{can_info}\n"
        f"📦 Source: {d.source} | Shipping: {d.shipping or 'N/A'}\n"
        f"🔗 {d.url}"
    )


def _send_discord(matches: List[Deal]) -> None:
    """Post matching deals to Discord, chunking to respect the char limit."""
    if not matches:
        _log("No matching deals found today — staying quiet on Discord.")
        if HEARTBEAT and DISCORD_WEBHOOK_URL:
            try:
                requests.post(
                    DISCORD_WEBHOOK_URL,
                    json={"content": "☕ **DealFindr** — no cold-brew deals matched today."},
                    timeout=15,
                )
                _log("  Heartbeat sent to Discord.")
            except requests.RequestException as exc:
                _log(f"  Heartbeat POST failed: {exc}")
        return

    if not DISCORD_WEBHOOK_URL:
        _log("WARNING: DISCORD_WEBHOOK_URL is not set — cannot send alerts.")
        _log(f"Would have posted {len(matches)} deal(s):")
        for d in matches:
            _log(f"  - {d.title} | ${d.total_price:.2f} | {d.url}")
        return

    header = (
        f"☕ **DealFindr** found {len(matches)} cold-brew deal(s)!\n"
        f"Bottled: {MIN_SIZE_OZ:.0f}oz+ under ${MAX_PRICE:.2f} | "
        f"Canned: under ${MAX_PRICE_PER_CAN:.2f}/can\n\n"
    )
    chunks: List[str] = []
    current = header
    for d in matches:
        block = _format_deal_block(d) + "\n\n"
        if len(current) + len(block) > DISCORD_CHAR_LIMIT:
            chunks.append(current)
            current = block
        else:
            current += block
    if current.strip():
        chunks.append(current)

    _log(f"Sending {len(chunks)} Discord message(s) for {len(matches)} deal(s).")
    for i, chunk in enumerate(chunks, 1):
        payload = {"content": chunk}
        try:
            resp = requests.post(
                DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=15,
            )
            if resp.status_code in (200, 204):
                _log(f"  [{i}/{len(chunks)}] Sent.")
            else:
                _log(
                    f"  [{i}/{len(chunks)}] Discord returned "
                    f"{resp.status_code}: {resp.text[:200]}"
                )
        except requests.RequestException as exc:
            _log(f"  [{i}/{len(chunks)}] Discord POST failed: {exc}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def _search_all() -> List[Deal]:
    """Run all store searches — Walmart/Craigslist in parallel, Amazon/Target/Google sequentially."""
    queries = _build_queries()
    _log(f"Searching {len(queries)} query(ies): {queries}")

    all_deals: List[Deal] = []

    # Phase 1: Walmart + Craigslist + Flipp in parallel (thread-safe, no rate-limit issues).
    req_tasks: List[tuple] = []
    for q in queries:
        req_tasks.append((f"search_walmart({q!r})", lambda q=q: search_walmart(q, MAX_RESULTS)))
        req_tasks.append((f"search_craigslist({q!r})", lambda q=q: search_craigslist(q, cities=CL_CITIES, max_results=MAX_RESULTS)))
        req_tasks.append((f"search_flipp({q!r})", lambda q=q: search_flipp(q)))

    with ThreadPoolExecutor(max_workers=min(len(req_tasks), 8)) as pool:
        futures = {pool.submit(fn): label for label, fn in req_tasks}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results = fut.result()
                _log(f"  {label} -> {len(results)} raw deal(s)")
                all_deals.extend(results)
            except Exception as exc:  # noqa: BLE001
                _log(f"  {label} FAILED: {exc}")

    # Phase 2: Amazon sequentially with delays + coupon extraction.
    all_coupons: dict = {}
    for i, q in enumerate(queries):
        if i > 0:
            time.sleep(3)  # 3s gap between Amazon searches
        label = f"search_amazon({q!r})"
        try:
            deals, coupons = _search_amazon_with_coupons(q, MAX_RESULTS)
            _log(f"  {label} -> {len(deals)} raw deal(s), {len(coupons)} coupon(s)")
            all_deals.extend(deals)
            all_coupons.update(coupons)
        except Exception as exc:  # noqa: BLE001
            _log(f"  {label} FAILED: {exc}")

    # Apply coupons to Amazon deals (adjusts prices for S&S, annotates promos).
    if all_coupons:
        _log(f"Applying coupons: {len(all_coupons)} total")
        all_deals = _apply_coupons_to_deals(all_deals, all_coupons)

    # Phase 3: Playwright-based scrapers sequentially (not thread-safe).
    for q in queries:
        for label, fn in [
            (f"search_target_pw({q!r})", lambda q=q: search_target_playwright(q, MAX_RESULTS)),
            (f"search_google_pw({q!r})", lambda q=q: search_google_shopping_playwright(q, MAX_RESULTS)),
        ]:
            try:
                results = fn()
                _log(f"  {label} -> {len(results)} raw deal(s)")
                all_deals.extend(results)
            except Exception as exc:  # noqa: BLE001
                _log(f"  {label} FAILED: {exc}")

    return all_deals


def _dedup(deals: List[Deal]) -> List[Deal]:
    """De-duplicate deals by normalized URL (Amazon + Walmart may overlap on queries)."""
    seen = set()
    unique: List[Deal] = []
    for d in deals:
        key = _normalize_url(d.url) if d.url else d.title.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(d)
    return unique


def _normalize_url(url: str) -> str:
    """Normalize a product URL for dedup — strips tracking params.

    Walmart: strip query string, keep path (e.g. /ip/.../12345)
    Amazon: strip /ref=... and query params, keep /dp/ASIN path
    """
    # Strip query params for Walmart
    if "walmart.com" in url:
        return url.split("?")[0]
    # Strip /ref= tracking for Amazon
    if "amazon.com" in url:
        return re.sub(r"/ref=.*", "", url.split("?")[0])
    return url


def main() -> None:
    _log("=" * 60)
    _log("DealFindr cron starting")
    _log(f"  Query:    {QUERY}")
    _log(f"  Brands:   {BRANDS}")
    _log(f"  Min oz:   {MIN_SIZE_OZ}")
    _log(f"  Max $:    {MAX_PRICE} (bottled) / ${MAX_PRICE_PER_CAN}/can (canned)")
    _log(f"  Max res:  {MAX_RESULTS}/source")
    _log("=" * 60)

    try:
        raw_deals = _search_all()
        _log(f"Total raw deals: {len(raw_deals)}")

        # Post-process size/unit_oz (replicate dealfindr.py main() lines 1761-1763).
        for d in raw_deals:
            _post_process(d)

        unique = _dedup(raw_deals)
        _log(f"After dedup: {len(unique)}")

        # For brand-matched Amazon deals, check product pages for real prices
        # and hidden discounts. Search page prices are often stale/rounded.
        for d in unique:
            if d.source != "Amazon" or d.price is None:
                continue
            if not _matches_brand(d.title):
                continue
            if d.unit_oz is None or d.unit_oz < MIN_SIZE_OZ:
                continue

            try:
                real_price = _check_amazon_product_discount(d)
                if real_price and real_price != d.price:
                    _log(f"  Price update: {d.title[:60]} ${d.price:.2f} -> ${real_price:.2f}")
                    d.price = real_price
            except Exception as exc:
                _log(f"  [amazon] discount check failed for {d.title[:60]}: {exc}")

        # Filter.
        matches = [d for d in unique if _filter(d)]
        _log(f"Matches (brand + size + price): {len(matches)}")

        # Sort by unit price ascending (best deal first).
        # For canned deals without unit_oz, sort by price_per_can.
        def _sort_key(d: Deal) -> float:
            if d.unit_price is not None:
                return d.unit_price
            can_count = _extract_can_count(d.title)
            if can_count and d.total_price:
                return d.total_price / can_count
            return float("inf")
        matches.sort(key=_sort_key)

        # Emit a JSON summary for log scraping / debugging.
        summary = [
            {
                "title": d.title,
                "price": d.price,
                "shipping": d.shipping,
                "total_price": d.total_price,
                "unit_oz": d.unit_oz,
                "unit_price": d.unit_price,
                "source": d.source,
                "url": d.url,
                "size": d.size,
            }
            for d in matches
        ]
        _log("MATCHES_JSON=" + json.dumps(summary))

        _send_discord(matches)
        _log("DealFindr cron complete.")

    except Exception:
        _log("FATAL: unhandled exception:")
        _log(traceback.format_exc())
        sys.exit(1)
    finally:
        _close_browser()


if __name__ == "__main__":
    main()