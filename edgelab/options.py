"""Fetch one bounded page of Alpaca options snapshots per underlying."""

import argparse
from datetime import date, datetime, timedelta, timezone
import math
import re
from urllib.parse import quote, urlencode

from .news import NEWS_PATH, ROOT, alpaca_headers, cache, check_symbols, get_market_data, timestamp, utc_now
from .providers import ProviderError, read_env


OPTIONS_PATH = NEWS_PATH.with_name("market-options.json")
ENDPOINT = "https://data.alpaca.markets/v1beta1/options/snapshots"
CONTRACT = re.compile(r"[A-Z0-9.]{1,6}[0-9]{6}[CP][0-9]{8}")
PAGE_LIMIT = 20
FEEDS = ("indicative", "opra")


def number(value, *, nonnegative=False):
    if value is None:
        return None
    if type(value) not in (float, int) or not math.isfinite(value) or (nonnegative and value < 0):
        raise ValueError("Invalid numeric option observation")
    return value


def contract_snapshot(symbol: str, raw: dict) -> dict:
    if not CONTRACT.fullmatch(symbol) or not isinstance(raw, dict):
        raise ValueError("Invalid option contract")
    quote_row, trade, greeks = (raw.get(field) for field in ("latestQuote", "latestTrade", "greeks"))
    for row in (quote_row, trade, greeks):
        if row is not None and not isinstance(row, dict):
            raise ValueError("Invalid option fields")
    quote_row, trade, greeks = quote_row or {}, trade or {}, greeks or {}
    return {"contract": symbol, "bid": number(quote_row.get("bp"), nonnegative=True),
            "ask": number(quote_row.get("ap"), nonnegative=True),
            "quote_at": timestamp(quote_row["t"]) if quote_row.get("t") else None,
            "last": number(trade.get("p"), nonnegative=True),
            "trade_at": timestamp(trade["t"]) if trade.get("t") else None,
            "implied_volatility": number(raw.get("impliedVolatility"), nonnegative=True),
            "greeks": {key: number(greeks.get(key)) for key in ("delta", "gamma", "theta", "vega", "rho")}}


def fetch_options(settings: dict, symbols: tuple[str, ...], *, feed: str = "indicative",
                  start: date, end: date, transport=get_market_data) -> dict:
    check_symbols(symbols, 4)
    if feed not in FEEDS or not timedelta(0) <= end - start <= timedelta(days=30):
        raise ValueError("Choose an allowed feed and an expiry range of up to 30 days")
    headers = alpaca_headers(settings)
    query = urlencode({"feed": feed, "limit": PAGE_LIMIT, "expiration_date_gte": start,
                       "expiration_date_lte": end})
    chains = {}
    for symbol in symbols:
        reply = transport(f"{ENDPOINT}/{quote(symbol, safe='')}?{query}", headers)
        try:
            rows, token = reply["snapshots"], reply.get("next_page_token")
            if not isinstance(rows, dict) or len(rows) > PAGE_LIMIT or (token is not None and not isinstance(token, str)):
                raise ValueError
            chains[symbol] = {"more_available": bool(token),
                              "contracts": [contract_snapshot(name, row) for name, row in sorted(rows.items())]}
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
            raise ProviderError("Alpaca returned an invalid options snapshot") from None
    return {"origin": "alpaca", "ingested_at": utc_now(), "feed": feed,
            "expiration_start": str(start), "expiration_end": str(end), "chains": chains}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--allow-provider-io", action="store_true")
    parser.add_argument("--feed", choices=FEEDS, default="indicative")
    parser.add_argument("--expiration", type=date.fromisoformat, help="Fetch one specific YYYY-MM-DD expiration")
    args = parser.parse_args()
    if not args.allow_provider_io:
        parser.error("Requires --allow-provider-io")
    today = datetime.now(timezone.utc).date()
    start, end = (args.expiration, args.expiration) if args.expiration else (today, today + timedelta(days=14))
    try:
        snapshot = fetch_options(read_env(ROOT / ".env"), tuple(s.upper() for s in args.symbols),
                                 feed=args.feed, start=start, end=end)
        cache(OPTIONS_PATH, snapshot)
        counts = ", ".join(f"{symbol}={len(chain['contracts'])}" for symbol, chain in snapshot["chains"].items())
        print(f"Cached {args.feed} option snapshots ({counts}) at {snapshot['ingested_at']}")
    except (OSError, ProviderError, ValueError) as error:
        parser.exit(2, f"Options ingestion stopped: {error}\n")


if __name__ == "__main__":
    main()
