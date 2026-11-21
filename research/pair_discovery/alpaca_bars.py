"""Acquire only approved development sessions of raw Alpaca SIP daily bars.

The plan fixes permanent instrument IDs and certified symbol intervals. Alpaca
asof resolves a symbol identity, while start/end bound every observation. This
export carries only raw closes and reported share volume. Join independently
certified corporate actions before using these bars in the discovery screen.
"""

import argparse
from collections import defaultdict
from datetime import date, datetime, time
from hashlib import sha256
from itertools import islice
import json
from math import isfinite
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from edgelab.contracts import fingerprint
from edgelab.market_history import ENDPOINT
from edgelab.news import SYMBOL, alpaca_headers, get_market_data
from edgelab.providers import ProviderError, _parse_json, read_env

from .screen import CALIBRATION_END, HOLDOUT_START, day, development_sessions, require


PLAN_VERSION = 1
MAX_INSTRUMENTS = 2000
MAX_ALIASES = MAX_INSTRUMENTS * 5
MAX_REQUESTS = 2000
PAGE_SIZE = 1000
GROUP_SIZE = 40
REQUEST_SPACING = 0.4
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
DAILY_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.0{1,9})?(?:Z|[+-]\d{2}:\d{2})")
QUERY = {"timeframe": "1Day", "feed": "sip", "adjustment": "raw", "currency": "USD", "sort": "asc", "limit": PAGE_SIZE}


def freeze_plan(plan):
    """Validate all windows and aliases before generating any provider query."""
    require(type(plan) is dict and set(plan) == {"schema", "window", "instruments", "aliases"}
            and type(plan["schema"]) is int and plan["schema"] == PLAN_VERSION, "Invalid Alpaca acquisition plan")
    window = plan["window"]
    require(type(window) is dict and set(window) == {"start", "end"}, "Expected a frozen acquisition window")
    first, last = day(window["start"]), day(window["end"])
    calendar = sum(development_sessions(), ())
    require(first in calendar and last in calendar and first <= last <= CALIBRATION_END < HOLDOUT_START,
            "Expected only reviewed EdgeLab development sessions")
    sessions = tuple(stamp for stamp in calendar if first <= stamp <= last)
    instruments = plan["instruments"]
    require(type(instruments) is list and 1 <= len(instruments) <= MAX_INSTRUMENTS
            and all(type(key) is str and ID.fullmatch(key) for key in instruments)
            and instruments == sorted(set(instruments)), "Instrument IDs must be sorted, unique and bounded")
    aliases = plan["aliases"]
    require(type(aliases) is list and 0 <= len(aliases) <= MAX_ALIASES, "Invalid certified alias inventory")

    mapped = {key: set() for key in instruments}
    symbol_days = defaultdict(set)
    intervals = defaultdict(dict)
    for row in aliases:
        require(type(row) is dict and set(row) == {
            "instrument_id", "symbol", "effective_from", "effective_through", "known_on", "source",
        }, "Invalid symbol alias fields")
        instrument, symbol, source = row["instrument_id"], row["symbol"], row["source"]
        require(type(instrument) is str and instrument in mapped and type(symbol) is str and SYMBOL.fullmatch(symbol)
                and type(source) is str and 1 <= len(source) <= 255 and source.isprintable() and source.strip() == source,
                "Aliases require an in-scope ID, provider symbol and provenance")
        begin, end, known = (day(row[field]) for field in ("effective_from", "effective_through", "known_on"))
        require(begin <= end, "Invalid symbol alias validity interval")
        dates = tuple(stamp for stamp in sessions if begin <= stamp <= end)
        if not dates:
            continue
        require(known <= dates[0], "Symbol alias was unavailable at its first queried session")
        require(mapped[instrument].isdisjoint(dates) and symbol_days[symbol].isdisjoint(dates),
                "Overlapping instrument or symbol alias coverage")
        mapped[instrument].update(dates)
        symbol_days[symbol].update(dates)
        intervals[dates][symbol] = instrument
    return sessions, mapped, intervals


def request_groups(intervals, eastern):
    """Group symbols only when their certified session sets are identical."""
    for dates, symbols in sorted(intervals.items()):
        query = {**QUERY,
                 "start": datetime.combine(date.fromisoformat(dates[0]), time.min, eastern).isoformat(),
                 "end": datetime.combine(date.fromisoformat(dates[-1]), time(23, 59, 59), eastern).isoformat(),
                 "asof": dates[-1]}
        ordered = iter(sorted(symbols))
        while group := tuple(islice(ordered, GROUP_SIZE)):
            yield {**query, "symbols": ",".join(group)}, set(dates), {symbol: symbols[symbol] for symbol in group}


