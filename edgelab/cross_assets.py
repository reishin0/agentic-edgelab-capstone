"""Freeze independent FX and crypto research queues from public data."""

import argparse
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .history import _read
from .news import timestamp
from .providers import _parse_json
from .waitlist import WAITLIST_CACHE


BIS = "https://www.bis.org/publications/triennial-survey/rpfx25-fx-annex-2606.pdf"
KRAKEN = "https://docs.kraken.com/api-reference/market-data/get-ticker-information"
BIS_BOOK = "https://www.bis.org/publications/triennial-survey/rpfx25-fx-annex-tables.xlsx"
KRAKEN_PAIRS = "https://api.kraken.com/0/public/AssetPairs"
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"
NAMES = frozenset("BTC ETH XRP ZEC SOL AVAX HYPE NEAR PEPE XMR SUI TAO ADA ONDO XLM LINK LTC DOGE "
                  "UNI JUP ENA QNT AAVE BCH DOT ATOM ETC ICP FIL TON BNB APT ARB OP TRX HBAR KAS".split())
MARKET = re.compile(r"[A-Z0-9]{2,12}/[A-Z]{3}")
LIMIT = 10
MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def fx_rows(raw: bytes) -> list[dict]:
    with ZipFile(BytesIO(raw)) as book:
        def xml(name: str):
            if book.getinfo(name).file_size > 3_000_000:
                raise ValueError("Unexpectedly large BIS XML member")
            return ET.fromstring(book.read(name))

        strings = ["".join(item.itertext()) for item in xml("xl/sharedStrings.xml")]
        relationships = {row.attrib["Id"]: row.attrib["Target"] for row in xml("xl/_rels/workbook.xml.rels")}
        sheets = {sheet.attrib["name"]: sheet.attrib[REL] for sheet in xml("xl/workbook.xml").find(MAIN + "sheets")}
        currencies = {}
        for i in range(1, 5):
            sheet = f"T_03_{i:02}"
            target = relationships[sheets[sheet]]
            if not re.fullmatch(r"worksheets/sheet[0-9]+\.xml", target):
                raise ValueError("Unexpected BIS worksheet location")
            cells = {}
            for cell in xml(str(PurePosixPath("xl") / target)).iter(MAIN + "c"):
                ref, value = cell.get("r"), cell.findtext(MAIN + "v")
                if value is not None and ref and re.fullmatch(r"[A-Z]+[2-5]", ref):
                    cells[ref] = strings[int(value)] if cell.get("t") == "s" else value
            if (cells.get("B2") != "US dollar against:" or "April 2025" not in cells.get("B3", "") or
                    "Spot" not in (cells.get("B5"), cells.get("I5"))):
                raise ValueError("Unexpected BIS table scope")
            for ref, code in cells.items():
                if ref.endswith("4") and re.fullmatch(r"[A-Z]{3}", code) and code != "TOT":
                    number = Decimal(cells[ref[:-1] + "5"])
                    if not number.is_finite() or number <= 0 or code in currencies:
                        raise ValueError("Invalid currency turnover")
                    currencies[code] = number
        if len(currencies) < LIMIT:
            raise ValueError("Incomplete BIS coverage")
    ranked = sorted(currencies, key=lambda code: (-currencies[code], code))[:LIMIT]
    return [{"rank": rank, "symbol": f"{code}/USD" if code in {"EUR", "GBP", "AUD", "NZD"} else f"USD/{code}",
             "turnover_usd": int(currencies[code] * 1_000_000), "sell_margin_listed": None}
            for rank, code in enumerate(ranked, 1)]


def crypto_rows(pair_data: bytes, ticker_data: bytes) -> list[dict]:
    pairs, tickers = _parse_json(pair_data), _parse_json(ticker_data)
    if any(not isinstance(doc, dict) or doc.get("error") != [] or not isinstance(doc.get("result"), dict)
           for doc in (pairs, tickers)):
        raise ValueError("Invalid Kraken public response")
    rows = {}
    for code, market in pairs["result"].items():
        if not isinstance(market, dict):
            raise ValueError("Invalid Kraken market definition")
        if (market.get("status") != "online" or market.get("aclass_base") != "currency" or
                not market.get("wsname", "").endswith("/USD")):
            continue
        base = market["wsname"].split("/")[0]
        base = {"XBT": "BTC", "XDG": "DOGE"}.get(base, base)
        if base not in NAMES or code not in tickers["result"]:
            continue
        stat = tickers["result"][code]
        turnover = Decimal(stat["v"][1]) * Decimal(stat["p"][1])
        leverage_sell = market.get("leverage_sell")
        if not turnover.is_finite() or turnover <= 0 or not isinstance(leverage_sell, list) or base in rows:
            raise ValueError("Invalid or duplicate Kraken market data")
        rows[base] = (turnover, bool(leverage_sell))
    if len(rows) < LIMIT:
        raise ValueError("Incomplete Kraken coverage")
    ranked = sorted(rows, key=lambda name: (-rows[name][0], name))[:LIMIT]
    return [{"rank": rank, "symbol": name + "/USD", "turnover_usd": int(rows[name][0]),
             "sell_margin_listed": rows[name][1]} for rank, name in enumerate(ranked, 1)]


