"""Screen a frozen, development-only equity pair family using EdgeLab statistics."""

import argparse
from collections import defaultdict
from datetime import date, timedelta
from hashlib import sha256
from itertools import combinations
import json
from math import isfinite, log
from pathlib import Path
from statistics import mean, median, stdev

import numpy as np

from edgelab.contracts import fingerprint
from edgelab.market_history import calendar_2025
from edgelab.research import release_fingerprint
from edgelab import stat_tools as stats


ELIGIBILITY_END = "2023-12-29"
FORMATION_START = "2024-01-02"
FORMATION_END = "2024-12-31"
CALIBRATION_START = "2025-01-02"
CALIBRATION_END = "2025-10-31"
HOLDOUT_START = "2025-11-03"
CALIBRATION_MONTHS = ("2025-08", "2025-09", "2025-10")
EARLY_CLOSE_2025 = "2025-07-03"
FORMATION_HOLIDAYS = frozenset({"2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19",
                               "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25"})
SCHEMA_VERSION = 1
PAYLOADS = {"calendar": "calendar.json", "universe": "universe.json", "bars": "bars.json"}
NUMERICAL_ERRORS = (ValueError, ArithmeticError, np.linalg.LinAlgError)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def day(value):
    require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value,
            "Expected an ISO session date")
    return value


def positive(value):
    return type(value) in (int, float) and isfinite(value) and value > 0


def load_development(manifest_path, *, pinned=None):
    """Check the scope and pinned filenames before opening any payload."""
    manifest_path = Path(manifest_path).resolve(strict=True)
    raw_manifest = manifest_path.read_bytes()
    if pinned is not None:
        require(sha256(raw_manifest).hexdigest() == pinned.get("manifest"), "Development manifest pin differs")
    manifest = json.loads(raw_manifest)
    require(isinstance(manifest, dict) and set(manifest) == {
        "schema_version", "classification", "through", "holdout_start", "files",
    }, "Invalid development manifest")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == SCHEMA_VERSION
            and manifest["classification"] in ("equity-development", "synthetic-development")
            and manifest["through"] == CALIBRATION_END and manifest["holdout_start"] == HOLDOUT_START,
            "The manifest must seal the exact development boundary")
    sources = manifest["files"]
    require(isinstance(sources, dict) and set(PAYLOADS) <= set(sources) <= {*PAYLOADS, "execution"},
            "Development manifest files do not match the input contract")
    allowed = {**PAYLOADS, "execution": "execution.json"}
    payloads = {}
    for key, record in sources.items():
        require(isinstance(record, dict) and set(record) == {"name", "sha256", "source", "through"}
                and record["name"] == allowed[key] and day(record["through"]) <= CALIBRATION_END
                and isinstance(record["source"], str) and bool(record["source"].strip()),
                f"Unsafe development input record for {key}")
        if pinned is not None:
            require(record["sha256"] == pinned.get(key), f"Development payload pin differs for {key}")
    for key, record in sources.items():
        path = manifest_path.parent / record["name"]
        require(path.resolve(strict=True) == path and path.is_file(), f"Unsafe input path for {key}")
        raw = path.read_bytes()
        require(sha256(raw).hexdigest() == record["sha256"], f"Source digest mismatch for {key}")
        payloads[key] = json.loads(raw)
    return manifest, payloads


def venue_calendar(packet):
    require(isinstance(packet, dict) and set(packet) == {"calendar", "sessions"}
            and isinstance(packet["calendar"], str) and bool(packet["calendar"].strip())
            and isinstance(packet["sessions"], list), "A named, explicit exchange calendar is required")
    sessions = tuple(day(value) for value in packet["sessions"])
    require(sessions == tuple(sorted(set(sessions))) and bool(sessions) and sessions[-1] == CALIBRATION_END,
            "Calendar must end at the fixed calibration boundary and contain sorted unique sessions")
    eligibility = tuple(value for value in sessions if value <= ELIGIBILITY_END)
    formation = tuple(value for value in sessions if FORMATION_START <= value <= FORMATION_END)
    calibration = tuple(value for value in sessions if CALIBRATION_START <= value <= CALIBRATION_END)
    require((eligibility, formation, calibration) == development_sessions()
            and len(sessions) == len(eligibility) + len(formation) + len(calibration),
            "Calendar does not match the eligibility, formation, and calibration boundaries")
    return eligibility, formation, calibration