def validate_page(packet, symbols, dates, bars, eastern):
    if (type(packet) is not dict or type(packet.get("bars")) is not dict
            or "next_page_token" not in packet or set(packet["bars"]) - symbols.keys()):
        raise ProviderError("Invalid Alpaca daily-bars envelope or unrequested symbol")
    token = packet["next_page_token"]
    if token is not None and (type(token) is not str or not 1 <= len(token) <= 1024 or not token.isprintable()
                              or any(c.isspace() for c in token)):
        raise ProviderError("Invalid Alpaca pagination token")
    count = 0
    for symbol, series in packet["bars"].items():
        if type(series) is not list:
            raise ProviderError("Invalid Alpaca daily-bar series")
        count += len(series)
        if count > PAGE_SIZE:
            raise ProviderError("Alpaca page exceeded the declared bar count")
        column = bars[symbols[symbol]]
        for row in series:
            try:
                stamp, close, volume = row["t"], row["c"], row["v"]
                if not DAILY_STAMP.fullmatch(stamp):
                    raise ValueError
                local = datetime.fromisoformat(stamp).astimezone(eastern)
                stamp = local.date().isoformat()
                if (stamp not in dates or local.time() != time.min or stamp in column
                        or type(close) not in (int, float) or not isfinite(close) or close <= 0
                        or type(volume) is not int or not 0 <= volume <= (2**63 - 1)):
                    raise ValueError
            except (KeyError, TypeError, ValueError, OverflowError):
                raise ProviderError("Invalid, duplicate, or out-of-scope Alpaca daily bar") from None
            column[stamp] = {"close": close, "volume": volume}
    return token, count


def acquire(plan, headers, *, max_requests, transport=get_market_data):
    """Return a point-in-time-mapped development artifact and provenance."""
    sessions, mapped, intervals = freeze_plan(plan)
    require(type(max_requests) is int and 1 <= max_requests <= MAX_REQUESTS, "Invalid Alpaca request budget")
    eastern = ZoneInfo("America/New_York")
    groups = tuple(request_groups(intervals, eastern))
    require(len(groups) <= max_requests, "Fewer requests budgeted than required symbol groups")
    bars = {instrument: {} for instrument in plan["instruments"]}
    receipts = []
    requests = 0
    for query, dates, symbols in groups:
        group = {"query": query, "instrument_ids": symbols, "pages": []}
        receipts.append(group)
        token, seen = None, set()
        while True:
            if requests >= max_requests:
                raise ProviderError("Alpaca acquisition reached its request budget; no artifact emitted")
            params = query if token is None else {**query, "page_token": token}
            packet = transport(f"{ENDPOINT}?{urlencode(params)}", headers)
            requests += 1
            token, count = validate_page(packet, symbols, dates, bars, eastern)
            group["pages"].append({"response_fingerprint": fingerprint(packet), "bars": count})
            if token is None:
                break
            if token in seen:
                raise ProviderError("Repeated Alpaca pagination token")
            seen.add(token)

    coverage = {}
    for key, values in bars.items():
        missing_alias = [stamp for stamp in sessions if stamp not in mapped[key]]
        missing_bars = [stamp for stamp in sessions if stamp in mapped[key] and stamp not in values]
        coverage[key] = {"status": "complete" if not (missing_alias or missing_bars) else "partial" if values else "missing",
                         "observations": len(values), "missing_alias_sessions": missing_alias, "missing_bar_sessions": missing_bars}
    return {"schema": 1, "classification": "equity-development", "window": plan["window"], "sessions": list(sessions),
            "holdout_start": HOLDOUT_START, "feed": QUERY["feed"], "adjustment": QUERY["adjustment"],
            "timeframe": QUERY["timeframe"], "currency": QUERY["currency"], "bars": bars, "coverage": coverage,
            "source": {"endpoint": ENDPOINT, "plan": plan, "plan_fingerprint": fingerprint(plan),
                       "exporter_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                       "request_budget": max_requests, "request_count": requests, "requests": receipts}}


def spaced_transport():
    not_before = 0.0

    def send(url, headers):
        nonlocal not_before
        sleep(max(0, not_before - monotonic()))
        not_before = monotonic() + REQUEST_SPACING
        return get_market_data(url, headers)

    return send


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--settings-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument("--allow-provider-io", action="store_true")
    args = parser.parse_args()
    if not args.allow_provider_io:
        parser.error("Requires --allow-provider-io")
    try:
        if args.output.exists():
            raise ValueError("Choose a new acquisition output file")
        plan = _parse_json(args.plan.read_bytes())
        result = acquire(plan, alpaca_headers(read_env(args.settings_file)), max_requests=args.max_requests,
                         transport=spaced_transport())
        raw = (json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        with args.output.open("xb") as handle:
            handle.write(raw)
    except (OSError, ValueError, ProviderError, ZoneInfoNotFoundError) as error:
        parser.exit(2, f"Alpaca development bars: {error}\n")
    print(f"Saved {sum(len(rows) for rows in result['bars'].values())} development bars, SHA-256 {sha256(raw).hexdigest()}")


if __name__ == "__main__":
    main()
