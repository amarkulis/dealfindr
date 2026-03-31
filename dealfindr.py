#!/usr/bin/env python3
"""
DealFindr — Find the best prices across multiple shopping platforms.

Searches eBay, Craigslist, Amazon, Walmart, Best Buy, and Google Shopping
locally without any API keys. Results are sorted cheapest → most expensive.
"""

import argparse
import json
import re
import time
import random
import sys
import csv
import io
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote_plus, unquote

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

console = Console()

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


_SESSION = requests.Session()


def _get(url: str, timeout: int = 14, **kwargs) -> Optional[requests.Response]:
    """GET with random UA and graceful failure."""
    try:
        headers = kwargs.pop("headers", _headers())
        resp = _SESSION.get(url, headers=headers, timeout=timeout, **kwargs)
        if resp.status_code == 200:
            return resp
    except Exception:
        pass
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

    @property
    def total_price(self) -> float:
        return self.price + (self.shipping or 0.0)


# ──────────────────────────────────────────────────────────────────────────────
# eBay scraper
# ──────────────────────────────────────────────────────────────────────────────

def search_ebay(query: str, max_results: int = 20) -> List[Deal]:
    """Buy-It-Now listings sorted by price + shipping (lowest first)."""
    deals: List[Deal] = []
    url = (
        f"https://www.ebay.com/sch/i.html"
        f"?_nkw={quote_plus(query)}&_sop=15&LH_BIN=1&_ipg=50"
    )
    resp = _get(url)
    if not resp:
        return deals

    soup = BeautifulSoup(resp.text, "html.parser")

    for item in soup.select(".s-item"):
        if len(deals) >= max_results:
            break
        try:
            title_el = item.select_one(".s-item__title")
            price_el  = item.select_one(".s-item__price")
            link_el   = item.select_one("a.s-item__link")
            ship_el   = item.select_one(".s-item__shipping, .s-item__freeXDays")
            cond_el   = item.select_one(".SECONDARY_INFO")

            if not (title_el and price_el and link_el):
                continue

            title = title_el.get_text(strip=True)
            if title in ("Shop on eBay", ""):
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
            link = re.sub(r"[?&](?:hash|_trkparms|_trksid)[^&]*", "", link).rstrip("?&")

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


def search_craigslist(
    query: str,
    cities: Optional[List[str]] = None,
    max_results: int = 20,
) -> List[Deal]:
    """Craigslist for-sale search via public RSS feeds."""
    cities = cities or _CL_CITIES
    deals: List[Deal] = []

    for city in cities[:8]:
        if len(deals) >= max_results:
            break
        try:
            url = (
                f"https://{city}.craigslist.org/search/sss"
                f"?query={quote_plus(query)}&sort=date&format=rss"
            )
            resp = _get(url, timeout=10)
            if not resp:
                continue

            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:8]:
                try:
                    title = (item.findtext("title") or "").strip()
                    link  = (item.findtext("link")  or "").strip()
                    desc  = item.findtext("description") or ""

                    # Price must appear somewhere
                    pm = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", title + " " + desc)
                    if not pm:
                        continue

                    price = _parse_price(pm.group())
                    if not price or price <= 0:
                        continue

                    deals.append(
                        Deal(title[:90], price, link, "Craigslist",
                             "Used", None, city.title())
                    )
                except Exception:
                    continue

            time.sleep(0.25)
        except Exception:
            continue

    return deals[:max_results]


# ──────────────────────────────────────────────────────────────────────────────
# Amazon scraper
# ──────────────────────────────────────────────────────────────────────────────

def search_amazon(query: str, max_results: int = 20) -> List[Deal]:
    """Amazon search sorted by price ascending."""
    deals: List[Deal] = []
    url = f"https://www.amazon.com/s?k={quote_plus(query)}&s=price-asc-rank"
    resp = _get(url, headers=_headers("https://www.amazon.com/"), timeout=16)
    if not resp:
        return deals

    # Detect robot/captcha page
    if "robot" in resp.text.lower() or "captcha" in resp.text.lower():
        return deals

    soup = BeautifulSoup(resp.text, "html.parser")

    for result in soup.select('[data-component-type="s-search-result"]'):
        if len(deals) >= max_results:
            break
        try:
            title_el  = result.select_one("h2 .a-text-normal, h2 a span")
            link_el   = result.select_one("h2 a")
            whole     = result.select_one(".a-price-whole")
            frac      = result.select_one(".a-price-fraction")

            if not (title_el and whole):
                continue

            title = title_el.get_text(strip=True)
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

            deals.append(Deal(title[:90], price, link, "Amazon", "New", shipping))
        except Exception:
            continue

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
# Output
# ──────────────────────────────────────────────────────────────────────────────