def development_sessions():
    def weekdays(start, end, holidays):
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        days = (first + timedelta(days=index) for index in range((last - first).days + 1))
        return tuple(stamp.isoformat() for stamp in days if stamp.weekday() < 5 and stamp.isoformat() not in holidays)

    eligibility = weekdays("2023-09-01", ELIGIBILITY_END,
                           {"2023-09-04", "2023-11-23", "2023-12-25"})[-60:]
    formation = weekdays(FORMATION_START, FORMATION_END, FORMATION_HOLIDAYS)
    calibration = calendar_2025(date.fromisoformat(CALIBRATION_START), date.fromisoformat(CALIBRATION_END))
    return eligibility, formation, calibration


def load_universe(packet):
    require(isinstance(packet, dict) and set(packet) == {"index", "effective", "published", "members"}
            and packet["index"] == "IWB" and day(packet["effective"]) <= day(packet["published"]) <= ELIGIBILITY_END
            and isinstance(packet["members"], list) and bool(packet["members"]),
            "Membership needs pre-formation IWB provenance")
    members = {}
    for row in packet["members"]:
        require(isinstance(row, dict) and set(row) == {"id", "issuer", "kind", "sector", "country", "primary_listing", "known_on"}
                and all(isinstance(row[key], str) and bool(row[key].strip()) for key in row)
                and day(row["known_on"]) <= ELIGIBILITY_END and row["id"] not in members,
                "Membership rows need unique permanent IDs and contemporaneous classification")
        members[row["id"]] = row
    return members


def check_bars(packet, members, sessions):
    require(isinstance(packet, dict) and set(packet) <= set(members), "Bar instruments must belong to the frozen universe")
    allowed = set(sessions)
    for rows in packet.values():
        require(isinstance(rows, dict) and set(rows) <= allowed, "Bar contains a session outside the frozen development calendar")
        for stamp, row in rows.items():
            require(isinstance(row, dict) and set(row) == {"close", "volume", "split", "distribution", "action_known_on"}
                    and positive(row["close"]) and type(row["volume"]) is int and row["volume"] >= 0
                    and positive(row["split"]) and type(row["distribution"]) in (int, float)
                    and isfinite(row["distribution"]) and row["distribution"] >= 0
                    and day(row["action_known_on"]) <= stamp, "Bar or action has invalid units or as-of provenance")


def pair_family(members, bars, sessions):
    records, sectors = {}, defaultdict(list)
    for key, member in sorted(members.items()):
        if member["kind"] != "common_stock" or member["country"] != "US":
            records[key] = {"status": "ineligible_instrument"}
            continue
        rows = bars.get(key, {})
        if any(stamp not in rows for stamp in sessions):
            records[key] = {"status": "missing_eligibility_history"}
            continue
        daily_dollars = median(rows[stamp]["volume"] * rows[stamp]["close"] for stamp in sessions)
        last = rows[sessions[-1]]["close"]
        reason = "eligible" if daily_dollars >= 10_000_000 and last >= 5 else "fails_liquidity_or_price"
        records[key] = {"status": reason, "median_daily_dollars": daily_dollars, "last_close": last}
        if reason == "eligible":
            sectors[member["sector"]].append((key, daily_dollars))
    family = []
    for values in sectors.values():
        ordered = sorted(values, key=lambda row: (-row[1], row[0]))
        for key, _ in ordered[30:]:
            records[key]["status"] = "outside_sector_cap"
        ids = sorted(key for key, _ in ordered[:30])
        family.extend((a, b) for a, b in combinations(ids, 2) if members[a]["issuer"] != members[b]["issuer"])
    return records, sorted(family)


def log_total_return(rows, sessions):
    """Rebuild levels forward. Distributions are dollars per post-split share."""
    result, prior, level = {}, None, 0.0
    for stamp in sessions:
        row = rows.get(stamp)
        if row is None:
            break  # A later close cannot reconstruct the intervening returns or actions.
        if prior is not None:
            level += log(row["split"]) + log(row["close"] + row["distribution"]) - log(prior)
        result[stamp] = level
        prior = row["close"]
    return result


def holm_adjust(rows):
    adjusted, size, running = {}, len(rows), 0.0
    for rank, ((a, b), p) in enumerate(sorted(rows.items(), key=lambda item: (item[1], item[0]))):
        running = max(running, min(1.0, (size - rank) * p))
        adjusted[a, b] = running
    return adjusted


def root_evidence(series, sessions):
    if any(stamp not in series for stamp in sessions):
        return None
    values = np.asarray([series[stamp] for stamp in sessions])
    try:
        level = stats._root_tests(values, "BIC")
        difference = stats._root_tests(np.diff(values), "BIC")
    except NUMERICAL_ERRORS:
        return None
    return {"level": level, "difference": difference, "order": stats.integration_order(level, difference)}


