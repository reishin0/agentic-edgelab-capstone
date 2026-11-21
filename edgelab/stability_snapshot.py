"""Read-only projection of the pinned November 15 synthetic bootstrap study."""

import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from statistics import mean


CACHE_FILE = Path(".cache/bootstrap_all_dates_20261115/results.json")
SNAPSHOT_SHA256 = "ada09c1b8038474d84b9e1433210b70ad3fd64da62cfcfa5fb2bd6f4e96aee6d"
LEDGER_SHA256 = "a7c5c88b91ec6487257f7e987cd0924738c34da42cf768c1402b722bcad9ded1"
MAX_BYTES = 32 * 1024
LEDGER_MAX_BYTES = 512 * 1024
ARMS = ("cusum", "revised", "all_dates")
FORMATION_OBSERVATIONS = 540
LATE_SPLIT_PERCENT = 88
DATE_TOLERANCE = 10


def break_date_accuracy(path: Path) -> dict:
    try:
        with path.open("rb") as file:
            raw = file.read(LEDGER_MAX_BYTES + 1)
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError:
        return {"status": "unavailable"}
    if len(raw) > LEDGER_MAX_BYTES or sha256(raw).hexdigest() != LEDGER_SHA256:
        return {"status": "unavailable"}

    planted = FORMATION_OBSERVATIONS * LATE_SPLIT_PERCENT // 100
    columns = {"revised": "revised_date", "all_dates": "all_date"}
    cases = {name: {"scenario": name, "eligible": 0, "within_tolerance": dict.fromkeys(columns, 0)}
             for name in ("weak_0.6_88", "weak_2.4_88")}
    for row in csv.DictReader(StringIO(raw.decode("ascii"), newline="")):
        if row["stage"] != "stability" or row["scenario"] not in cases:
            continue
        case = cases[row["scenario"]]
        case["eligible"] += 1
        for arm, date in columns.items():
            if row[arm] == "break" and abs(int(row[date]) - planted) <= DATE_TOLERANCE:
                case["within_tolerance"][arm] += 1
    return {"status": "available", "tolerance_observations": DATE_TOLERANCE,
            "planted_split_percent": LATE_SPLIT_PERCENT, "scenarios": list(cases.values())}


def load_stability_snapshot(path: Path) -> dict | None:
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES or sha256(raw).hexdigest() != SNAPSHOT_SHA256:
        raise ValueError("Synthetic stability snapshot integrity check failed")

    evidence = json.loads(raw)
    rows = []
    late = []
    for name, scenario in evidence["scenarios"].items():
        row = {"scenario": name, "n": scenario["n"],
               "advanced": {arm: scenario["arms"][arm]["advanced"] for arm in ARMS}}
        rows.append(row)
        if name.rsplit("_", 1)[-1] in {"78", "88", "89"}:
            late.append((row["advanced"]["revised"] - row["advanced"]["all_dates"]) / row["n"])
    late_gain_pp = 100 * mean(late)
    return {"date": "2026-11-15", "holdout": evidence["holdout"],
            "formation_observations": FORMATION_OBSERVATIONS, "samples": sum(row["n"] for row in rows),
            "late_gain_pp": late_gain_pp, "late_target_pp": 5, "scenarios": rows,
            "break_date_accuracy": break_date_accuracy(path.with_name("trials.csv"))}
