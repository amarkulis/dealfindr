"""Tests for DealFindr — unit tests for parsing, relevance, data model, and export."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path so `import dealfindr` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dealfindr


# ──────────────────────────────────────────────────────────────────────────────
# _parse_price
# ──────────────────────────────────────────────────────────────────────────────


class TestParsePrice:
    def test_dollar_sign(self):
        assert dealfindr._parse_price("$19.99") == 19.99

    def test_no_dollar_sign(self):
        assert dealfindr._parse_price("19.99") == 19.99

    def test_comma_thousands(self):
        assert dealfindr._parse_price("$1,299.00") == 1299.00

    def test_whole_number(self):
        assert dealfindr._parse_price("$50") == 50.0

    def test_with_extra_text(self):
        assert dealfindr._parse_price("Price: $14.99 + shipping") == 14.99

    def test_free(self):
        # "Free" has no digits so should return None
        assert dealfindr._parse_price("Free") is None

    def test_empty_string(self):
        assert dealfindr._parse_price("") is None

    def test_none(self):
        assert dealfindr._parse_price(None) is None


# ──────────────────────────────────────────────────────────────────────────────
# Deal dataclass
# ──────────────────────────────────────────────────────────────────────────────


class TestDeal:
    def test_total_price_with_shipping(self):
        d = dealfindr.Deal("Item", 10.0, "http://x", "eBay", shipping=5.0)
        assert d.total_price == 15.0

    def test_total_price_free_shipping(self):
        d = dealfindr.Deal("Item", 10.0, "http://x", "eBay", shipping=0.0)
        assert d.total_price == 10.0

    def test_total_price_none_shipping(self):
        d = dealfindr.Deal("Item", 10.0, "http://x", "eBay", shipping=None)
        assert d.total_price == 10.0

    def test_default_condition(self):
        d = dealfindr.Deal("Item", 10.0, "http://x", "eBay")
        assert d.condition == "Unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Relevance filtering
# ──────────────────────────────────────────────────────────────────────────────


class TestRelevance:
    def test_exact_substring_match(self):
        assert dealfindr._is_relevant_title(
            "Apple Thunderbolt 2 Cable 0.5m", "thunderbolt 2 cable"
        )

    def test_empty_title(self):
        assert not dealfindr._is_relevant_title("", "thunderbolt cable")

    def test_no_overlap(self):
        assert not dealfindr._is_relevant_title(
            "Samsung Galaxy Phone Case", "apple thunderbolt cable"
        )

    def test_partial_token_overlap_rejected_when_strict(self):
        # 3 tokens: "apple", "thunderbolt", "cable" → needs all 3
        # Title has "apple" + "cable" but NOT "thunderbolt" → rejected
        assert not dealfindr._is_relevant_title(
            "Apple USB-C to Lightning Cable 1m", "apple thunderbolt cable"
        )

    def test_all_tokens_match(self):
        assert dealfindr._is_relevant_title(
            "Apple Thunderbolt 3 Cable USB-C", "apple thunderbolt cable"
        )

    def test_single_token_query(self):
        assert dealfindr._is_relevant_title("AirPods Pro 2nd Gen", "airpods")

    def test_walnuts_rejected_for_pistachio_query(self):
        # The bug: "2lb" + "shelled" matched but product name didn't
        assert not dealfindr._is_relevant_title(
            "Sincerely Nuts Raw Shelled Walnuts (2lb bag)",
            "2lb shelled pistachios",
        )

    def test_four_token_allows_one_miss(self):
        # 4 tokens → needs 3
        assert dealfindr._is_relevant_title(
            "Apple Thunderbolt Cable 2m", "apple thunderbolt 2 cable"
        )


class TestFuzzyMatching:
    def test_typo_pistachio(self):
        # "pistashios" (typo) should fuzzy-match "pistachios"
        assert dealfindr._is_relevant_title(
            "Wonderful Pistachios Shelled 2lb Bag",
            "2lb shelled pistashios",
        )

    def test_typo_does_not_match_unrelated(self):
        assert not dealfindr._fuzzy_token_in_title(
            "pistashios", ["walnuts", "shelled", "raw"]
        )

    def test_short_tokens_skip_fuzzy(self):
        # Tokens < 4 chars should not attempt fuzzy matching
        assert not dealfindr._fuzzy_token_in_title("2lb", ["2lbs", "bag"])

    def test_close_match_passes(self):
        assert dealfindr._fuzzy_token_in_title(
            "thunderblt", ["thunderbolt", "cable"]
        )


class TestContradictions:
    def test_shelled_rejects_in_shell(self):
        assert not dealfindr._is_relevant_title(
            "Roasted In-Shell Pistachios USDA Food 2LB",
            "2lb shelled pistachios",
        )

    def test_shelled_accepts_shelled(self):
        assert dealfindr._is_relevant_title(
            "Wonderful Pistachios Shelled 2lb Bag",
            "2lb shelled pistachios",
        )

    def test_in_shell_rejects_shelled(self):
        assert not dealfindr._is_relevant_title(
            "Pistachios Shelled Raw Unsalted 2Lbs",
            "2lb in-shell pistachios",
        )

    def test_unsalted_rejects_salted(self):
        assert dealfindr._has_contradiction(
            "Sea Salted Pistachios 2lb", "unsalted pistachios"
        )

    def test_salted_rejects_unsalted(self):
        assert dealfindr._has_contradiction(
            "Unsalted Raw Pistachios", "salted pistachios"
        )

    def test_raw_rejects_roasted(self):
        assert dealfindr._has_contradiction(
            "Roasted Salted Pistachios", "raw pistachios"
        )

    def test_no_contradiction_when_matching(self):
        assert not dealfindr._has_contradiction(
            "Shelled Pistachios 2lb", "2lb shelled pistachios"
        )


class TestIntentFilters:
    def test_cable_query_rejects_display(self):
        assert not dealfindr._passes_intent_filters(
            "Dell 27 inch Monitor Display 4K", "thunderbolt cable"
        )

    def test_cable_query_rejects_adapter(self):
        assert not dealfindr._passes_intent_filters(
            "Thunderbolt 3 to HDMI Adapter Dongle", "thunderbolt cable"
        )

    def test_cable_query_accepts_cable(self):
        assert dealfindr._passes_intent_filters(
            "Apple Thunderbolt 2 Cable", "thunderbolt cable"
        )

    def test_no_intent_passes_anything(self):
        assert dealfindr._passes_intent_filters(
            "Random Product With a Dock", "macbook pro"
        )

    def test_charger_query_accepts_charger(self):
        assert dealfindr._passes_intent_filters(
            "Apple 96W USB-C Power Charger", "macbook charger"
        )


class TestQueryTokens:
    def test_strips_stop_words(self):
        tokens = dealfindr._query_tokens("cable for the new macbook")
        assert "for" not in tokens
        assert "the" not in tokens
        assert "new" not in tokens
        assert "cable" in tokens
        assert "macbook" in tokens

    def test_keeps_digits(self):
        tokens = dealfindr._query_tokens("thunderbolt 2 cable")
        assert "2" in tokens


# ──────────────────────────────────────────────────────────────────────────────
# Dedupe & sort
# ──────────────────────────────────────────────────────────────────────────────


class TestDedupeAndSort:
    def test_sorts_by_total_price(self):
        deals = [
            dealfindr.Deal("Expensive", 50.0, "http://a", "eBay"),
            dealfindr.Deal("Cheap", 5.0, "http://b", "eBay"),
            dealfindr.Deal("Mid", 20.0, "http://c", "eBay"),
        ]
        result = dealfindr._dedupe_and_sort(deals)
        prices = [d.total_price for d in result]
        assert prices == sorted(prices)

    def test_removes_duplicate_urls(self):
        deals = [
            dealfindr.Deal("Item A", 10.0, "http://same", "eBay"),
            dealfindr.Deal("Item B", 20.0, "http://same", "Amazon"),
        ]
        result = dealfindr._dedupe_and_sort(deals)
        assert len(result) == 1

    def test_keeps_unique_urls(self):
        deals = [
            dealfindr.Deal("Item A", 10.0, "http://a", "eBay"),
            dealfindr.Deal("Item B", 20.0, "http://b", "Amazon"),
        ]
        result = dealfindr._dedupe_and_sort(deals)
        assert len(result) == 2


# ──────────────────────────────────────────────────────────────────────────────
# Export functions
# ──────────────────────────────────────────────────────────────────────────────


class TestExportCSV:
    def test_csv_header_and_rows(self):
        deals = [
            dealfindr.Deal("Item A", 10.0, "http://a", "eBay", "New", 0.0),
            dealfindr.Deal("Item B", 20.0, "http://b", "Amazon", "New", 5.0),
        ]
        csv_str = dealfindr.export_csv(deals, "test query")
        lines = csv_str.strip().split("\n")
        assert len(lines) == 3  # header + 2 rows
        assert "Rank" in lines[0]
        assert "eBay" in lines[1]  # cheapest first

    def test_empty_deals(self):
        csv_str = dealfindr.export_csv([], "nothing")
        lines = csv_str.strip().split("\n")
        assert len(lines) == 1  # header only


class TestExportJSON:
    def test_json_structure(self):
        deals = [
            dealfindr.Deal("Item A", 10.0, "http://a", "eBay", "Used", 0.0),
        ]
        result = json.loads(dealfindr.export_json(deals, "test"))
        assert result["query"] == "test"
        assert result["count"] == 1
        assert result["deals"][0]["title"] == "Item A"
        assert result["deals"][0]["price"] == 10.0
        assert result["deals"][0]["total_price"] == 10.0
        assert result["deals"][0]["source"] == "eBay"
        assert result["deals"][0]["url"] == "http://a"

    def test_empty_deals(self):
        result = json.loads(dealfindr.export_json([], "nothing"))
        assert result["count"] == 0
        assert result["deals"] == []

    def test_sorted_output(self):
        deals = [
            dealfindr.Deal("Expensive", 50.0, "http://b", "Amazon"),
            dealfindr.Deal("Cheap", 5.0, "http://a", "eBay"),
        ]
        result = json.loads(dealfindr.export_json(deals, "test"))
        assert result["deals"][0]["price"] == 5.0
        assert result["deals"][1]["price"] == 50.0


# ──────────────────────────────────────────────────────────────────────────────
# Source selection parsing
# ──────────────────────────────────────────────────────────────────────────────


class TestParseSourceSelection:
    def test_all_keyword(self):
        result = dealfindr._parse_source_selection("all")
        assert set(result) == set(dealfindr._SOURCE_LABELS.keys())

    def test_empty_string(self):
        result = dealfindr._parse_source_selection("")
        assert set(result) == set(dealfindr._SOURCE_LABELS.keys())

    def test_by_name(self):
        result = dealfindr._parse_source_selection("amazon ebay")
        assert result == ["amazon", "ebay"]

    def test_by_number(self):
        options = list(dealfindr._SOURCE_LABELS.keys())
        result = dealfindr._parse_source_selection("1")
        assert result == [options[0]]

    def test_comma_separated(self):
        result = dealfindr._parse_source_selection("amazon, walmart")
        assert "amazon" in result
        assert "walmart" in result

    def test_deduplicates(self):
        result = dealfindr._parse_source_selection("amazon amazon ebay")
        assert result.count("amazon") == 1


# ──────────────────────────────────────────────────────────────────────────────
# Scraper unit tests (mocked HTTP)
# ──────────────────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    pytest.skip(f"Fixture {name} not found — run with live data first")


def _mock_response(html: str, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    return resp


class TestEbayParser:
    """Test eBay parser against a minimal fixture."""

    MINIMAL_EBAY_HTML = """
    <html><body>
    <ul>
      <li data-view="mi:1234">
        <a class="s-card__link" href="https://www.ebay.com/itm/123456789?hash=tracking">
          <span class="s-card__title">Apple Thunderbolt 2 Cable 0.5m</span>
        </a>
        <span class="s-card__price">$14.99</span>
        <span class="su-styled-text secondary large">Free shipping</span>
        <span class="su-styled-text secondary default">New</span>
      </li>
      <li data-view="mi:1235">
        <a class="s-card__link" href="https://www.ebay.com/itm/987654321?hash=tracking2">
          <span class="s-card__title">Apple Thunderbolt 2 Cable 2m</span>
        </a>
        <span class="s-card__price">$24.50</span>
        <span class="su-styled-text secondary large">+$3.99 shipping</span>
        <span class="su-styled-text secondary default">Used</span>
      </li>
    </ul>
    </body></html>
    """

    @patch("dealfindr._get")
    def test_parses_two_items(self, mock_get):
        mock_get.return_value = _mock_response(self.MINIMAL_EBAY_HTML)
        deals = dealfindr.search_ebay("apple thunderbolt 2 cable", max_results=10)
        assert len(deals) == 2
        assert deals[0].source == "eBay"
        assert deals[0].price == 14.99
        assert "123456789" in deals[0].url
        assert deals[1].price == 24.50

    @patch("dealfindr._get")
    def test_returns_empty_on_failure(self, mock_get):
        mock_get.return_value = None
        deals = dealfindr.search_ebay("anything")
        assert deals == []

    @patch("dealfindr._get")
    def test_strips_tracking_from_url(self, mock_get):
        mock_get.return_value = _mock_response(self.MINIMAL_EBAY_HTML)
        deals = dealfindr.search_ebay("apple thunderbolt 2 cable")
        for d in deals:
            assert "?" not in d.url
            assert "hash=" not in d.url


class TestAmazonParser:
    MINIMAL_AMAZON_HTML = """
    <html><body>
    <div data-component-type="s-search-result">
      <h2><span>Apple Thunderbolt 2 Cable</span></h2>
      <a class="a-link-normal s-no-outline" href="/dp/B00P0GHXNI/ref=sr_1_1"></a>
      <span class="a-price-whole">19</span>
      <span class="a-price-fraction">99</span>
    </div>
    </body></html>
    """

    @patch("dealfindr._get")
    def test_parses_one_item(self, mock_get):
        mock_get.return_value = _mock_response(self.MINIMAL_AMAZON_HTML)
        deals = dealfindr.search_amazon("apple thunderbolt 2 cable", max_results=10)
        assert len(deals) == 1
        assert deals[0].price == 19.99
        assert deals[0].source == "Amazon"
        assert "/dp/B00P0GHXNI" in deals[0].url

    @patch("dealfindr._get")
    def test_detects_captcha(self, mock_get):
        mock_get.return_value = _mock_response(
            "<html><body>Please enter the characters you see below</body></html>"
        )
        deals = dealfindr.search_amazon("anything")
        assert deals == []


class TestWalmartParser:
    MINIMAL_WALMART_HTML = """
    <html><body>
    <script id="__NEXT_DATA__" type="application/json">
    {
      "props": {
        "pageProps": {
          "initialData": {
            "searchResult": {
              "itemStacks": [{
                "items": [
                  {
                    "name": "Apple Thunderbolt Cable",
                    "price": 29.99,
                    "canonicalUrl": "/ip/Apple-Thunderbolt-Cable/12345"
                  }
                ]
              }]
            }
          }
        }
      }
    }
    </script>
    </body></html>
    """

    @patch("dealfindr._get")
    def test_parses_nextdata(self, mock_get):
        mock_get.return_value = _mock_response(self.MINIMAL_WALMART_HTML)
        deals = dealfindr.search_walmart("apple thunderbolt cable", max_results=10)
        assert len(deals) == 1
        assert deals[0].price == 29.99
        assert deals[0].source == "Walmart"
        assert "walmart.com" in deals[0].url


# ──────────────────────────────────────────────────────────────────────────────
# Thread safety
# ──────────────────────────────────────────────────────────────────────────────


class TestThreadSafety:
    def test_per_thread_sessions(self):
        """Each thread should get its own requests.Session."""
        sessions = []

        def _grab_session():
            sessions.append(dealfindr._get_session())

        import threading

        t1 = threading.Thread(target=_grab_session)
        t2 = threading.Thread(target=_grab_session)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(sessions) == 2
        assert sessions[0] is not sessions[1]
