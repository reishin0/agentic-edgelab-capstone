"""Freeze paired Alpaca daily closes for a single approved in-sample window."""

import argparse
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .history import FrozenHistory
from .news import NEWS_PATH, ROOT, alpaca_headers, check_symbols, get_market_data, timestamp
from .providers import ProviderError, read_env
from .stat_tools import MIN_SAMPLE, Window, canonical_day


ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
HISTORY_CACHE = NEWS_PATH.parent / "history"
CALENDAR = "NYSE Arca 2025: published NYSE Group holidays plus January 9 closure"
CLOSED_2025 = frozenset({
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
})
MAX_SESSIONS = 250


def calendar_2025(start: date, end: date) -> tuple[str, ...]:
    if start.year != 2025 or end.year != 2025 or start > end:
        raise ValueError("The bundled, reviewed daily session calendar covers 2025 only")
    days = (start + timedelta(days=i) for i in range((end - start).days + 1))
    return tuple(str(day) for day in days if day.weekday() < 5 and str(day) not in CLOSED_2025)


def freeze_pair(settings: dict, symbols: tuple[str, str], start: date, end: date,
                *, feed: str, transport=get_market_data) -> tuple[dict, bytes]:
    check_symbols(symbols, 2)
    if len(symbols) != 2 or feed not in {"iex", "sip"}:
        raise ValueError("Choose two distinct tickers and an explicit supported feed")
    sessions = calendar_2025(start, end)
    later = calendar_2025(end, date(2025, 12, 31))[1:] if str(end) in sessions else ()
    if (not MIN_SAMPLE <= len(sessions) <= MAX_SESSIONS or not later
            or sessions[0] != str(start) or sessions[-1] != str(end)):
        raise ValueError("Require 80-250 complete sessions and a later holdout boundary in 2025")
    eastern = ZoneInfo("America/New_York")
    query = urlencode({"symbols": ",".join(symbols), "timeframe": "1Day",
                       "start": datetime.combine(start, time.min, eastern).isoformat(),
                       "end": datetime.combine(end, time(23, 59, 59), eastern).isoformat(),
                       "feed": feed, "adjustment": "raw", "asof": str(end),
                       "sort": "asc", "limit": 2 * MAX_SESSIONS})
    payload = transport(f"{ENDPOINT}?{query}", alpaca_headers(settings))
    if payload.get("next_page_token"):
        raise ProviderError("Historical bar page was incomplete; no pagination or fallback was used")
    bars = payload.get("bars")
    if not isinstance(bars, dict) or set(bars) != set(symbols):
        raise ProviderError("Alpaca did not return both requested series")
    columns = {}
    for symbol in symbols:
        rows = bars[symbol]
        if not isinstance(rows, list) or len(rows) > MAX_SESSIONS:
            raise ProviderError("Invalid daily-bar response")
        daily = {}
        for bar in rows:
            try:
                anchor = datetime.fromisoformat(timestamp(bar["t"])).astimezone(eastern)
                day, close = str(anchor.date()), bar["c"]
                if (anchor.time() != time.min or day in daily or type(close) not in (float, int)
                        or not math.isfinite(close) or close <= 0):
                    raise ValueError
                daily[day] = close
            except (KeyError, OverflowError, TypeError, ValueError):
                raise ProviderError("Invalid or duplicate daily bar") from None
        missing = set(sessions) - daily.keys()
        extra = daily.keys() - set(sessions)
        if missing or extra:
            raise ProviderError(f"{symbol} calendar mismatch: {len(missing)} missing, {len(extra)} extra; no fill used")
        columns[symbol] = daily
    a, b = symbols
    csv = ("day,a,b\n" + "".join(f"{day},{columns[a][day]},{columns[b][day]}\n" for day in sessions)).encode("ascii")
    window = Window(str(start), str(end), str(end), later[0])
    manifest = {"schema_version": 1, "classification": "public",
                "source": f"Alpaca {feed.upper()} daily raw USD closes; A={a}, B={b}; local use only",
                "calendar": CALENDAR, "file": "paired.csv", "sha256": sha256(csv).hexdigest(),
                "sessions": list(sessions), "window": asdict(window)}
    return manifest, csv


def store_history(parent: Path, manifest: dict, csv: bytes) -> Path:
    raw = (json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / sha256(raw).hexdigest()
    if not final.exists():
        with TemporaryDirectory(prefix=".stage-", dir=parent) as temp:
            pending = Path(temp) / "sample"
            pending.mkdir()
            (pending / "paired.csv").write_bytes(csv)
            path = pending / "manifest.json"
            path.write_bytes(raw)
            FrozenHistory.open(path).sample()
            try:
                pending.rename(final)
            except OSError:
                if not final.is_dir():
                    raise
    path = final / "manifest.json"
    saved = FrozenHistory.open(path)
    if saved.manifest_sha256 != final.name:
        raise ValueError("Existing cache does not match its content address")
    saved.sample()
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs=2)
    parser.add_argument("--start", required=True, type=canonical_day)
    parser.add_argument("--end", required=True, type=canonical_day)
    parser.add_argument("--feed", choices=("iex", "sip"), default="iex")
    parser.add_argument("--allow-provider-io", action="store_true")
    args = parser.parse_args()
    if not args.allow_provider_io:
        parser.error("Requires --allow-provider-io")
    try:
        manifest, csv = freeze_pair(read_env(ROOT / ".env"), tuple(s.upper() for s in args.symbols),
                                    args.start, args.end, feed=args.feed)
        path = store_history(HISTORY_CACHE, manifest, csv)
    except (OSError, ValueError, ZoneInfoNotFoundError, ProviderError) as error:
        parser.exit(2, f"Market history: {error}\n")
    print(json.dumps({"manifest": str(path.relative_to(ROOT)), "sample_sha256": manifest["sha256"],
                      "observations": len(manifest["sessions"]), "window": manifest["window"],
                      "source": manifest["source"]}, indent=2))


if __name__ == "__main__":
    main()
