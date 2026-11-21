"""Validate and rank a local, evidence-only pair screening snapshot."""

from math import isfinite
from pathlib import Path
import re

from .history import _read
from .providers import _parse_json


STOCK = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
FIAT = set("AUD CAD CHF CNY EUR GBP HKD JPY MXN NOK NZD SEK SGD USD".split())
CRYPTO = set("AVAX BTC ETH HYPE NEAR PEPE SOL XMR XRP ZEC".split())
KINDS = {"stock-stock", "stock-stock-adjusted", "fx-crypto"}
STATUSES = {"continue", "stop", "unresolved", "revise", "data_blocked"}


def _p(value):
    return value is None or (type(value) in (int, float) and isfinite(value) and 0 <= value <= 1)


def _pair(kind, pair):
    if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(item, str) for item in pair):
        return False
    a, b = pair
    if a == b:
        return False
    if kind in {"stock-stock", "stock-stock-adjusted"}:
        return bool(STOCK.fullmatch(a) and STOCK.fullmatch(b))
    return (bool(re.fullmatch(r"[A-Z]{3}/USD", a)) and a[:3] in FIAT - {"USD"}
            and b.endswith("/USD") and b[:-4] in CRYPTO)


def rank_snapshot(data):
    if (not isinstance(data, dict) or set(data) != {"schema", "groups"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or not isinstance(data["groups"], list) or len(data["groups"]) > len(KINDS)):
        raise ValueError("Invalid leaderboard snapshot")
    groups, kinds = [], set()
    for group in data["groups"]:
        required = {"kind", "label", "possible", "complete_history", "rows"}
        if not isinstance(group, dict) or set(group) not in (required, required | {"screened"}):
            raise ValueError("Invalid leaderboard group")
        kind, possible, complete = group["kind"], group["possible"], group["complete_history"]
        label, rows = group["label"], group["rows"]
        if (not isinstance(kind, str) or kind not in KINDS or kind in kinds
                or not isinstance(label, str) or not 1 <= len(label) <= 160
                or not label.isprintable() or type(possible) is not int or type(complete) is not int
                or not isinstance(rows, list) or not 0 <= complete <= possible <= 10_000_000
                or len(rows) > min(possible, 250)):
            raise ValueError("Invalid leaderboard coverage")
        screened = group.get("screened")
        if "screened" in group and (kind != "stock-stock-adjusted" or type(screened) is not int
                                    or not len(rows) <= screened <= complete):
            raise ValueError("Invalid full-screen coverage")
        kinds.add(kind)
        seen, checked = set(), []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"symbols", "status", "p", "holm_p", "stability", "reason"}:
                raise ValueError("Invalid leaderboard row")
            symbols, status = row["symbols"], row["status"]
            p, holm, stability, reason = row["p"], row["holm_p"], row["stability"], row["reason"]
            if (not _pair(kind, symbols) or frozenset(symbols) in seen
                    or not isinstance(status, str) or status not in STATUSES
                    or not _p(p) or not _p(holm) or (p is None) != (holm is None)
                    or (p is not None and holm < p)
                    or stability not in (None, "break_detected", "no_break_detected")
                    or not isinstance(reason, str) or not 1 <= len(reason) <= 240 or not reason.isprintable()
                    or (status == "data_blocked" and (p is not None or stability is not None))):
                raise ValueError("Invalid or duplicate leaderboard pair evidence")
            seen.add(frozenset(symbols))
            checked.append(row)
        measured = sorted((row for row in checked if row["p"] is not None),
                          key=lambda row: (row["holm_p"], row["p"], row["symbols"]))
        unranked = sorted((row for row in checked if row["p"] is None), key=lambda row: row["symbols"])
        groups.append({**group,
                       "unlisted": possible - (len(checked) if screened is None else screened),
                       "data_blocked": sum(row["status"] == "data_blocked" for row in checked),
                       "rows": [{**row, "rank": i} for i, row in enumerate(measured, 1)]
                               + [{**row, "rank": None} for row in unranked]})
    return {"schema": 1, "groups": groups}


def load_leaderboard(path: Path):
    return rank_snapshot(_parse_json(_read(path, 256 * 1024)))
