#!/usr/bin/env python3
"""
router_dealfindr.py — Daily deal monitor for ASUS ZenWiFi BT10 router.

Searches eBay (via Bing search snippets + eBay product page) and Amazon
for ASUS ZenWiFi BT10 listings under $200. Posts matches to Discord.

Strategy:
  - eBay blocks direct scraping from datacenter IPs, but:
    1. Bing indexes eBay listings with price snippets → use Bing search
    2. eBay product pages (ebay.com/p/) are accessible → extract JSON-LD prices
  - Amazon works with standard requests.

Runs as a Kubernetes CronJob on the TC cluster.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from typing import List, Optional
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

# curl_cffi for TLS fingerprint impersonation (bypasses Cloudflare/bot detection).
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

# Playwright is optional — only needed for eBay search (bypasses bot detection).
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

QUERY = os.getenv("ROUTER_QUERY", "ASUS ZenWiFi BT10").strip()
MAX_PRICE = float(os.getenv("ROUTER_MAX_PRICE", "270"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# eBay product page ID for ASUS ZenWiFi BT10.
_EBAY_PRODUCT_ID = "28072548206"
_EBAY_PRODUCT_URL = f"https://www.ebay.com/p/{_EBAY_PRODUCT_ID}"
# Alternative regional eBay product pages (sometimes less aggressive bot detection).
_EBAY_PRODUCT_URLS = [
    _EBAY_PRODUCT_URL,
    f"https://www.ebay.ca/p/{_EBAY_PRODUCT_ID}",
    f"https://www.ebay.co.uk/p/{_EBAY_PRODUCT_ID}",
]

# Headers that mimic a real Chrome browser (needed for eBay product page).
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_AMZN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class RouterDeal:
    title: str
    price: float
    url: str
    source: str
    condition: str = "?"
    shipping: Optional[float] = None


# --------------------------------------------------------------------------- #
# eBay via Bing search
# --------------------------------------------------------------------------- #

_PRICE_RE = re.compile(r'\$(\d+(?:\.\d{2})?)')
_COND_RE = re.compile(
    r'(Brand New|Used|Refurbished|Open Box|Pre-Owned|New|Like New|Seller Refurbished)',
    re.IGNORECASE,
)


def _search_ebay_bing(query: str) -> List[RouterDeal]:
    """Search eBay for ASUS BT10 via Bing.

    Bing indexes eBay listings and shows price snippets. We extract those.
    Bing wraps result URLs in redirects; we use the redirect URL as the link
    since base64 decoding is unreliable across different link types.
    """
    deals: List[RouterDeal] = []
    search_query = f"{query} site:ebay.com"

    try:
        r = requests.get(
            "https://www.bing.com/search",
            params={"q": search_query},
            headers=_HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            _log(f"  Bing search returned {r.status_code}")
            return deals
    except Exception as exc:
        _log(f"  Bing search error: {exc}")
        return deals

    soup = BeautifulSoup(r.text, "html.parser")
    results = soup.select(".b_algo")

    for res in results:
        try:
            title_el = res.select_one("h2 a")
            snippet_el = res.select_one(".b_caption p")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            href = title_el.get("href", "")

            # Skip category pages (no prices in snippet).
            prices = _PRICE_RE.findall(snippet)
            if not prices:
                continue

            # User wants only ASUS BT10 listings; reject other ASUS router families.
            combined = (title + " " + snippet).lower()
            if "bt10" not in combined:
                continue

            price = float(prices[0])

            # Extract condition.
            cond_match = _COND_RE.search(snippet)
            condition = cond_match.group(1) if cond_match else "?"

            # Use the Bing redirect URL as the link (it redirects to eBay).
            url = href

            deals.append(RouterDeal(
                title=title[:120],
                price=price,
                url=url,
                source="eBay",
                condition=condition,
            ))
        except Exception:
            continue

    return deals


# --------------------------------------------------------------------------- #
# eBay via Brave Search
# --------------------------------------------------------------------------- #

def _search_ebay_brave(query: str) -> List[RouterDeal]:
    """Search eBay for ASUS BT10 via Brave Search.

    Brave Search indexes far more eBay listings than Bing (141 vs 1 URLs).
    Uses curl_cffi for TLS fingerprint impersonation.
    """
    deals: List[RouterDeal] = []
    if not _HAS_CURL_CFFI:
        _log("  curl_cffi not available — skipping Brave Search")
        return deals

    search_query = f"{query} site:ebay.com"
    try:
        r = cffi_requests.get(
            "https://search.brave.com/search",
            params={"q": search_query},
            impersonate="chrome120",
            timeout=15,
        )
        if r.status_code != 200:
            _log(f"  Brave Search returned {r.status_code}")
            return deals
    except Exception as exc:
        _log(f"  Brave Search error: {exc}")
        return deals

    soup = BeautifulSoup(r.text, "html.parser")
    seen_urls: set = set()

    for el in soup.select("div.snippet"):
        try:
            text = el.get_text(strip=True)
            if "$" not in text:
                continue

            prices = _PRICE_RE.findall(text)
            if not prices:
                continue

            combined = text.lower()
            if "bt10" not in combined:
                continue

            # Skip accessories.
            if any(w in combined for w in ["mount", "wall", "case", "cover", "adapter"]):
                continue

            price = float(prices[0])

            # Extract condition.
            cond_match = _COND_RE.search(text)
            condition = cond_match.group(1) if cond_match else "?"

            # Extract eBay item URL from the snippet.
            url_match = re.search(r'(https?://(?:www\.)?ebay\.com/itm/\d+)', text)
            if not url_match:
                url_match = re.search(r'(ebay\.com[^\s]+)', text)
            url = url_match.group(1) if url_match else ""
            if url.startswith("ebay.com"):
                url = f"https://www.{url}"

            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Extract a clean title from the snippet.
            title = text[:120]
            # Try to get just the product name before "| eBay".
            title_match = re.search(r'^(.+?)(?:\s*\|\s*eBay|\s*ebay\.com)', text, re.IGNORECASE)
            if title_match:
                title = title_match.group(1).strip()

            deals.append(RouterDeal(
                title=title[:120],
                price=price,
                url=url,
                source="eBay",
                condition=condition,
            ))
        except Exception:
            continue

    return deals


# --------------------------------------------------------------------------- #
# eBay via Playwright (headless browser)
# --------------------------------------------------------------------------- #

def _search_ebay_playwright(query: str) -> List[RouterDeal]:
    """Search eBay with Playwright to bypass bot detection.

    eBay blocks datacenter IPs for direct requests, but a headless browser
    with JavaScript rendering gets through. Sorted by lowest price + shipping.
    """
    deals: List[RouterDeal] = []
    if not _HAS_PLAYWRIGHT:
        _log("  Playwright not available — skipping eBay browser search")
        return deals

    search_url = (
        f"https://www.ebay.com/sch/i.html?_nkw={requests.utils.quote(query)}"
        f"&LH_BIN=1&_sop=15"  # Buy It Now, sorted by price+shipping lowest
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()

            # Navigate directly to search — no homepage visit.
            page.goto(search_url, timeout=30000, wait_until="domcontentloaded")

            # Immediately extract content before any JS redirect.
            content = page.content()
            title = page.title()
            url = page.url
            _log(f"  Playwright loaded: title='{title[:80]}' url='{url[:100]}'")
            _log(f"  Page length: {len(content)}")

            context.close()
            browser.close()

            # If we got a challenge page, bail.
            if "challenge" in url.lower() or "interruption" in title.lower() or "error page" in title.lower():
                _log("  Got challenge/error page — skipping")
                return deals

            # Regex extraction from HTML content.
            # Find all /itm/ links with nearby prices.
            itm_pattern = re.compile(
                r'<a[^>]*href="(https?://(?:www\.)?ebay\.com/itm/\d+[^"]*)"[^>]*>'
                r'((?:(?!</a>).)*)</a>',
                re.DOTALL | re.IGNORECASE,
            )
            seen_urls = set()
            for match in itm_pattern.finditer(content):
                deal_url = match.group(1)
                link_text = re.sub(r'<[^>]+>', ' ', match.group(2)).strip()
                link_text = re.sub(r'\s+', ' ', link_text)

                # Clean HTML entities.
                link_text = link_text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                link_text = link_text.replace('&quot;', '"').replace('&#39;', "'")

                if not link_text or len(link_text) < 5:
                    continue
                if deal_url in seen_urls:
                    continue
                seen_urls.add(deal_url)

                # User wants only ASUS BT10 listings; reject other ASUS router families.
                combined = (link_text + " " + deal_url).lower()
                if "bt10" not in combined:
                    continue

                # Extract price from link text.
                prices = _PRICE_RE.findall(link_text)
                if not prices:
                    continue
                price = float(prices[0])

                # Extract condition.
                cond_match = _COND_RE.search(link_text)
                condition = cond_match.group(1) if cond_match else "?"

                deals.append(RouterDeal(
                    title=link_text[:120],
                    price=price,
                    url=deal_url,
                    source="eBay",
                    condition=condition,
                ))

            _log(f"  Regex extracted {len(deals)} deals from page content")
    except Exception as exc:
        _log(f"  eBay Playwright error: {exc}")

    return deals


# --------------------------------------------------------------------------- #
# eBay product page (JSON-LD)
# --------------------------------------------------------------------------- #

def _search_ebay_product() -> List[RouterDeal]:
    """Scrape the eBay product page for ASUS ZenWiFi BT10 listings.

    Tries multiple regional eBay product pages (US, CA, UK) with curl_cffi
    TLS fingerprint impersonation to bypass bot detection.
    """
    deals: List[RouterDeal] = []
    seen_urls: set = set()

    for product_url in _EBAY_PRODUCT_URLS:
        try:
            if _HAS_CURL_CFFI:
                r = cffi_requests.get(
                    product_url,
                    headers=_HEADERS,
                    timeout=15,
                    impersonate="chrome120",
                )
            else:
                r = requests.get(product_url, headers=_HEADERS, timeout=15)
            if r.status_code != 200:
                _log(f"  eBay product ({product_url.split('/')[2]}): {r.status_code}")
                continue
            _log(f"  eBay product ({product_url.split('/')[2]}): {r.status_code}, {len(r.text)} bytes")
        except Exception as exc:
            _log(f"  eBay product ({product_url.split('/')[2]}) error: {exc}")
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # Extract from JSON-LD — handles deeply nested offer structures.
        jsonld_scripts = soup.select('script[type="application/ld+json"]')
        _log(f"  JSON-LD scripts found: {len(jsonld_scripts)}")
        for script in jsonld_scripts:
            try:
                data = json.loads(script.string)

                # Capture the full product name from the top-level WebPage.
                product_name = data.get("name", "ASUS ZenWiFi BT10")

                def _extract_offers(obj, depth=0, name=None):
                    """Recursively extract offers from nested JSON-LD."""
                    if depth > 10 or obj is None:
                        return
                    if isinstance(obj, dict):
                        # Track the product name as we descend (itemOffered.name).
                        if obj.get("name"):
                            name = obj.get("name")
                        # Direct offer with price.
                        price = obj.get("price")
                        if price is not None:
                            try:
                                price_val = float(price)
                            except (ValueError, TypeError):
                                price_val = None
                            if price_val and price_val > 0:
                                # Convert CAD to USD if needed.
                                currency = obj.get("priceCurrency", "")
                                if currency == "CAD":
                                    price_val = round(price_val * 0.72, 2)
                                elif currency == "GBP":
                                    price_val = round(price_val * 1.27, 2)
                                title = name or product_name or "ASUS ZenWiFi BT10"
                                cond = obj.get("itemCondition", "?")
                                if isinstance(cond, str) and "/" in cond:
                                    cond = cond.split("/")[-1]
                                # Normalize "NewCondition" -> "New", "UsedCondition" -> "Used".
                                if isinstance(cond, str) and cond.endswith("Condition"):
                                    cond = cond[:-len("Condition")]
                                url = obj.get("url", product_url)

                                # Build a clean /itm/ listing link from the iid param.
                                iid_match = re.search(r'[?&]iid=(\d+)', str(url))
                                if iid_match:
                                    url = f"https://www.ebay.com/itm/{iid_match.group(1)}"

                                # Extract shipping cost from shippingDetails.
                                shipping = None
                                sd = obj.get("shippingDetails") or {}
                                rate = sd.get("shippingRate") or {}
                                try:
                                    shipping = float(rate.get("value", 0) or 0)
                                except (ValueError, TypeError):
                                    shipping = None

                                if url not in seen_urls:
                                    seen_urls.add(url)
                                    deals.append(RouterDeal(
                                        title=str(title)[:120],
                                        price=price_val,
                                        url=str(url),
                                        source="eBay",
                                        condition=str(cond),
                                        shipping=shipping,
                                    ))
                        # Recurse into nested structures.
                        for key in ("offers", "itemOffered", "mainEntity", "item"):
                            val = obj.get(key)
                            if val is not None:
                                _extract_offers(val, depth + 1, name)
                    elif isinstance(obj, list):
                        for item in obj:
                            _extract_offers(item, depth + 1, name)

                _extract_offers(data)
            except Exception:
                continue

        # Also extract prices from HTML price elements (catches more listings).
        for price_el in soup.select('[class*="price"]'):
            try:
                text = price_el.get_text(strip=True)
                prices = _PRICE_RE.findall(text)
                if not prices:
                    continue
                price = float(prices[0])
                if price < 20 or price > 2000:
                    continue

                # Find the parent listing container to get title/URL.
                parent = price_el
                title = ""
                url = ""
                for _ in range(8):
                    parent = parent.parent
                    if parent is None:
                        break
                    parent_text = parent.get_text(strip=True)
                    # Look for a link in the parent.
                    link = parent.select_one('a[href*="/itm/"]')
                    if link:
                        url = link.get("href", "")
                        title = link.get_text(strip=True)
                        if not title:
                            title = parent_text[:120]
                        break

                if not title:
                    continue

                # User wants only ASUS BT10 listings; reject other ASUS router families.
                combined = (title + " " + text).lower()
                if "bt10" not in combined:
                    continue

                # Skip accessories.
                if any(w in combined for w in ["mount", "wall", "case", "cover", "adapter"]):
                    continue

                # Extract condition.
                cond_match = _COND_RE.search(title)
                condition = cond_match.group(1) if cond_match else "?"

                if url in seen_urls:
                    continue
                seen_urls.add(url)

                deals.append(RouterDeal(
                    title=title[:120],
                    price=price,
                    url=url,
                    source="eBay",
                    condition=condition,
                ))
            except Exception:
                continue

        # If we got deals from this URL, stop trying others.
        if deals:
            break

    return deals


# --------------------------------------------------------------------------- #
# Amazon
# --------------------------------------------------------------------------- #

def _search_amazon(query: str) -> List[RouterDeal]:
    """Search Amazon for ASUS BT10 (new + used)."""
    deals: List[RouterDeal] = []
    seen_urls: set = set()

    for condition, cond_label in [("", "New"), ("&condition=used", "Used")]:
        url = f"https://www.amazon.com/s?k={requests.utils.quote(query)}{condition}"
        try:
            r = requests.get(url, headers=_AMZN_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
        except Exception:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        results = soup.select('[data-component-type="s-search-result"]')

        for res in results:
            try:
                img = res.select_one("img[alt]")
                title = (img.get("alt") or "").strip() if img else ""
                if title.lower().startswith("sponsored ad - "):
                    title = title[15:].strip()
                if not title:
                    continue

                whole = res.select_one(".a-price-whole")
                frac = res.select_one(".a-price-fraction")
                if not whole:
                    continue
                price_str = whole.get_text(strip=True).replace(",", "")
                if frac:
                    price_str += "." + frac.get_text(strip=True)
                try:
                    price = float(price_str)
                except ValueError:
                    continue

                link_el = res.select_one("a.a-link-normal.s-no-outline, h2 a")
                href = link_el.get("href", "") if link_el else ""
                if href.startswith("/"):
                    link = f"https://www.amazon.com{href}"
                elif href.startswith("http"):
                    link = href
                else:
                    continue
                link = re.sub(r"/ref=.*", "", link)

                if link in seen_urls:
                    continue
                seen_urls.add(link)

                deals.append(RouterDeal(
                    title=title[:120],
                    price=price,
                    url=link,
                    source="Amazon",
                    condition=cond_label,
                ))
            except Exception:
                continue

    return deals


# --------------------------------------------------------------------------- #
# Newegg
# --------------------------------------------------------------------------- #

def _search_newegg(query: str) -> List[RouterDeal]:
    """Search Newegg for ASUS BT10.

    Newegg is accessible from datacenter IPs and has BT10 listings.
    Uses curl_cffi for TLS fingerprint impersonation to avoid blocking.
    """
    deals: List[RouterDeal] = []
    if not _HAS_CURL_CFFI:
        _log("  curl_cffi not available — skipping Newegg")
        return deals

    url = f"https://www.newegg.com/p/pl?d={requests.utils.quote(query)}"
    try:
        r = cffi_requests.get(url, impersonate="chrome120", timeout=15)
        if r.status_code != 200:
            _log(f"  Newegg returned {r.status_code}")
            return deals
    except Exception as exc:
        _log(f"  Newegg error: {exc}")
        return deals

    soup = BeautifulSoup(r.text, "html.parser")

    for item in soup.select(".item-cell"):
        try:
            title_el = item.select_one(".item-title")
            price_el = item.select_one(".price-current")
            link_el = item.select_one("a.item-title")

            if not title_el or not price_el:
                continue

            title = title_el.get_text(strip=True)
            price_text = price_el.get_text(strip=True)
            href = link_el.get("href", "") if link_el else ""

            # User wants only ASUS BT10 listings; reject other ASUS router families.
            combined = (title + " " + price_text).lower()
            if "bt10" not in combined:
                continue

            # Extract price.
            prices = _PRICE_RE.findall(price_text)
            if not prices:
                continue
            price = float(prices[0])

            # Extract condition from title.
            cond_match = _COND_RE.search(title)
            condition = cond_match.group(1) if cond_match else "New"

            deals.append(RouterDeal(
                title=title[:120],
                price=price,
                url=href,
                source="Newegg",
                condition=condition,
            ))
        except Exception:
            continue

    return deals


# --------------------------------------------------------------------------- #
# B&H Photo
# --------------------------------------------------------------------------- #

def _search_bhphoto(query: str) -> List[RouterDeal]:
    """Search B&H Photo for ASUS BT10.

    B&H is accessible from datacenter IPs and has BT10 listings.
    Uses curl_cffi for TLS fingerprint impersonation.
    """
    deals: List[RouterDeal] = []
    if not _HAS_CURL_CFFI:
        _log("  curl_cffi not available — skipping B&H Photo")
        return deals

    url = f"https://www.bhphotovideo.com/c/search?q={requests.utils.quote(query)}"
    try:
        r = cffi_requests.get(url, impersonate="chrome120", timeout=15)
        if r.status_code != 200:
            _log(f"  B&H returned {r.status_code}")
            return deals
    except Exception as exc:
        _log(f"  B&H error: {exc}")
        return deals

    soup = BeautifulSoup(r.text, "html.parser")
    seen_urls: set = set()

    # B&H product containers use data-selenium attributes with numeric IDs.
    # We look for product links and their parent containers.
    for link in soup.select('a[href*="/c/product/"]'):
        try:
            href = link.get("href", "")
            # Skip review links, only keep product pages.
            if "/reviews" in href or href in seen_urls:
                continue

            # Get title from the link text — skip empty links first.
            title = link.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            seen_urls.add(href)

            # User wants only ASUS BT10 listings; reject other ASUS router families.
            if "bt10" not in title.lower():
                continue

            # Find the parent container that has the price element.
            parent = link
            for _ in range(5):
                parent = parent.parent
                if parent is None:
                    break
                if parent.select_one('[class*="price_x"]'):
                    break

            if parent is None:
                continue

            # Extract price from the specific price element.
            price_el = parent.select_one('[class*="price_x"]')
            if not price_el:
                continue
            price_text = price_el.get_text(strip=True)

            # B&H shows prices like "$57999" (cents) or "$689.99".
            prices = _PRICE_RE.findall(price_text)
            if not prices:
                continue
            price = float(prices[0])
            # If price looks like cents (e.g., 57999), divide by 100.
            if price > 1000 and "." not in price_text:
                price = price / 100

            # Extract condition.
            cond_match = _COND_RE.search(title)
            condition = cond_match.group(1) if cond_match else "New"

            # Build full URL.
            full_url = href
            if href.startswith("/"):
                full_url = f"https://www.bhphotovideo.com{href}"

            deals.append(RouterDeal(
                title=title[:120],
                price=price,
                url=full_url,
                source="B&H Photo",
                condition=condition,
            ))
        except Exception:
            continue

    return deals

    return deals


# --------------------------------------------------------------------------- #
# Filtering & Discord
# --------------------------------------------------------------------------- #

def _log(msg: str) -> None:
    """Print with flush for K8s log capture."""
    print(msg, flush=True)


def _is_bt10_title(title: str) -> bool:
    """True only for ASUS BT10 router titles."""
    title_lower = (title or "").lower()
    if not title_lower:
        return False
    return "bt10" in title_lower


def _filter(deals: List[RouterDeal]) -> List[RouterDeal]:
    """Filter deals: must be under MAX_PRICE and relevant to ASUS BT10."""
    matches = []
    for d in deals:
        # Skip obviously fake prices (BT10 routers don't sell for under $20).
        if d.price < 20:
            continue
        if d.price >= MAX_PRICE:
            continue
        title_lower = d.title.lower()
        if not _is_bt10_title(title_lower):
            continue
        exclude = ["case", "cover", "skin", "screen protector", "mount", "stand", "adapter"]
        if any(w in title_lower for w in exclude):
            continue
        matches.append(d)
    return matches


def _format_deal(d: RouterDeal) -> str:
    """Format a deal for Discord."""
    shipping = ""
    if d.shipping is not None and d.shipping > 0:
        shipping = f" + ${d.shipping:.2f} shipping"
    elif d.shipping is not None and d.shipping == 0:
        shipping = " (free shipping)"
    return (
        f"**{d.title}**\n"
        f"💰 ${d.price:.2f}{shipping} | Condition: {d.condition} | Source: {d.source}\n"
        f"🔗 {d.url}"
    )


def _send_discord(matches: List[RouterDeal]) -> None:
    """Post matching deals to Discord."""
    if not matches:
        _log("No ASUS BT10 deals under ${:.0f} today.".format(MAX_PRICE))
        return

    if not DISCORD_WEBHOOK_URL:
        _log("WARNING: DISCORD_WEBHOOK_URL not set — cannot send alerts.")
        for d in matches:
            _log(f"  {d.title} | ${d.price:.2f} | {d.url}")
        return

    header = f"🔧 **ASUS ZenWiFi BT10** — {len(matches)} deal(s) under ${MAX_PRICE:.0f}!\n\n"
    blocks = []
    current = header
    for d in matches:
        block = _format_deal(d) + "\n\n"
        if len(current) + len(block) > 1900:
            blocks.append(current)
            current = block
        else:
            current += block
    blocks.append(current)

    for i, chunk in enumerate(blocks, 1):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=15)
            if resp.status_code in (200, 204):
                _log(f"  [{i}/{len(blocks)}] Sent to Discord.")
            else:
                _log(f"  [{i}/{len(blocks)}] Discord {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            _log(f"  [{i}/{len(blocks)}] Discord error: {exc}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    _log("=" * 60)
    _log("Router DealFindr — ASUS ZenWiFi BT10")
    _log(f"  Query:     {QUERY}")
    _log(f"  Max price: ${MAX_PRICE:.0f}")
    _log("=" * 60)

    try:
        all_deals: List[RouterDeal] = []

        # eBay via Bing search snippets.
        _log("Searching eBay (via Bing)...")
        ebay_bing = _search_ebay_bing(QUERY)
        _log(f"  eBay/Bing: {len(ebay_bing)} raw deal(s)")
        all_deals.extend(ebay_bing)

        # eBay via Brave Search (finds far more listings than Bing).
        _log("Searching eBay (via Brave)...")
        ebay_brave = _search_ebay_brave(QUERY)
        _log(f"  eBay/Brave: {len(ebay_brave)} raw deal(s)")
        all_deals.extend(ebay_brave)

        # eBay via Playwright (headless browser — bypasses bot detection).
        _log("Searching eBay (via Playwright)...")
        ebay_pw = _search_ebay_playwright(QUERY)
        _log(f"  eBay/Playwright: {len(ebay_pw)} raw deal(s)")
        all_deals.extend(ebay_pw)

        # eBay product page (JSON-LD).
        _log("Searching eBay product page...")
        ebay_product = _search_ebay_product()
        _log(f"  eBay product: {len(ebay_product)} raw deal(s)")
        all_deals.extend(ebay_product)

        # Amazon.
        _log("Searching Amazon...")
        amzn_deals = _search_amazon(QUERY)
        _log(f"  Amazon: {len(amzn_deals)} raw deal(s)")
        all_deals.extend(amzn_deals)

        # Newegg.
        _log("Searching Newegg...")
        newegg_deals = _search_newegg(QUERY)
        _log(f"  Newegg: {len(newegg_deals)} raw deal(s)")
        all_deals.extend(newegg_deals)

        # B&H Photo.
        _log("Searching B&H Photo...")
        bh_deals = _search_bhphoto(QUERY)
        _log(f"  B&H Photo: {len(bh_deals)} raw deal(s)")
        all_deals.extend(bh_deals)

        # Dedup by URL.
        seen = set()
        unique = []
        for d in all_deals:
            key = d.url
            if key in seen:
                continue
            seen.add(key)
            unique.append(d)
        _log(f"After dedup: {len(unique)}")

        # Filter.
        matches = _filter(unique)
        matches.sort(key=lambda d: d.price)

        _log(f"Matches (under ${MAX_PRICE:.0f}): {len(matches)}")
        summary = [
            {
                "title": d.title,
                "price": d.price,
                "source": d.source,
                "condition": d.condition,
                "url": d.url,
            }
            for d in matches
        ]
        _log("MATCHES_JSON=" + json.dumps(summary))

        _send_discord(matches)
        _log("Router DealFindr complete.")

    except Exception:
        _log("FATAL: " + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
