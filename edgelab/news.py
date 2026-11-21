"""Explicit, bounded news ingestion. The research graph never reads this cache."""

import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .providers import NoRedirect, ProviderError, _parse_json, read_env


ROOT = Path(__file__).resolve().parent.parent
NEWS_CACHE = Path(".cache") / "market-news.json"
NEWS_PATH = ROOT / NEWS_CACHE
ENDPOINT = "https://data.alpaca.markets/v1beta1/news"
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
LIMIT = 20
MAX_BYTES = 400_000


def timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("News timestamps must include an offset")
    return parsed.astimezone(timezone.utc).isoformat()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def article(raw: dict) -> dict:
    if type(raw["id"]) is not int or not isinstance(raw["headline"], str) or not raw["headline"].strip():
        raise ValueError("Invalid news article")
    link = raw.get("url") or ""
    parsed = urlsplit(link)
    if (len(link) > 2000 or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(c.isspace() or c == "\\" for c in link)):
        link = ""
    symbols = raw.get("symbols") or []
    if not isinstance(symbols, list):
        raise ValueError("Invalid article symbols")
    return {"id": raw["id"], "headline": raw["headline"].strip()[:350],
            "source": str(raw.get("source") or "")[:60], "url": link,
            "created_at": timestamp(raw["created_at"]),
            "updated_at": timestamp(raw["updated_at"]),
            "symbols": [s for s in symbols[:24] if isinstance(s, str) and SYMBOL.fullmatch(s)]}


def get_market_data(url: str, headers: dict) -> dict:
    try:
        request = Request(url, headers=headers, method="GET")
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as reply:
            body = reply.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Market data response exceeded the byte limit")
        result = _parse_json(body)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result
    except HTTPError as error:
        error.close()
        raise ProviderError(f"Alpaca market data returned HTTP {error.code}") from None
    except (OSError, HTTPException, ValueError, RecursionError):
        raise ProviderError("Alpaca market data request or response failed") from None


def alpaca_headers(settings: dict) -> dict:
    key, secret = settings.get("EDGELAB_NEWS_KEY"), settings.get("EDGELAB_NEWS_SECRET")
    if not key or not secret:
        raise ProviderError("EdgeLab Alpaca market-data credentials are not configured")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def check_symbols(symbols: tuple[str, ...], maximum: int) -> None:
    if not 1 <= len(symbols) <= maximum or len(set(symbols)) != len(symbols) or not all(map(SYMBOL.fullmatch, symbols)):
        raise ValueError(f"Specify 1 to {maximum} distinct stock symbols")


def fetch(settings: dict, symbols: tuple[str, ...], transport=get_market_data) -> dict:
    headers = alpaca_headers(settings)
    check_symbols(symbols, 8)
    query = urlencode({"symbols": ",".join(symbols), "limit": LIMIT, "sort": "desc", "include_content": "false"})
    reply = transport(f"{ENDPOINT}?{query}", headers)
    try:
        rows = reply["news"]
        if not isinstance(rows, list) or len(rows) > LIMIT:
            raise ValueError
        items = {}
        for row in rows:
            clean = article(row)
            previous = items.get(clean["id"])
            if not previous or clean["updated_at"] > previous["updated_at"]:
                items[clean["id"]] = clean
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ProviderError("News response has an invalid article schema") from None
    return {"origin": "alpaca", "ingested_at": utc_now(), "symbols": list(symbols),
            "articles": sorted(items.values(), key=lambda row: row["updated_at"], reverse=True)}


def cache(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".news-", delete=False, mode="w", encoding="utf-8") as output:
            temp = Path(output.name)
            json.dump(snapshot, output, ensure_ascii=True, allow_nan=False)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--allow-provider-io", action="store_true")
    parser.add_argument("--polls", type=int, default=1)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if not args.allow_provider_io or not 1 <= args.polls <= 60 or not 30 <= args.interval <= 600:
        parser.error("Requires --allow-provider-io, 1 to 60 polls, and a 30 to 600 second interval")
    try:
        settings = read_env(ROOT / ".env")
        for i in range(args.polls):
            if i:
                sleep(args.interval)
            snapshot = fetch(settings, tuple(s.upper() for s in args.symbols))
            cache(NEWS_PATH, snapshot)
            print(f"Ingested {len(snapshot['articles'])} headlines at {snapshot['ingested_at']}", flush=True)
    except (OSError, ProviderError, ValueError) as error:
        parser.exit(2, f"News ingestion stopped: {error}\n")


if __name__ == "__main__":
    main()
