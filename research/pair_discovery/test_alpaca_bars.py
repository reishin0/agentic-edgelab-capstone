import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from edgelab.providers import ProviderError
from research.pair_discovery import alpaca_bars


def alias(instrument, symbol, first='2025-10-29', last='2025-10-31', known='2025-10-28'):
    return {"instrument_id": instrument, "symbol": symbol, "effective_from": first, "effective_through": last,
            "known_on": known, "source": "fixture security master"}


def plan(*aliases, instruments=None):
    return {"schema": 1, "window": {"start": "2025-10-29", "end": "2025-10-31"},
            "aliases": list(aliases), "instruments": sorted(set(instruments or [row["instrument_id"] for row in aliases]))}


def bar(day='2025-10-29', close=100.5, volume=120_000):
    return {"t": f"{day}T04:00:00Z", "c": close, "v": volume}


def page(rows, token=None):
    return {"bars": rows, "next_page_token": token}


class Acquisition(unittest.TestCase):
    def run_pages(self, inputs, *pages, max_requests=20):
        packets = iter(pages)
        queries = []

        def fetch(url, headers):
            parsed = urlsplit(url)
            self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                             ("https", "data.alpaca.markets", "/v2/stocks/bars"))
            self.assertEqual(headers, {"fixture": "only"})
            queries.append({key: values[0] for key, values in parse_qs(parsed.query).items()})
            return next(packets)

        result = alpaca_bars.acquire(inputs, {"fixture": "only"}, max_requests=max_requests, transport=fetch)
        return result, queries

    def test_pages_pin_query_and_keep_missing_coverage(self):
        inputs = plan(alias("A", "ABC"), alias("B", "DEF"), instruments=["A", "B", "C"])
        result, queries = self.run_pages(inputs, page({"ABC": [bar()]}, "next-page"),
                                         page({"DEF": [bar("2025-10-30")]}))
        expected = {"symbols": "ABC,DEF", "feed": "sip", "currency": "USD", "adjustment": "raw",
                    "asof": "2025-10-31", "start": "2025-10-29T00:00:00-04:00",
                    "end": "2025-10-31T23:59:59-04:00", "timeframe": "1Day", "sort": "asc", "limit": "1000"}
        self.assertEqual(queries, [expected, {**expected, "page_token": "next-page"}])
        self.assertEqual(result["bars"]["A"], {"2025-10-29": {"close": 100.5, "volume": 120_000}})
        self.assertEqual(result["bars"]["C"], {})
        self.assertEqual(result["coverage"]["B"]["missing_bar_sessions"], ["2025-10-29", "2025-10-31"])
        self.assertEqual(result["coverage"]["C"]["missing_alias_sessions"], result["sessions"])
        self.assertEqual(result["source"]["request_count"], 2)
        self.assertEqual(len(result["source"]["requests"][0]["pages"]), 2)
        self.assertNotIn("next-page", json.dumps(result))

    def test_symbol_transition_is_bound_to_permanent_id_and_two_nonoverlapping_intervals(self):
        inputs = plan(alias("ID", "OLD", last="2025-10-29"), alias("ID", "NEW", first="2025-10-30", known="2025-10-30"))
        result, queries = self.run_pages(inputs, page({"OLD": [bar()]}),
                                         page({"NEW": [bar("2025-10-30"), bar("2025-10-31")]}))
        self.assertEqual([(q["symbols"], q["asof"]) for q in queries], [("OLD", "2025-10-29"), ("NEW", "2025-10-31")])
        self.assertEqual(list(result["bars"]["ID"]), result["sessions"])
        self.assertEqual(result["coverage"]["ID"]["status"], "complete")

    def test_invalid_scope_aliases_and_budget_never_call_provider(self):
        post_cutoff = plan(alias("A", "ABC"))
        post_cutoff["window"]["end"] = "2025-11-03"
        unbudgeted = plan(alias("A", "ABC", last="2025-10-29"), alias("A", "DEF", first="2025-10-30"))
        rejected = (post_cutoff, plan(alias("A", "ABC"), alias("B", "ABC")),
                    plan(alias("A", "ABC"), alias("A", "DEF")), plan(alias("A", "ABC", known="2025-10-31")), unbudgeted)
        for invalid in rejected:
            with (self.subTest(plan=invalid), self.assertRaises(ValueError),
                  patch.object(alpaca_bars, "get_market_data", side_effect=AssertionError("provider called")) as transport):
                alpaca_bars.acquire(invalid, {}, max_requests=1, transport=transport)
                transport.assert_not_called()

    def test_unexpected_instrument_date_duplicate_and_bad_bar_abort(self):
        inputs = plan(alias("A", "ABC"))
        invalid_packets = (
            (page({"DEF": [bar()]}),),
            (page({"ABC": [bar("2025-11-03")]}),),
            (page({"ABC": [bar()]}, "again"), page({"ABC": [bar()]})),
            (page({"ABC": [bar(volume=12.5)]}),),
            (page({"ABC": [bar(close=False)]}),),
            (page({"ABC": [{**bar(), "t": "2025-10-29T04:00:00.000000001Z"}]}),),
        )
        for packets in invalid_packets:
            with self.subTest(pages=packets), self.assertRaisesRegex(ProviderError, "out-of-scope|unrequested"):
                self.run_pages(inputs, *packets)

    def test_pagination_loop_and_cap_abort(self):
        inputs = plan(alias("A", "ABC"))
        with self.assertRaisesRegex(ProviderError, "Repeated Alpaca pagination token"):
            self.run_pages(inputs, page({}, "loop"), page({}, "loop"))
        with self.assertRaisesRegex(ProviderError, "request budget"):
            self.run_pages(inputs, page({"ABC": [bar()]}, "continue"), max_requests=1)

    def test_cli_requires_opt_in_and_writes_verified_artifact_to_new_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, output = root / "plan.json", root / "alpaca_bars.json"
            plan_path.write_bytes(json.dumps(plan(alias("A", "ABC"))).encode("utf-8"))
            args = ["alpaca_bars", "--plan", str(plan_path), "--settings-file", str(root / "fixture.env"),
                    "--output", str(output), "--max-requests", "1"]
            with patch("sys.argv", args), patch.object(alpaca_bars, "read_env") as settings:
                with self.assertRaises(SystemExit):
                    alpaca_bars.main()
                settings.assert_not_called()
            with (patch("sys.argv", [*args, "--allow-provider-io"]),
                  patch.object(alpaca_bars, "read_env", return_value={"EDGELAB_NEWS_KEY": "fixture",
                                                                    "EDGELAB_NEWS_SECRET": "only"}),
                  patch.object(alpaca_bars, "get_market_data", return_value=page({"ABC": [bar()]})) as fetch):
                alpaca_bars.main()
                with self.assertRaises(SystemExit):
                    alpaca_bars.main()
                self.assertEqual(fetch.call_count, 1)
            stored = json.loads(output.read_bytes())
            self.assertEqual(stored["bars"]["A"]["2025-10-29"]["volume"], 120_000)
            self.assertNotIn("APCA-API", output.read_text(encoding="utf-8"))
