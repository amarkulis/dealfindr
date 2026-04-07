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
        # 4 core tokens → needs 3 (no version phrase involved)
        assert dealfindr._is_relevant_title(
            "Apple Thunderbolt Cable Pro", "apple thunderbolt usb cable"
        )


class TestModifierTokens:
    def test_standalone_number_is_core(self):
        # "15" in "iphone 15" is a model number, not a modifier
        assert not dealfindr._is_modifier_token("15")

    def test_qty_unit_is_modifier(self):
        assert dealfindr._is_modifier_token("2lb")
        assert dealfindr._is_modifier_token("24oz")
        assert dealfindr._is_modifier_token("16gb")

    def test_unit_word_is_modifier(self):
        assert dealfindr._is_modifier_token("lb")
        assert dealfindr._is_modifier_token("oz")

    def test_product_word_is_not_modifier(self):
        assert not dealfindr._is_modifier_token("iphone")
        assert not dealfindr._is_modifier_token("pistachios")

    def test_iphone_15_rejects_iphone_14(self):
        # "15" is core, so "iphone 14" must NOT match "iphone 15"
        assert not dealfindr._is_relevant_title(
            "Apple iPhone 14 Pro Max 256GB", "iphone 15"
        )

    def test_iphone_15_accepts_iphone_15(self):
        assert dealfindr._is_relevant_title(
            "Apple iPhone 15 Pro 128GB Unlocked", "iphone 15"
        )

    def test_ps5_rejects_ps4(self):
        assert not dealfindr._is_relevant_title(
            "Sony PS4 DualShock Controller", "ps5 controller"
        )

    def test_2lb_pistachios_allows_different_weight(self):
        # "2lb" is a modifier, so a 1lb bag of the right product should pass
        assert dealfindr._is_relevant_title(
            "Wonderful Pistachios Shelled 1lb Bag",
            "2lb shelled pistachios",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Version phrase matching
# ──────────────────────────────────────────────────────────────────────────────


class TestVersionPhrases:
    def test_extracts_thunderbolt_2(self):
        pairs = dealfindr._version_phrases("thunderbolt 2 cable")
        assert ("thunderbolt", "2") in pairs

    def test_extracts_iphone_15(self):
        pairs = dealfindr._version_phrases("iphone 15 pro max")
        assert ("iphone", "15") in pairs

    def test_extracts_usb_3_0(self):
        pairs = dealfindr._version_phrases("usb 3.0 cable")
        assert ("usb", "3.0") in pairs

    def test_no_phrase_for_ps5(self):
        # "ps5" has no space → no version phrase detected
        pairs = dealfindr._version_phrases("ps5")
        assert len(pairs) == 0

    def test_skips_stop_words(self):
        # "for 2" → "for" is a stop word, shouldn't be a version phrase
        pairs = dealfindr._version_phrases("cable for 2 devices")
        assert not any(w == "for" for w, n in pairs)

    def test_thunderbolt_2_rejects_thunderbolt_3(self):
        assert not dealfindr._is_relevant_title(
            "StarTech.com Thunderbolt 3 Cable 2m", "thunderbolt 2 cable"
        )

    def test_thunderbolt_2_rejects_usb_for_tb_drive(self):
        assert not dealfindr._is_relevant_title(
            "White USB 3.0 Sync Data Cable for LACIE D2 2 Thunderbolt External Hard Drive",
            "thunderbolt 2 cable",
        )

    def test_thunderbolt_2_accepts_actual_tb2(self):
        assert dealfindr._is_relevant_title(
            "Apple Thunderbolt 2 Cable 0.5m", "thunderbolt 2 cable"
        )

    def test_usb_3_rejects_usb_2(self):
        assert not dealfindr._is_relevant_title(
            "USB 2.0 Type A Cable 6ft", "usb 3 cable"
        )

    def test_usb_3_accepts_usb_3_0(self):
        assert dealfindr._is_relevant_title(
            "USB 3.0 Type A Cable 6ft", "usb 3 cable"
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
        # Force requests fallback by making Playwright import fail
        import builtins
        _real_import = builtins.__import__
        def _block_pw(name, *a, **kw):
            if "playwright" in name:
                raise ImportError("blocked for test")
            return _real_import(name, *a, **kw)
        with patch("builtins.__import__", side_effect=_block_pw):
            deals = dealfindr.search_ebay("apple thunderbolt 2 cable", max_results=10)
        assert len(deals) == 2
        assert deals[0].source == "eBay"
        assert deals[0].price == 14.99
        assert "123456789" in deals[0].url
        assert deals[1].price == 24.50

    @patch("dealfindr._get")
    def test_returns_empty_on_failure(self, mock_get):
        mock_get.return_value = None
        import builtins
        _real_import = builtins.__import__
        def _block_pw(name, *a, **kw):
            if "playwright" in name:
                raise ImportError("blocked for test")
            return _real_import(name, *a, **kw)
        with patch("builtins.__import__", side_effect=_block_pw):
            deals = dealfindr.search_ebay("anything")
        assert deals == []

    @patch("dealfindr._get")
    def test_strips_tracking_from_url(self, mock_get):
        mock_get.return_value = _mock_response(self.MINIMAL_EBAY_HTML)
        import builtins
        _real_import = builtins.__import__
        def _block_pw(name, *a, **kw):
            if "playwright" in name:
                raise ImportError("blocked for test")
            return _real_import(name, *a, **kw)
        with patch("builtins.__import__", side_effect=_block_pw):
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
        # Both passes return same URL → deduped to 1 deal (first pass = New)
        assert len(deals) == 1
        assert deals[0].price == 19.99
        assert deals[0].source == "Amazon"
        assert deals[0].condition == "New"
        assert "/dp/B00P0GHXNI" in deals[0].url

    @patch("dealfindr._get")
    def test_detects_captcha(self, mock_get):
        mock_get.return_value = _mock_response(
            "<html><body>Please enter the characters you see below</body></html>"
        )
        deals = dealfindr.search_amazon("anything")
        assert deals == []

    def test_renewed_condition_detection(self):
        html = """
        <html><body>
        <div data-component-type="s-search-result">
          <h2><span>Apple iPhone 13, 128GB, Midnight - Unlocked (Renewed)</span></h2>
          <a class="a-link-normal s-no-outline" href="/dp/B09LNW3CY2/ref=sr_1"></a>
          <span class="a-price-whole">269</span>
          <span class="a-price-fraction">00</span>
        </div>
        </body></html>
        """
        deals = dealfindr._parse_amazon_results(html, "iphone 13", "Used", 10)
        assert len(deals) == 1
        assert deals[0].condition == "Renewed"


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


# ──────────────────────────────────────────────────────────────────────────────
# Weight / unit conversion
# ──────────────────────────────────────────────────────────────────────────────


class TestFmtNum:
    def test_whole_number(self):
        assert dealfindr._fmt_num(32.0) == "32"

    def test_decimal(self):
        assert dealfindr._fmt_num(2.5) == "2.5"

    def test_trailing_zeros(self):
        assert dealfindr._fmt_num(1.10) == "1.1"


class TestExtractSize:
    def test_lb_with_conversion(self):
        result = dealfindr._extract_size("Wonderful Pistachios 2lb Bag")
        assert result is not None
        assert "2 lb" in result
        assert "32 oz" in result

    def test_oz_with_conversion(self):
        result = dealfindr._extract_size("Pistachios 32oz Resealable")
        assert result is not None
        assert "32 oz" in result
        assert "2 lb" in result

    def test_kg(self):
        result = dealfindr._extract_size("Organic Nuts 1kg Bag")
        assert result is not None
        assert "1 kg" in result

    def test_count(self):
        result = dealfindr._extract_size("K-Cups 24 Count Box")
        assert result == "24 ct"

    def test_no_weight(self):
        assert dealfindr._extract_size("Apple Thunderbolt 2 Cable") is None

    def test_small_oz_no_lb_conversion(self):
        # Under 4oz should not show lb conversion
        result = dealfindr._extract_size("Sample Pack 2oz")
        assert result is not None
        assert "lb" not in result


class TestGenerateAltQueries:
    def test_lb_to_oz(self):
        alts = dealfindr._generate_alt_queries("2lb shelled pistachios")
        assert len(alts) == 1
        assert "32oz" in alts[0]
        assert "shelled pistachios" in alts[0]

    def test_oz_to_lb(self):
        alts = dealfindr._generate_alt_queries("32oz shelled pistachios")
        assert len(alts) == 1
        assert "2lb" in alts[0]
        assert "shelled pistachios" in alts[0]

    def test_kg_to_lb(self):
        alts = dealfindr._generate_alt_queries("1kg almonds")
        assert len(alts) == 1
        assert "lb" in alts[0]

    def test_no_weight(self):
        alts = dealfindr._generate_alt_queries("iphone 15 pro max")
        assert alts == []

    def test_small_oz_no_conversion(self):
        # 2oz = 0.125lb, below 0.25 threshold
        alts = dealfindr._generate_alt_queries("2oz sample")
        assert alts == []


# ──────────────────────────────────────────────────────────────────────────────
# JSON utilities
# ──────────────────────────────────────────────────────────────────────────────


class TestWalkJsonProducts:
    def test_finds_product(self):
        data = {"items": [{"name": "Widget", "price": 9.99}]}
        found = dealfindr._walk_json_products(data)
        assert len(found) == 1
        assert found[0]["name"] == "Widget"

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": [{"name": "Deep", "price": 5}]}}}
        found = dealfindr._walk_json_products(data)
        assert len(found) == 1

    def test_empty_data(self):
        assert dealfindr._walk_json_products({}) == []
        assert dealfindr._walk_json_products([]) == []


class TestExtractJsonPrice:
    def test_numeric_price(self):
        assert dealfindr._extract_json_price({"price": 19.99}) == 19.99

    def test_string_price(self):
        assert dealfindr._extract_json_price({"price": "$14.99"}) == 14.99

    def test_nested_price(self):
        assert dealfindr._extract_json_price({"price": {"min": 12.50}}) == 12.50

    def test_price_info_fallback(self):
        assert dealfindr._extract_json_price({"priceInfo": {"currentPrice": 8.0}}) == 8.0

    def test_no_price(self):
        assert dealfindr._extract_json_price({"title": "No price here"}) is None

    def test_custom_keys(self):
        assert dealfindr._extract_json_price(
            {"current_retail": 24.99}, keys=("current_retail",)
        ) == 24.99


# ──────────────────────────────────────────────────────────────────────────────
# Source labels expansion
# ──────────────────────────────────────────────────────────────────────────────


class TestSourceLabels:
    def test_all_eleven_sources(self):
        assert len(dealfindr._SOURCE_LABELS) == 11

    def test_new_sources_present(self):
        for key in ("target", "newegg", "mercari", "swappa", "aliexpress"):
            assert key in dealfindr._SOURCE_LABELS

    def test_parse_source_new_sources(self):
        sel = dealfindr._parse_source_selection("target newegg")
        assert "target" in sel
        assert "newegg" in sel


# ──────────────────────────────────────────────────────────────────────────────
# Deal size field
# ──────────────────────────────────────────────────────────────────────────────


class TestDealSize:
    def test_size_default_none(self):
        d = dealfindr.Deal("Item", 10.0, "http://x", "eBay")
        assert d.size is None

    def test_size_set(self):
        d = dealfindr.Deal("Item", 10.0, "http://x", "eBay", size="2 lb (32 oz)")
        assert d.size == "2 lb (32 oz)"


# ──────────────────────────────────────────────────────────────────────────────
# _title_to_oz
# ──────────────────────────────────────────────────────────────────────────────


class TestTitleToOz:
    def test_oz(self):
        assert dealfindr._title_to_oz("Almonds 12 oz bag") == pytest.approx(12.0)

    def test_lb(self):
        assert dealfindr._title_to_oz("Pistachios 2 lb") == pytest.approx(32.0)

    def test_kg(self):
        assert dealfindr._title_to_oz("Coffee 1 kg") == pytest.approx(35.274)

    def test_grams(self):
        assert dealfindr._title_to_oz("Spice 100g") == pytest.approx(3.527)

    def test_no_weight(self):
        assert dealfindr._title_to_oz("Bluetooth Speaker") is None

    def test_count_returns_none(self):
        assert dealfindr._title_to_oz("Batteries 12 pack") is None


# ──────────────────────────────────────────────────────────────────────────────
# Unit price property & sort order
# ──────────────────────────────────────────────────────────────────────────────


class TestUnitPrice:
    def test_unit_price_computed(self):
        d = dealfindr.Deal("Nuts 16oz", 16.0, "http://x", "Amazon", unit_oz=16.0)
        assert d.unit_price == pytest.approx(1.0)

    def test_unit_price_includes_shipping(self):
        d = dealfindr.Deal("Nuts 16oz", 16.0, "http://x", "Amazon", shipping=4.0, unit_oz=16.0)
        assert d.unit_price == pytest.approx(1.25)

    def test_unit_price_none_without_oz(self):
        d = dealfindr.Deal("Nuts", 10.0, "http://x", "Amazon")
        assert d.unit_price is None

    def test_sort_by_unit_price(self):
        """Deals with unit_price sort by $/oz; deals without sort by total last."""
        expensive_per_oz = dealfindr.Deal("Small 6oz", 6.0, "http://a", "A", unit_oz=6.0)   # $1.00/oz
        cheap_per_oz = dealfindr.Deal("Big 32oz", 22.0, "http://b", "B", unit_oz=32.0)       # $0.69/oz
        no_size_cheap = dealfindr.Deal("Mystery", 3.0, "http://c", "C")                       # no unit

        result = dealfindr._dedupe_and_sort([expensive_per_oz, no_size_cheap, cheap_per_oz])
        assert result[0].title == "Big 32oz"          # $0.69/oz — best value
        assert result[1].title == "Small 6oz"          # $1.00/oz
        assert result[2].title == "Mystery"            # no unit price, sorted last


# ──────────────────────────────────────────────────────────────────────────────
# Export with size field
# ──────────────────────────────────────────────────────────────────────────────


class TestExportWithSize:
    def test_csv_includes_size(self):
        deals = [dealfindr.Deal("Pistachios 2lb", 15.99, "http://x", "Amazon", size="2 lb (32 oz)", unit_oz=32.0)]
        csv_str = dealfindr.export_csv(deals, "pistachios")
        assert "Size" in csv_str
        assert "2 lb (32 oz)" in csv_str
        assert "$/oz" in csv_str
        assert "0.50" in csv_str  # $15.99/32oz ≈ $0.50

    def test_json_includes_size(self):
        deals = [dealfindr.Deal("Pistachios 2lb", 15.99, "http://x", "Amazon", size="2 lb (32 oz)", unit_oz=32.0)]
        json_str = dealfindr.export_json(deals, "pistachios")
        data = json.loads(json_str)
        assert data["deals"][0]["size"] == "2 lb (32 oz)"
        assert data["deals"][0]["unit_oz"] == 32.0
        assert data["deals"][0]["unit_price"] == pytest.approx(15.99 / 32.0)
