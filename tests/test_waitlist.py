import csv
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from edgelab.waitlist import load_waitlist, parse_holdings, save_waitlist


def holdings(*extra):
    out = StringIO()
    writer = csv.writer(out)
    writer.writerows([
        ["iShares Russell 1000 ETF"], ["Fund Holdings as of", "Nov 06, 2026"], [],
        ["Ticker", "Type", "Sector", "Asset Class", "Market Value", "Exchange", "Market Currency"],
        ["AAA", "EQUITY", "Technology", "Equity", "1,000", "NASDAQ", "USD"],
        ["BRK B", "EQUITY", "Financials", "Equity", "2,000", "NYSE", "USD"],
        ["CCC", "EQUITY", "Health Care", "Equity", "900", "NYSE", "USD"],
        ["DDD", "WARRANT", "Financials", "Equity", "5,000", "NYSE", "USD"],
        ["EEE", "EQUITY", "Real Estate", "Equity", "9,000", "NO MARKET (E.G. UNLISTED)", "USD"],
        ["USD", "CASH", "Cash", "Cash", "99,000", "-", "USD"],
        *extra, [],
    ])
    return out.getvalue().encode("utf-8")


class WaitlistTests(unittest.TestCase):
    def test_stock_selection_source_and_freeze(self):
        result = parse_holdings(holdings(), target=2)
        self.assertEqual(result["as_of"], "2026-11-06")
        self.assertEqual(result["eligible"], 3)
        self.assertEqual([row["symbol"] for row in result["entries"]], ["BRK.B", "AAA"])
        self.assertEqual(result["exclusions"], {"non_equity": 2, "outside_listed_usd": 1})
        self.assertEqual(result["status"], "not tested")
        with TemporaryDirectory() as directory:
            folder = save_waitlist(Path(directory), result)
            self.assertEqual(load_waitlist(folder / "waitlist.json"), result)
            self.assertEqual((folder / "tickers.txt").read_text().splitlines(), ["BRK.B", "AAA"])
            self.assertEqual(save_waitlist(Path(directory), result), folder)

    def test_no_padding_and_no_ambiguous_ticker(self):
        with self.assertRaisesRegex(ValueError, "Only 3 eligible"):
            parse_holdings(holdings(), target=4)
        with self.assertRaisesRegex(ValueError, "duplicate ticker"):
            parse_holdings(holdings(["BRK.B", "EQUITY", "Financials", "Equity", "1", "NYSE", "USD"]), target=2)
