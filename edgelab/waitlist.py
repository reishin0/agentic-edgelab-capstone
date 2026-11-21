"""Build a local candidate waitlist from an official IWB holdings export."""

import argparse
from collections import Counter
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from .history import _read
from .news import NEWS_PATH, SYMBOL
from .providers import _parse_json


SOURCE = "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/latest-holdings.csv"
WAITLIST_CACHE = NEWS_PATH.parent / "waitlists"
WAITLIST_COUNT = 1000
EXCHANGES = frozenset({"NASDAQ", "NYSE", "CBOE BZX", "NYSE MKT LLC", "NYSE AMERICAN", "NYSE ARCA", "TXE"})
HEADER = {"Ticker", "Sector", "Asset Class", "Market Value", "Exchange", "Market Currency"}


def parse_holdings(raw: bytes, *, target: int = WAITLIST_COUNT) -> dict:
    rows = csv.reader(StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    first = next(rows, [])
    dated = next(rows, [])
    if first != ["iShares Russell 1000 ETF"] or len(dated) != 2 or dated[0] != "Fund Holdings as of":
        raise ValueError("Expected the official iShares Russell 1000 ETF holdings export")
    as_of = datetime.strptime(dated[1], "%b %d, %Y").date().isoformat()
    header = next((row for row in rows if row and row[0] == "Ticker"), [])
    if not HEADER <= set(header) or len(set(header)) != len(header):
        raise ValueError("Missing or ambiguous holdings columns")
    eligible, excluded = {}, Counter()
    for row in rows:
        if not any(value.strip() for value in row):
            break
        if len(row) != len(header):
            raise ValueError("Malformed holdings row")
        item = dict(zip(header, row))
        if item["Asset Class"] != "Equity" or item.get("Type", "EQUITY") != "EQUITY":
            excluded["non_equity"] += 1
            continue
        exchange = item["Exchange"].upper()
        if exchange not in EXCHANGES or item["Market Currency"] != "USD":
            excluded["outside_listed_usd"] += 1
            continue
        raw_symbol = item["Ticker"].strip().upper()
        symbol = raw_symbol.replace(" ", ".")
        if not SYMBOL.fullmatch(symbol):
            excluded["invalid_symbol"] += 1
            continue
        try:
            value = Decimal(item["Market Value"].replace(",", ""))
        except InvalidOperation:
            raise ValueError("Invalid fund holding market value") from None
        if not value.is_finite() or value <= 0:
            excluded["nonpositive_market_value"] += 1
            continue
        if symbol in eligible:
            raise ValueError("Ambiguous duplicate ticker after normalization")
        eligible[symbol] = (value, item["Sector"], exchange)
    if not 2 <= target <= WAITLIST_COUNT or len(eligible) < target:
        raise ValueError(f"Only {len(eligible)} eligible equities; cannot fill the requested waitlist")
    selected = sorted(eligible, key=lambda symbol: (-eligible[symbol][0], symbol))[:target]
    return {"schema": "edgelab-waitlist-v1", "as_of": as_of, "fund": "iShares Russell 1000 ETF (IWB)",
            "source": SOURCE, "source_sha256": sha256(raw).hexdigest(), "eligible": len(eligible),
            "exclusions": dict(excluded), "status": "not tested",
            "selection": "US-listed USD equities ranked by positive fund holding market value; ties by ticker",
            "entries": [{"rank": rank, "symbol": symbol, "sector": eligible[symbol][1],
                         "exchange": eligible[symbol][2]} for rank, symbol in enumerate(selected, 1)]}


def load_waitlist(path: Path) -> dict:
    doc = _parse_json(_read(path, 256 * 1024))
    fields = {"schema", "as_of", "fund", "source", "source_sha256", "eligible", "exclusions",
              "status", "selection", "entries"}
    if (not isinstance(doc, dict) or set(doc) != fields or doc["schema"] != "edgelab-waitlist-v1"
            or doc["source"] != SOURCE or doc["status"] != "not tested"
            or type(doc["eligible"]) is not int or not re.fullmatch(r"[0-9a-f]{64}", doc["source_sha256"])):
        raise ValueError("Invalid waitlist metadata")
    if date.fromisoformat(doc["as_of"]).isoformat() != doc["as_of"]:
        raise ValueError("Invalid waitlist date")
    entries = doc["entries"]
    if not isinstance(entries, list) or not 2 <= len(entries) <= min(WAITLIST_COUNT, doc["eligible"]):
        raise ValueError("Invalid waitlist size")
    seen = set()
    for rank, item in enumerate(entries, 1):
        if (not isinstance(item, dict) or set(item) != {"rank", "symbol", "sector", "exchange"}
                or type(item["rank"]) is not int or item["rank"] != rank
                or not isinstance(item["symbol"], str) or not SYMBOL.fullmatch(item["symbol"])
                or not isinstance(item["sector"], str) or item["exchange"] not in EXCHANGES
                or item["symbol"] in seen):
            raise ValueError("Invalid waitlist entry")
        seen.add(item["symbol"])
    return doc


def save_waitlist(parent: Path, doc: dict) -> Path:
    data = (json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("ascii")
    tickers = ("\n".join(item["symbol"] for item in doc["entries"]) + "\n").encode("ascii")
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / ("iwb-" + doc["as_of"] + "-" + sha256(data).hexdigest()[:12])
    if not final.exists():
        with TemporaryDirectory(prefix=".stage-", dir=parent) as temporary:
            pending = Path(temporary) / "waitlist"
            pending.mkdir()
            (pending / "waitlist.json").write_bytes(data)
            (pending / "tickers.txt").write_bytes(tickers)
            load_waitlist(pending / "waitlist.json")
            try:
                pending.rename(final)
            except OSError:
                if not final.is_dir():
                    raise
    if ((final / "waitlist.json").read_bytes() != data or (final / "tickers.txt").read_bytes() != tickers):
        raise ValueError("Existing waitlist differs; preserved unchanged")
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("holdings", type=Path, help="Downloaded official iShares IWB holdings CSV")
    args = parser.parse_args()
    try:
        doc = parse_holdings(_read(args.holdings, 1024 * 1024))
        folder = save_waitlist(WAITLIST_CACHE, doc)
    except (OSError, ValueError, TypeError, csv.Error) as error:
        parser.exit(2, f"Waitlist: {error}\n")
    n = len(doc["entries"])
    print(json.dumps({"waitlist": str(folder / "waitlist.json"), "tickers": str(folder / "tickers.txt"),
                      "as_of": doc["as_of"], "count": n, "eligible": doc["eligible"],
                      "possible_pairs": n * (n - 1) // 2, "status": doc["status"]}, indent=2))


if __name__ == "__main__":
    main()
