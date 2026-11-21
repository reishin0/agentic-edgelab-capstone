from datetime import date
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from edgelab.options import OPTIONS_PATH, fetch_options
from edgelab.providers import ProviderError


START = date(2026, 11, 10)
END = date(2026, 11, 24)
KEYS = {"EDGELAB_NEWS_KEY": "fixture-key", "EDGELAB_NEWS_SECRET": "fixture-secret"}


class OptionCacheTests(unittest.TestCase):
    def test_only_one_allowlisted_data_request_per_underlying(self):
        response = {"next_page_token": "never-cached", "snapshots": {
            "SPY261120C00500000": {
                "latestQuote": {"bp": 1.25, "ap": 1.27, "t": "2026-11-10T12:00:00Z"},
                "latestTrade": {"p": 1.26, "t": "2026-11-10T11:59:00Z"},
                "impliedVolatility": .2, "greeks": {"delta": .5}, "unknown": "never-cached",
            }}}
        transport = Mock(side_effect=[response, {"snapshots": {}}])
        data = fetch_options(KEYS, ("SPY", "QQQ"), start=START, end=END, transport=transport)
        self.assertEqual(transport.call_count, 2)
        for symbol, call in zip(("SPY", "QQQ"), transport.call_args_list):
            uri = urlsplit(call.args[0])
            self.assertEqual(uri.netloc, "data.alpaca.markets")
            self.assertEqual(uri.path, f"/v1beta1/options/snapshots/{symbol}")
            self.assertEqual(parse_qs(uri.query)["feed"], ["indicative"])
            self.assertNotIn("page_token", parse_qs(uri.query))
        self.assertEqual(data["chains"]["SPY"]["contracts"][0]["bid"], 1.25)
        self.assertTrue(data["chains"]["SPY"]["more_available"])
        self.assertEqual(data["chains"]["QQQ"]["contracts"], [])
        self.assertNotIn("never-cached", json.dumps(data))
        self.assertNotIn("fixture-secret", json.dumps(data))
        self.assertEqual(OPTIONS_PATH.parent.name, ".cache")

    def test_missing_credentials_and_malformed_snapshot_fail_closed(self):
        transport = Mock(return_value={"snapshots": {"SPY261120C00500000": {"latestQuote": {"ap": -1}}}})
        with self.assertRaises(ProviderError):
            fetch_options({}, ("SPY",), start=START, end=END, transport=transport)
        transport.assert_not_called()
        with self.assertRaises(ProviderError):
            fetch_options(KEYS, ("SPY", "QQQ"), start=START, end=END, transport=transport)
        transport.assert_called_once()