def create_snapshot(book: bytes, pairs: bytes, tickers: bytes, observed_at: str) -> dict:
    return {"schema": "edgelab-cross-assets-v1", "status": "not tested",
            "fx": {"as_of": "2025-04", "source": BIS, "metric": "Average daily spot turnover (USD)",
                   "entries": fx_rows(book), "inputs": {BIS_BOOK: sha256(book).hexdigest()}},
            "crypto": {"as_of": timestamp(observed_at), "source": KRAKEN,
                       "metric": "Kraken trailing 24h turnover (USD, volume x VWAP)",
                       "entries": crypto_rows(pairs, tickers),
                       "inputs": {KRAKEN_PAIRS: sha256(pairs).hexdigest(), KRAKEN_TICKER: sha256(tickers).hexdigest()}}}


def load_snapshot(path: Path) -> dict:
    doc = _parse_json(_read(path, 32 * 1024))
    if (not isinstance(doc, dict) or set(doc) != {"schema", "status", "fx", "crypto"}
            or doc["schema"] != "edgelab-cross-assets-v1" or doc["status"] != "not tested"):
        raise ValueError("Invalid cross-asset waitlist")
    for key, source, inputs in (("fx", BIS, {BIS_BOOK}), ("crypto", KRAKEN, {KRAKEN_PAIRS, KRAKEN_TICKER})):
        group = doc[key]
        if (not isinstance(group, dict) or set(group) != {"source", "as_of", "entries", "metric", "inputs"}
                or group["source"] != source or not isinstance(group["entries"], list) or
                len(group["entries"]) != LIMIT or not isinstance(group["metric"], str)
                or not isinstance(group["as_of"], str) or not isinstance(group["inputs"], dict)
                or group["inputs"].keys() != inputs or not all(isinstance(value, str) and
                    re.fullmatch(r"[0-9a-f]{64}", value) for value in group["inputs"].values())):
            raise ValueError("Invalid waitlist category")
        if (group["as_of"] != "2025-04" if key == "fx" else timestamp(group["as_of"]) != group["as_of"]):
            raise ValueError("Invalid source observation date")
        seen, last = set(), float("inf")
        for rank, row in enumerate(group["entries"], 1):
            if (not isinstance(row, dict) or set(row) != {"rank", "symbol", "turnover_usd", "sell_margin_listed"}
                    or type(row["rank"]) is not int or row["rank"] != rank
                    or not isinstance(row["symbol"], str) or not MARKET.fullmatch(row["symbol"])
                    or row["symbol"] in seen or type(row["turnover_usd"]) is not int
                    or not 0 < row["turnover_usd"] <= last or
                    (row["sell_margin_listed"] is not None if key == "fx" else type(row["sell_margin_listed"]) is not bool)):
                raise ValueError("Invalid ranked instrument")
            last = row["turnover_usd"]
            seen.add(row["symbol"])
    return doc


def save_snapshot(parent: Path, doc: dict) -> Path:
    raw = (json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("ascii")
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / ("fx-crypto-" + sha256(raw).hexdigest()[:12] + ".json")
    if not path.exists():
        with TemporaryDirectory(prefix=".cross-", dir=parent) as folder:
            draft = Path(folder) / "snapshot.json"
            draft.write_bytes(raw)
            load_snapshot(draft)
            try:
                draft.rename(path)
            except OSError:
                if not path.is_file():
                    raise
    if path.read_bytes() != raw:
        raise ValueError("Existing waitlist differs; preserved unchanged")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bis_workbook", "kraken_pairs", "kraken_tickers"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--observed-at", required=True)
    args = parser.parse_args()
    try:
        sources = [_read(path, 2_000_000) for path in (args.bis_workbook, args.kraken_pairs, args.kraken_tickers)]
        doc = create_snapshot(*sources, observed_at=args.observed_at)
        path = save_snapshot(WAITLIST_CACHE, doc)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile, InvalidOperation, ET.ParseError) as error:
        parser.exit(2, f"Cross-asset waitlist: {error}\n")
    print(json.dumps({"waitlist": str(path), "fx": [row["symbol"] for row in doc["fx"]["entries"]],
                      "crypto": [row["symbol"] for row in doc["crypto"]["entries"]]}, indent=2))


if __name__ == "__main__":
    main()