def model_points(a, b, sessions):
    rows = tuple(stats.Point(stamp, a[stamp], b[stamp]) for stamp in sessions)
    window = stats.Window(sessions[0], sessions[-1], sessions[-1], HOLDOUT_START)
    return rows, window


def calculate_pair(a, b, sessions, a_root, b_root):
    if a_root is None or b_root is None:
        return {"rule": "INCOMPLETE_OR_INVALID_HISTORY", "p": 1.0}
    root = stats.assess("diagnostics", {"result": {"a": a_root, "b": b_root}})
    if root["decision"] != "proceed":
        return {"rule": root["rule_id"], "p": 1.0}
    rows, window = model_points(a, b, sessions)
    try:
        result = stats.compute("engle_granger", rows, window, "BIC", sessions=sessions)["result"]
    except NUMERICAL_ERRORS:
        return {"rule": "EG_NUMERICAL_FAILURE", "p": 1.0}
    review = stats.assess("engle_granger", {"result": result})
    p = max(result["a_on_b"]["p"], result["b_on_a"]["p"])
    return {"rule": review["rule_id"], "p": p, "cointegration": result}


def stability(a, b, sessions):
    rows, window = model_points(a, b, sessions)
    try:
        evidence = stats.compute("stability", rows, window, "BIC", sessions=sessions)
    except NUMERICAL_ERRORS:
        return {"rule": "STABILITY_NUMERICAL_FAILURE", "passes": False}
    review = stats.assess("stability", evidence)
    return {"rule": review["rule_id"], "passes": review["decision"] == "continue", **evidence["result"]}


def rolling_windows(formation, calibration):
    sessions = formation + calibration
    month_positions = {stamp[:7]: index for index, stamp in enumerate(sessions)}
    require(all(month in month_positions for month in CALIBRATION_MONTHS), "Missing calibration month")
    return {sessions[index]: sessions[index - 179:index + 1]
            for index in (month_positions[month] for month in CALIBRATION_MONTHS)}


def rolling_check(a, b, sessions, beta):
    check = calculate_pair(a, b, sessions, root_evidence(a, sessions), root_evidence(b, sessions))
    check["passes"] = False
    if check["rule"] == "EG_BOTH_REJECT":
        check["stability"] = stability(a, b, sessions)
        slope = check["cointegration"]["a_on_b"]["slope"]
        check["passes"] = check["stability"]["passes"] and slope > 0 and abs(slope / beta - 1) <= 0.25
    return check


def z_paths(a, b, sessions, intercept, beta):
    spreads = [a[stamp] - intercept - beta * b[stamp] for stamp in sessions]
    values = {}
    for index in range(60, len(sessions)):
        trailing = spreads[index - 60:index]
        scale = stdev(trailing)
        if scale <= 0:
            raise ValueError("Spread standardization needs nonzero historical variation")
        values[sessions[index]] = (spreads[index] - mean(trailing)) / scale
    return values


def calibration_replay(packet, a, b, sessions, z, previous_day, checks, beta, *, bars, formation):
    from . import execution

    missing = {"status": "unresolved", "reason": "missing_execution_leg_session"}
    if any(key not in packet or stamp not in packet[key] for key in (a, b) for stamp in sessions):
        return missing, missing
    try:
        legs = zip(*(execution.reconciled_legs(key, packet[key], bars[key], formation, sessions)
                     for key in (a, b)), strict=True)
        inputs = [execution.Session(date.fromisoformat(stamp), z[stamp], checks.get(stamp, {"passes": True})["passes"],
                                    day_legs,
                                    close_ms=(13 if stamp == EARLY_CLOSE_2025 else 16) * 3_600_000)
                  for stamp, day_legs in zip(sessions, legs, strict=True)]
        kwargs = {"beta": beta, "previous_z": z[previous_day]}
        return tuple(execution.replay_calibration(inputs, stress=stress, **kwargs) for stress in (False, True))
    except ValueError as error:
        invalid = {"status": "unresolved", "reason": str(error)}
        return invalid, invalid


