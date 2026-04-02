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

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge)](LICENSE)
[![No API Keys](https://img.shields.io/badge/No%20API%20Keys-Required-f59e0b?style=for-the-badge)](#)
[![Runs Locally](https://img.shields.io/badge/Runs-100%25%20Locally-6366f1?style=for-the-badge)](#)

</div>

---

## What is DealFindr?

Every price-comparison tool out there is either missing sources, locked behind API keys, or wrapped in a slow browser extension. **DealFindr fills the gap.** It searches six major shopping platforms simultaneously and returns results sorted cheapest to most expensive — right in your terminal, running 100% locally with zero accounts.

> **Intended for personal use.** DealFindr runs on your own machine and makes the same requests a browser would. It is not a commercial data-harvesting service.

```
$ python3 dealfindr.py "apple thunderbolt 2 cable"
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

| Platform          | Method                | Reliability | Notes                                  |
|-------------------|-----------------------|-------------|----------------------------------------|
| **eBay**          | HTML scraping         | ★★★★☆      | Buy-It-Now, sorted by price + shipping. Layout changes can temporarily break selectors. |
| **Craigslist**    | HTML + JSON-LD        | ★★★★☆      | Searches up to 8 cities. Defaults to Chicago. |
| **Amazon**        | HTML scraping         | ★★★☆☆      | May trigger bot detection after repeated requests. |
| **Walmart**       | Next.js JSON          | ★★★★☆      | Parses embedded `__NEXT_DATA__` payload with tree-walk fallback. |
| **Best Buy**      | HTML scraping         | ★★★☆☆      | Sorted price-ascending. May return 0 results when blocked. |
| **Google Shopping**| HTML scraping        | ★★☆☆☆      | Aggressively rate-limited. Best as a bonus source. |

> **Reliability note:** All scrapers depend on page structure that can change without notice. Run with `--verbose` to see which sources returned results and which were blocked. If a source starts returning 0 results consistently, [open an issue](https://github.com/amarkulis/dealfindr/issues).

> **Why not Facebook Marketplace or OfferUp?**
> Both platforms require active login sessions and deploy heavy JavaScript rendering with fingerprinting — they are not scrapeable without a logged-in browser session. Use Craigslist results as an alternative for local used-item deals.

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

**Linux (Debian / Ubuntu)** — install `pip` first if needed:
```bash
sudo apt install python3-pip
python3 -m pip install -r requirements.txt
```

**macOS:**
```bash
pip3 install -r requirements.txt
```

**Windows:**
```powershell
pip install -r requirements.txt
```

**That's it.** No API keys, no accounts, no config files.

---

## Usage

### Basic search

```bash
python3 dealfindr.py "apple thunderbolt 2 cable"
```

> On Windows use `python` instead of `python3`.

### Interactive mode

Run without a query, or use `--interactive`, to pick your sources and cities from prompts.

```bash
python3 dealfindr.py
python3 dealfindr.py --interactive
```

### Multi-word queries (no quotes needed)

```bash
python3 dealfindr.py macbook pro m3
```

### Skip specific sources

```bash
python3 dealfindr.py "ps5 controller" --no-craigslist --no-google
```

### Search one source only

```bash
python3 dealfindr.py "apple thunderbolt 2 cable" --source amazon
python3 dealfindr.py "macbook pro" --source amazon ebay craigslist
```

### Search specific Craigslist cities

Craigslist defaults to `chicago` if you do not pass `--cities`.

```bash
python3 dealfindr.py "vintage guitar" --cities chicago
python3 dealfindr.py "vintage guitar" --cities chicago milwaukee madison
```

### Limit results per source

```bash
python3 dealfindr.py "airpods" --max-results 5
```

### Export to CSV

```bash
python3 dealfindr.py "iphone 15 pro" --export results.csv
```

### JSON output (for scripts / piping)

```bash
python3 dealfindr.py "airpods" --json
python3 dealfindr.py "airpods" --json | jq '.deals[0]'
```

### Debugging

```bash
python3 dealfindr.py "macbook" --verbose      # see which sources succeed/fail
python3 dealfindr.py "macbook" --debug         # full HTTP-level detail
```

### All options

```
usage: dealfindr [-h] [--interactive] [--version]
                 [--source {amazon,bestbuy,craigslist,ebay,google,walmart} [{amazon,bestbuy,craigslist,ebay,google,walmart} ...]]
                 [--no-ebay] [--no-amazon] [--no-craigslist]
                 [--no-walmart] [--no-bestbuy] [--no-google]
                 [--cities CITY [CITY ...]] [--max-results N]
                 [--export FILE] [--json] [-v] [--debug]
                 [query ...]

positional arguments:
  query                 Item to search for

options:
  --interactive         Launch interactive setup
  --version             Show version and exit
  --source              Only search specific sources
  --no-ebay             Skip eBay
  --no-amazon           Skip Amazon
  --no-craigslist       Skip Craigslist
  --no-walmart          Skip Walmart
  --no-bestbuy          Skip Best Buy
  --no-google           Skip Google Shopping
  --cities CITY [CITY ...]
                        Craigslist city slugs (default: chicago)
  --max-results N       Max results per source (default: 20)
  --export FILE         Export results to a CSV file
  --json                Output results as JSON
  -v, --verbose         Verbose logging output
  --debug               Debug-level logging (very detailed)
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
- **Use Craigslist** for used/local deals — low bot detection, good for used items
- **eBay** consistently has the widest used-item selection
- If **Amazon returns 0 results**, it triggered their bot check — wait a minute and retry
- **Google Shopping** is rate-limited aggressively; use `--no-google` if you're running multiple searches in a row
- Use `--verbose` to see which sources returned results and which failed

---

## Roadmap

- [ ] `--new-only` / `--used-only` condition filters
- [ ] `--min-price` / `--max-price` filters
- [ ] Price history tracking and alerts
- [ ] Saved searches
- [ ] OfferUp support (requires headless browser)
- [ ] `pip install dealfindr` (PyPI publication)

---

## Disclaimer

DealFindr is intended for **personal, non-commercial use only**. It makes standard HTTP requests on your behalf — just like a web browser would. It does not bypass CAPTCHAs, log in to accounts, or circumvent access controls. Scraping policies vary by site; use responsibly and respect each platform's terms of service.

---

## License

MIT © [amarkulis](https://github.com/amarkulis)

---

<div align="center">

Made with ☕ and too many browser tabs.

**[⭐ Star this repo](https://github.com/amarkulis/dealfindr)** if it saved you money.

</div>
