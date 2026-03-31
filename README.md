<div align="center">

```
██████╗ ███████╗ █████╗ ██╗     ███████╗██╗███╗   ██╗██████╗ ██████╗
██╔══██╗██╔════╝██╔══██╗██║     ██╔════╝██║████╗  ██║██╔══██╗██╔══██╗
██║  ██║█████╗  ███████║██║     █████╗  ██║██╔██╗ ██║██║  ██║██████╔╝
██║  ██║██╔══╝  ██╔══██║██║     ██╔══╝  ██║██║╚██╗██║██║  ██║██╔══██╗
██████╔╝███████╗██║  ██║███████╗██║     ██║██║ ╚████║██████╔╝██║  ██║
╚═════╝ ╚══════╝╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝╚═╝  ╚═══╝╚═════╝ ╚═╝  ╚═╝
```

**Find the best price for anything — across the entire internet — from your terminal.**

[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge)](LICENSE)
[![No API Keys](https://img.shields.io/badge/No%20API%20Keys-Required-f59e0b?style=for-the-badge)](#)
[![Runs Locally](https://img.shields.io/badge/Runs-100%25%20Locally-6366f1?style=for-the-badge)](#)

</div>

---

## What is DealFindr?

DealFindr is a **local, no-API-key price comparison tool** that simultaneously searches six major shopping platforms and returns results sorted cheapest to most expensive — right in your terminal.

```
$ python dealfindr.py "apple thunderbolt 2 cable"
```

```
╭─────────────────────────────────────────────────────╮
│ DealFindr  ·  24 deals for  apple thunderbolt 2 cable│
╰─────────────────────────────────────────────────────╯

 #  │ Source           │ Cond.    │   Price  │ Shipping │ Title
────┼──────────────────┼──────────┼──────────┼──────────┼────────────────────────
🥇  │ Craigslist       │ Used     │   $8.00  │    —     │ Apple Thunderbolt 2...
🥈  │ eBay             │ Used     │   $9.99  │   FREE   │ Apple Thunderbolt 2...
🥉  │ eBay             │ New      │  $14.99  │   FREE   │ Apple Thunderbolt to...
 4  │ Amazon           │ New      │  $19.99  │   FREE   │ Apple Thunderbolt 2...
 5  │ Best Buy         │ New      │  $29.99  │   FREE   │ Apple Thunderbolt 2 ...
 6  │ Walmart          │ New      │  $34.99  │   FREE   │ Apple Thunderbolt 2 ...

Purchase Links — cheapest → most expensive:

   1.  $8.00              Craigslist (Sfbay)           https://sfbay.craigslist.org/...
   2.  $9.99 (free ship)  eBay                         https://www.ebay.com/itm/...
   3.  $14.99 (free ship) eBay                         https://www.ebay.com/itm/...
```

---

## Supported Sources

| Platform          | Method           | Reliability | Notes                                  |
|-------------------|------------------|-------------|----------------------------------------|
| **eBay**          | HTML scraping    | ★★★★★      | Buy-It-Now, sorted by price + shipping |
| **Craigslist**    | RSS/XML feeds    | ★★★★★      | 8 major cities searched simultaneously |
| **Amazon**        | HTML scraping    | ★★★★☆      | Sorted price-ascending; may vary        |
| **Walmart**       | Next.js JSON     | ★★★★☆      | Parses embedded `__NEXT_DATA__` payload |
| **Best Buy**      | HTML scraping    | ★★★★☆      | Sorted price-ascending                  |
| **Google Shopping**| HTML scraping   | ★★★☆☆      | Works when not rate-limited             |

> **Why not Facebook Marketplace or OfferUp?**
> Both platforms require active login sessions and deploy heavy JavaScript rendering with fingerprinting — they are technically not scrapeable without a logged-in browser session. Use the Craigslist results as an alternative for local used-item deals.

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/amarkulis/dealfindr.git
cd dealfindr
```

### 2. Create a virtual environment (recommended)

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

**That's it.** No API keys, no accounts, no config files.

---

## Usage

### Basic search

```bash
python dealfindr.py "apple thunderbolt 2 cable"
```

### Multi-word queries (no quotes needed)

```bash
python dealfindr.py macbook pro m3
```

### Skip specific sources

```bash
python dealfindr.py "ps5 controller" --no-craigslist --no-google
```

### Search specific Craigslist cities

```bash
python dealfindr.py "vintage guitar" --cities losangeles sfbay newyork chicago seattle
```

### Limit results per source

```bash
python dealfindr.py "airpods" --max-results 5
```

### Export to CSV

```bash
python dealfindr.py "iphone 15 pro" --export results.csv
```

### All options

```
usage: dealfindr [-h] [--no-ebay] [--no-amazon] [--no-craigslist]
                 [--no-walmart] [--no-bestbuy] [--no-google]
                 [--cities CITY [CITY ...]] [--max-results N]
                 [--export FILE] query [query ...]

positional arguments:
  query                 Item to search for

options:
  --no-ebay             Skip eBay
  --no-amazon           Skip Amazon
  --no-craigslist       Skip Craigslist
  --no-walmart          Skip Walmart
  --no-bestbuy          Skip Best Buy
  --no-google           Skip Google Shopping
  --cities CITY [CITY ...]
                        Craigslist city slugs (e.g. losangeles sfbay newyork)
  --max-results N       Max results per source (default: 20)
  --export FILE         Export results to a CSV file
```

---

## How It Works

DealFindr runs all scrapers **concurrently** using Python's `ThreadPoolExecutor`. Each scraper is isolated so a failure on one platform doesn't affect the others.

```
┌─────────────┐     ┌──────────┐     ┌─────────────────────┐
│  Your query │────▶│ 6 threads│────▶│ eBay                │
└─────────────┘     │ parallel │     │ Craigslist (8 cities)│
                    │          │     │ Amazon               │
                    │          │     │ Walmart              │
                    │          │     │ Best Buy             │
                    └──────────┘     │ Google Shopping      │
                                     └──────────┬──────────┘
                                                │
                                     ┌──────────▼──────────┐
                                     │ Deduplicate & sort  │
                                     │ by total price      │
                                     └──────────┬──────────┘
                                                │
                                     ┌──────────▼──────────┐
                                     │  Rich terminal UI   │
                                     │  + clickable links  │
                                     └─────────────────────┘
```

### Price sorting

Results are sorted by **total price** (item price + shipping). Free shipping is counted as $0. Listings with unknown shipping are sorted by item price alone.

---

## Dependencies

| Package         | Purpose                          |
|-----------------|----------------------------------|
| `requests`      | HTTP requests to shopping sites  |
| `beautifulsoup4`| HTML parsing                     |
| `lxml`          | Fast HTML/XML parser backend     |
| `rich`          | Beautiful terminal output        |

All pure Python — no browser, no Selenium, no Playwright required.

---

## Tips for Best Results

- **Be specific**: `"apple thunderbolt 2 cable 0.5m"` returns better results than just `"cable"`
- **Use Craigslist** for used/local deals — it's the most reliable scraper (RSS feed, no bot detection)
- **eBay** consistently has the widest used-item selection
- If **Amazon returns 0 results**, it triggered their bot check — wait a minute and retry with `--no-craigslist --no-google` to reduce simultaneous load
- **Google Shopping** is rate-limited aggressively; use `--no-google` if you're running multiple searches in a row

---

## Roadmap

- [ ] OfferUp support (requires headless browser)
- [ ] Price history / alerts
- [ ] Saved searches
- [ ] `--new-only` / `--used-only` filters
- [ ] Minimum / maximum price filters (`--min-price`, `--max-price`)
- [ ] Browser extension companion

---

## License

MIT © [amarkulis](https://github.com/amarkulis)

---

<div align="center">

Made with ☕ and too many browser tabs.

**[⭐ Star this repo](https://github.com/amarkulis/dealfindr)** if it saved you money.

</div>
