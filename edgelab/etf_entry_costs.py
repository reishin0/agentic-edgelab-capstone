"""Project the pinned offline ETF entry-cost study for the local dashboard."""

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean


RESULT_SHA256 = "eb8dabe5c4476ae532a0c0b989c12216d55279f93b020322f39b592fe386fdaf"
MAX_BYTES = 128 * 1024
SYMBOLS = ("SPY", "IVV", "VOO")
SCENARIOS = ("base", "stress")


def load_etf_entry_costs(path: Path) -> dict:
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or sha256(raw).hexdigest() != RESULT_SHA256:
        raise ValueError("ETF entry-cost result integrity check failed")

    evidence = json.loads(raw)
    summary = evidence["summary"]
    sessions = evidence["sessions"]
    matched = [session for session in sessions if session["status"] == "matched"]
    rows = []
    for symbol in SYMBOLS:
        row = {"symbol": symbol, "median_spread_bps": float(summary["spread_bps"][symbol]["median"])}
        for mode in SCENARIOS:
            row[f"{mode}_mean_cost_bps"] = float(summary["scenarios"][mode][symbol]["cost_bps"]["mean"])
            row[f"{mode}_mean_cost_usd"] = float(mean(
                Decimal(session["scenarios"][mode][symbol]["cost_usd"]) for session in matched
            ))
        rows.append(row)
    return {"start_date": sessions[0]["day"], "end_date": sessions[-1]["day"],
            "budget_usd": int(evidence["capital_usd_per_hypothetical_purchase"]), "reference": "SPY",
            "matched_sessions": summary["matched_sessions"], "scheduled_sessions": summary["scheduled_sessions"],
            "clocks_checked": sum(session["clocks_examined"] for session in sessions),
            "clocks_fit": sum(session["all_orders_fit_clocks"] for session in sessions), "rows": rows}