_RANK = {1: "🥇", 2: "🥈", 3: "🥉"}
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
    unique.sort(key=lambda d: d.total_price)
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
    table.add_column("Source",   min_width=16, style="cyan")
    table.add_column("Cond.",    width=13)
    table.add_column("Price",    min_width=10, justify="right", style="bold green")
    table.add_column("Shipping", min_width=12, justify="right")
    table.add_column("Title",    min_width=38, style="white")

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

        table.add_row(rank, d.source, cond_str, price_str, ship_str, d.title)

    console.print(table)

    # ── Clickable purchase links ──────────────────────────────────────────────
    console.print()
    console.print("[bold cyan]Purchase Links — cheapest → most expensive:[/bold cyan]")
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
    writer.writerow(["Rank", "Source", "Condition", "Price", "Shipping", "Total", "Title", "URL"])
    for i, d in enumerate(unique, 1):
        writer.writerow([
            i, d.source, d.condition,
            f"{d.price:.2f}",
            f"{d.shipping:.2f}" if d.shipping is not None else "",
            f"{d.total_price:.2f}",
            d.title, d.url,
        ])
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _build_scrapers(
    query: str, args: argparse.Namespace
) -> List[Tuple[str, Callable]]:
    scrapers: List[Tuple[str, Callable]] = []
    m = args.max_results
    c = args.cities or None

    if not args.no_ebay:
        scrapers.append(("eBay",            lambda q=query, n=m: search_ebay(q, n)))
    if not args.no_craigslist:
        scrapers.append(("Craigslist",      lambda q=query, cities=c, n=m: search_craigslist(q, cities, n)))
    if not args.no_amazon:
        scrapers.append(("Amazon",          lambda q=query, n=m: search_amazon(q, n)))
    if not args.no_walmart:
        scrapers.append(("Walmart",         lambda q=query, n=m: search_walmart(q, n)))
    if not args.no_bestbuy:
        scrapers.append(("Best Buy",        lambda q=query, n=m: search_bestbuy(q, n)))
    if not args.no_google:
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
  python dealfindr.py macbook pro --no-craigslist --max-results 10
  python dealfindr.py "nintendo switch" --cities losangeles sfbay chicago
  python dealfindr.py "iphone 15" --export results.csv
        """,
    )
    parser.add_argument("query",           nargs="+", help="Item to search for")
    parser.add_argument("--no-ebay",       action="store_true", help="Skip eBay")
    parser.add_argument("--no-amazon",     action="store_true", help="Skip Amazon")
    parser.add_argument("--no-craigslist", action="store_true", help="Skip Craigslist")
    parser.add_argument("--no-walmart",    action="store_true", help="Skip Walmart")
    parser.add_argument("--no-bestbuy",    action="store_true", help="Skip Best Buy")
    parser.add_argument("--no-google",     action="store_true", help="Skip Google Shopping")
    parser.add_argument(
        "--cities", nargs="+", metavar="CITY",
        help="Craigslist city slugs to search (e.g. losangeles sfbay newyork)",
    )
    parser.add_argument(
        "--max-results", type=int, default=20, metavar="N",
        help="Max results per source (default: 20)",
    )
    parser.add_argument(
        "--export", metavar="FILE",
        help="Export results to a CSV file",
    )

    args = parser.parse_args()
    query = " ".join(args.query)

    console.print()
    console.print(
        Panel(
            f"[bold cyan]DealFindr[/bold cyan]  [dim]|[/dim]  "
            f"Searching for [bold yellow]{query}[/bold yellow]\n"
            "[dim]Scraping eBay · Craigslist · Amazon · Walmart · Best Buy · Google Shopping[/dim]",
            border_style="cyan",
        )
    )
    console.print()

    scrapers = _build_scrapers(query, args)
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

        with ThreadPoolExecutor(max_workers=6) as executor:
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
                except Exception:
                    progress.update(
                        task_map[name],
                        description=f"  [red]✗[/red] {name} [dim](failed)[/dim]",
                    )

    display_results(all_deals, query)

    if args.export:
        csv_data = export_csv(all_deals, query)
        with open(args.export, "w", newline="", encoding="utf-8") as f:
            f.write(csv_data)
        console.print(f"[green]✓[/green] Results exported to [bold]{args.export}[/bold]\n")


if __name__ == "__main__":
    main()