def screen_development(manifest, payloads, execution=None):
    eligibility, formation, calibration = venue_calendar(payloads["calendar"])
    members = load_universe(payloads["universe"])
    bars = payloads["bars"]
    check_bars(bars, members, eligibility + formation + calibration)
    if "execution" in payloads:
        records = payloads["execution"]
        require(isinstance(records, dict) and set(records) <= set(members)
                and all(isinstance(rows, dict) and set(rows) <= set(calibration) for rows in records.values()),
                "Execution input contains an unknown instrument or date outside calibration")
    selection, family = pair_family(members, bars, eligibility)
    report = {
        "schema_version": SCHEMA_VERSION, "manifest": manifest, "manifest_sha256": fingerprint(manifest),
        "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "protocol_sha256": sha256(Path(__file__).with_name("DISCOVERY_PROTOCOL_DRAFT.md").read_bytes()).hexdigest(),
        "edgelab_sha256": release_fingerprint(), "runtime": stats.numerical_runtime(),
        "holdout": "locked and untested", "tradable_pairs": [], "selection": selection, "pairs": [], "shortlist": [],
    }
    if any(entry["status"] == "missing_eligibility_history" for entry in selection.values()):
        return {**report, "verdict": "unresolved", "reason": "Incomplete eligibility prevents freezing the pair family"}

    ids = sorted({key for pair in family for key in pair})
    histories = {key: log_total_return(bars[key], formation + calibration) for key in ids}
    roots = {key: root_evidence(histories[key], formation) for key in ids}
    report["formation_roots"] = roots
    results = {(a, b): calculate_pair(histories[a], histories[b], formation, roots[a], roots[b]) for a, b in family}
    adjusted = holm_adjust({pair: result["p"] for pair, result in results.items()})
    windows = rolling_windows(formation, calibration)
    candidates = []
    for a, b in family:
        fit = results[a, b]
        row = {"pair": [a, b], "holm_p": adjusted[a, b], "formation": fit, "verdict": "stop",
               "reason": "formation_gate"}
        report["pairs"].append(row)
        if fit["rule"] in ("INCOMPLETE_OR_INVALID_HISTORY", "EG_NUMERICAL_FAILURE", "ROOT_INCONCLUSIVE", "EG_DISAGREEMENT"):
            row["verdict"] = "unresolved"
        if fit["rule"] == "ROOT_BOTH_I0":
            row["verdict"] = "revise"
        if adjusted[a, b] >= stats.ALPHA or fit["rule"] != "EG_BOTH_REJECT":
            continue
        a_series, b_series = histories[a], histories[b]
        stable = stability(a_series, b_series, formation)
        fit["stability"] = stable
        hedge = fit["cointegration"]["a_on_b"]
        beta = hedge["slope"]
        half_life = stable.get("ou_half_life_observations")
        if not stable["passes"] or beta <= 0 or stable["ecm_loading"] >= 0 or half_life is None or not 2 <= half_life <= 30:
            if stable["rule"] == "STABILITY_NUMERICAL_FAILURE":
                row["verdict"] = "unresolved"
            continue
        if any(stamp not in a_series or stamp not in b_series for stamp in calibration):
            row.update(verdict="unresolved", reason="missing_calibration_history")
            continue
        checks = {stamp: rolling_check(a_series, b_series, sessions, beta) for stamp, sessions in windows.items()}
        row["rolling_checks"] = checks
        if execution is None or "execution" not in payloads:
            row.update(verdict="unresolved" if all(check["passes"] for check in checks.values()) else "stop",
                       reason="execution_evidence_required" if all(check["passes"] for check in checks.values())
                       else "rolling_stability_gate")
            continue
        try:
            z = z_paths(a_series, b_series, formation + calibration, hedge["intercept"], beta)
        except NUMERICAL_ERRORS:
            row.update(verdict="unresolved", reason="invalid_spread_scale")
            continue
        base, stress = execution(payloads["execution"], a, b, calibration, z, formation[-1], checks, beta,
                                 bars=bars, formation=formation)
        row["calibration"] = {"base": base, "stress": stress}
        if base["status"] != "complete" or stress["status"] != "complete":
            row.update(verdict="unresolved", reason="execution_evidence_incomplete")
        elif not all(check["passes"] for check in checks.values()):
            row.update(reason="rolling_stability_gate")
        elif base["round_trips"] >= 12 and base["entry_fill_fraction"] >= 0.8 and base["net_return"] > 0:
            row.update(verdict="continue researching", reason="passed_development_gates")
            candidates.append(row)
        else:
            row.update(reason="calibration_economics_gate")

    candidates.sort(key=lambda row: (row["holm_p"], -row["calibration"]["stress"]["net_return"], row["pair"]))
    report["shortlist"] = [row["pair"] for row in candidates[:10]]
    report["verdict"] = ("continue researching" if candidates else
                         "unresolved" if any(row["verdict"] == "unresolved" for row in report["pairs"]) else "stop")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest, payloads = load_development(args.manifest)
    result = screen_development(manifest, payloads, execution=calibration_replay)
    if "execution" in payloads:
        result["execution_sha256"] = sha256(Path(__file__).with_name("execution.py").read_bytes()).hexdigest()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")


if __name__ == "__main__":
    main()
