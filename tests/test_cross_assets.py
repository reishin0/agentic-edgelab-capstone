from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from edgelab.cross_assets import (
    BIS, BIS_BOOK, KRAKEN, KRAKEN_PAIRS, KRAKEN_TICKER, crypto_rows, load_snapshot, save_snapshot,
)


class CrossAssetTests(unittest.TestCase):
    def test_ranks_online_unpegged_usd_markets_and_keeps_margin_separate(self):
        pairs, tickers = {}, {}
        markets = ["XBT/USD", "ETH/USD", "XRP/USD", "SOL/USD", "ZEC/USD", "XMR/USD", "HYPE/USD",
                   "NEAR/USD", "PEPE/USD", "LINK/USD", "ADA/USD", "ETH/EUR", "USDT/USD", "EUR/USD"]
        for n, name in enumerate(markets, 1):
            pairs[name] = {"wsname": name, "status": "online", "aclass_base": "currency",
                           "leverage_sell": [] if name == "ETH/USD" else [2]}
            tickers[name] = {"v": ["0", str(n)], "p": ["0", "2"]}
        tickers["XBT/USD"]["p"][1] = "1000"
        pairs["ADA/USD"]["status"] = "cancel_only"
        encode = lambda data: json.dumps({"error": [], "result": data}).encode()
        rows = crypto_rows(encode(pairs), encode(tickers))
        self.assertEqual(rows[0]["symbol"], "BTC/USD")
        self.assertEqual(rows[0]["turnover_usd"], 1000)
        self.assertEqual(len(rows), 10)
        self.assertFalse(next(row["sell_margin_listed"] for row in rows if row["symbol"] == "ETH/USD"))
        self.assertFalse(set(markets[-4:]) & {row["symbol"] for row in rows})

    def test_freezes_separate_snapshot_and_rejects_tampered_rank(self):
        def group(source, inputs, as_of, keys, sell):
            return {"source": source, "inputs": {key: "0" * 64 for key in inputs}, "as_of": as_of,
                    "metric": "source-specific metric",
                    "entries": [{"rank": rank, "symbol": key, "turnover_usd": 1000 - rank,
                                 "sell_margin_listed": sell} for rank, key in enumerate(keys, 1)]}
        fx = [f"USD/{key}" for key in "EUR JPY CHF GBP MXN SGD CNY CAD HKD AUD".split()]
        crypto = [f"{key}/USD" for key in "BTC ETH XRP SOL ADA ZEC XMR AVAX HYPE NEAR".split()]
        snapshot = {"schema": "edgelab-cross-assets-v1", "status": "not tested",
                    "fx": group(BIS, [BIS_BOOK], "2025-04", fx, None),
                    "crypto": group(KRAKEN, [KRAKEN_PAIRS, KRAKEN_TICKER], "2026-11-10T00:00:00+00:00", crypto, True)}
        with TemporaryDirectory() as folder:
            path = save_snapshot(Path(folder), snapshot)
            self.assertEqual(load_snapshot(path), snapshot)
            damaged = deepcopy(snapshot)
            damaged["fx"]["entries"][0]["turnover_usd"] = 10
            with self.assertRaisesRegex(ValueError, "ranked instrument"):
                save_snapshot(Path(folder), damaged)
