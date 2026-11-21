"""Run the frozen equity holdout through an authenticated, one-use host lease."""

from collections import defaultdict
from datetime import date, timedelta
from hashlib import sha256
import json
from math import isclose, isfinite
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

from edgelab import stat_tools as stats
from edgelab.contracts import fingerprint
from edgelab.market_history import calendar_2025
from edgelab.research import release_fingerprint

from . import acceptance, execution, screen
from .approval import HoldoutAuthority, HoldoutDenied, manifest_digest


PROTOCOL_ID = "equity-cointegration-v1"
PROTOCOL_PATH = Path(__file__).with_name("DISCOVERY_PROTOCOL_DRAFT.md")
HOLDOUT_END = "2026-10-30"
DEVELOPMENT_INPUT_IDS = frozenset({"manifest", "report", "calendar", "universe", "bars", "execution"})
REQUIRED_CODE_PATHS = frozenset(
    {f"research/pair_discovery/{name}.py" for name in ("approval", "acceptance", "execution", "holdout", "holdout_host", "screen")}
    | {f"edgelab/{name}.py" for name in ("contracts", "journal", "market_history", "research", "stat_tools")}
)
ARTIFACT_FIELDS = {
    "calendar.json": ("calendar", "close_ms"),
    "bars.json": ("close", "volume", "split", "distribution", "action_known_on"),
    "execution.json": ("quotes", "mark", "median_volume", "borrow", "financing_bps", "commission_per_share",
                       "sell_fee_bps", "halted", "split_ratio", "cash_in_lieu", "dividend", "settlement"),
}
CALENDAR_ID = "NYSE Group 2025-2026 (US equities)"
DAY_MS = 3_600_000
BASELINE = {"name": "flat_no_trade", "net_return": 0, "max_drawdown": 0}
CLOSED_2026 = frozenset({"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
                        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07"})
EARLY_CLOSES = frozenset({"2025-11-28", "2025-12-24"})
INCOMPLETE_CHECKS = frozenset({"INCOMPLETE_OR_INVALID_HISTORY", "EG_NUMERICAL_FAILURE", "INVALID_EVIDENCE",
                              "NO_OBSERVATION", "STABILITY_NUMERICAL_FAILURE"})


def artifact_digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def development_start():
    return screen.development_sessions()[0][0]


def frozen_parameters():
    return {
        "strategy": {"formation": [screen.FORMATION_START, screen.FORMATION_END],
                     "calibration": [screen.CALIBRATION_START, screen.CALIBRATION_END],
                     "formation_lag_policy": "BIC", "alpha": stats.ALPHA, "rolling_sessions": 180,
                     "rolling_beta_drift_max": 0.25, "normalization_sessions": 60, "entry_z": 2,
                     "mean_reversion_exit_z": 0.5, "adverse_exit_z": 4, "time_stop_sessions": execution.MAX_HOLD,
                     "position_policy": "flat-at-start; frozen-beta; final-session-liquidation"},
        "execution": {"capital_usd": execution.CAPITAL, "gross_target_usd": execution.CAPITAL,
                      "quote_window_ms": [execution.OPEN_MS, execution.WINDOW_END_MS],
                      "max_quote_age_ms": execution.MAX_QUOTE_AGE_MS, "volume_sessions": execution.VOLUME_LOOKBACK,
                      "volume_fraction": 0.001, "weight_rounding_max": 0.05,
                      "base_display_fraction": 0.25, "stress_display_fraction": 0.125,
                      "base_slippage_bps": 5, "stress_slippage_bps": 10, "stress_half_spread_multiplier": 2,
                      "stress_borrow_multiplier": 2, "stress_leg_delay_ms": execution.LEG_DELAY_MS,
                      "fee_schedule": "dated custodian artifact", "holding_day_count": "ACT/365"},
        "acceptance": {**{mode: dict(gates) for mode, gates in acceptance.THRESHOLDS.items()},
                       "require_all_monthly_checks": True, "require_complete_evidence": True,
                       "require_flat_terminal": True, "strictly_positive_stress": True},
    }


def holdout_calendar():
    first_2026 = date(2026, 1, 1)
    last = date.fromisoformat(HOLDOUT_END)
    sessions_2026 = (first_2026 + timedelta(days=i) for i in range((last - first_2026).days + 1))
    sessions = calendar_2025(date.fromisoformat(screen.HOLDOUT_START), date(2025, 12, 31)) + tuple(
        day.isoformat() for day in sessions_2026 if day.weekday() < 5 and day.isoformat() not in CLOSED_2026
    )
    return {stamp: (13 if stamp in EARLY_CLOSES else 16) * DAY_MS for stamp in sessions}


def _require(condition, message):
    if not condition:
        raise HoldoutDenied(message)


def _decode(raw, label):
    def object_hook(pairs):
        obj = dict(pairs)
        _require(len(obj) == len(pairs), f"Duplicate fields in {label}")
        return obj

    def bad_constant(_):
        raise HoldoutDenied(f"Nonfinite value in {label}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=object_hook, parse_constant=bad_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HoldoutDenied(f"Invalid JSON in {label}") from error


def validate_run_manifest(manifest):
    digest = manifest_digest(manifest)
    _require(manifest["protocol"] == {"id": PROTOCOL_ID, "sha256": artifact_digest(PROTOCOL_PATH)},
             "The approved protocol does not match the installed document")
    _require(REQUIRED_CODE_PATHS <= set(manifest["code"]["files"]), "Missing holdout code pins")
    _require(fingerprint(manifest["parameters"]) == fingerprint(frozen_parameters()), "Unknown frozen parameters")
    development = manifest["development"]
    _require(development["start"] == development_start() and development["end"] == screen.CALIBRATION_END
             and set(development["inputs"]) == DEVELOPMENT_INPUT_IDS, "Unknown development input scope")
    scope = manifest["holdout"]
    _require((scope["start"], scope["end"]) == (screen.HOLDOUT_START, HOLDOUT_END),
             "Unknown holdout window")
    _require({item["path"]: tuple(item["fields"]) for item in scope["artifacts"]} == ARTIFACT_FIELDS,
             "Unknown holdout artifact schema")
    return digest


def development_candidates(report):
    rows = {}
    for row in report["pairs"]:
        pair = tuple(row["pair"])
        _require(pair not in rows, "Duplicate pair in development report")
        rows[pair] = row
    shortlist = []
    for pair in report["shortlist"]:
        row = rows.get(tuple(pair))
        _require(row is not None and row["verdict"] == "continue researching"
                 and row["reason"] == "passed_development_gates", "Shortlist contains an unqualified pair")
        hedge = row["formation"]["cointegration"]["a_on_b"]
        shortlist.append({"pair": pair, "intercept": hedge["intercept"], "beta": hedge["slope"]})
    return shortlist


def _load_development(manifest, development_manifest_path, development_report_path):
    pinned = manifest["development"]["inputs"]
    report_bytes = Path(development_report_path).read_bytes()
    _require(sha256(report_bytes).hexdigest() == pinned["report"], "Development report pin differs")
    report = _decode(report_bytes, "development report")
    try:
        source, payloads = screen.load_development(development_manifest_path, pinned=pinned)
        _require(report["manifest"] == source and report["manifest_sha256"] == fingerprint(source)
                 and report["runner_sha256"] == artifact_digest(screen.__file__)
                 and report["execution_sha256"] == artifact_digest(execution.__file__)
                 and report["edgelab_sha256"] == release_fingerprint()
                 and report["runtime"] == stats.numerical_runtime()
                 and report["protocol_sha256"] == manifest["protocol"]["sha256"]
                 and report["holdout"] == "locked and untested" and report["tradable_pairs"] == [],
                 "Development report provenance does not match the frozen research path")
        _require(report["verdict"] == "continue researching" and "execution" in payloads,
                 "Development report has no execution-qualified shortlist")
        _require(fingerprint(development_candidates(report)) == fingerprint(manifest["candidates"]),
                 "Frozen shortlist or formation hedge differs from development report")
    except (KeyError, TypeError, ValueError) as error:
        raise HoldoutDenied(f"Invalid frozen development evidence: {error}") from error
    # Reproduce the claims used to select candidates, not merely a saved verdict.
    reproduced = screen.screen_development(source, payloads, execution=screen.calibration_replay)
    reproduced["execution_sha256"] = artifact_digest(execution.__file__)
    _require(fingerprint(reproduced) == fingerprint(report), "Development report does not reproduce")
    return source, payloads


def _leased_payloads(lease, manifest, reader, classification, instruments):
    scope = manifest["holdout"]
    data = {}
    for name, fields in ARTIFACT_FIELDS.items():
        packet = _decode(lease.read_artifact(name, reader), name)
        expected = {"schema_version": 1, "snapshot_id": scope["snapshot_id"], "classification": classification,
                    "start": scope["start"], "end": scope["end"], "instrument_ids": list(instruments),
                    "fields": list(fields)}
        _require(type(packet) is dict and set(packet) == {*expected, "data"} and type(packet["data"]) is dict
                 and fingerprint({key: packet[key] for key in expected}) == fingerprint(expected),
                 f"Custodian scope differs for {name}")
        data[name] = packet["data"]
    schedule = holdout_calendar()
    calendar = data["calendar.json"]
    _require(set(calendar) == set(ARTIFACT_FIELDS["calendar.json"]) and calendar["calendar"] == CALENDAR_ID
             and fingerprint(calendar["close_ms"]) == fingerprint(schedule), "Incomplete or unexpected NY session schedule")
    for name in ("bars.json", "execution.json"):
        _require(set(data[name]) == set(instruments), f"Unexpected instrument set in {name}")
        for key, rows in data[name].items():
            _require(type(rows) is dict and set(rows) <= schedule.keys(), f"Unexpected session scope for {key} in {name}")
            _require(all(type(row) is dict and set(row) == set(ARTIFACT_FIELDS[name]) for row in rows.values()),
                     f"Unexpected payload columns for {key} in {name}")
    screen.check_bars(data["bars.json"], instruments, tuple(schedule))
    return data, schedule


def _rolling_check(a, b, sessions, beta):
    """Run the same EdgeLab tests on scoped series after the holdout lease verifies them."""
    a_root, b_root = (screen.root_evidence(values, sessions) for values in (a, b))
    if a_root is None or b_root is None:
        return {"passes": False, "rule": "INCOMPLETE_OR_INVALID_HISTORY"}
    roots = {"a": a_root, "b": b_root}
    root = stats.assess("diagnostics", {"result": roots})
    if root["decision"] != "proceed":
        return {"passes": False, "rule": root["rule_id"], "roots": roots}
    a_level, b_level = (np.asarray([series[stamp] for stamp in sessions]) for series in (a, b))
    try:
        cointegration = {"a_on_b": stats._cointegration(a_level, b_level, "BIC"),
                         "b_on_a": stats._cointegration(b_level, a_level, "BIC")}
        fingerprint(cointegration)
    except screen.NUMERICAL_ERRORS:
        return {"passes": False, "rule": "EG_NUMERICAL_FAILURE", "roots": roots}
    eg = stats.assess("engle_granger", {"result": cointegration})
    result = {"passes": False, "rule": eg["rule_id"], "roots": roots, "cointegration": cointegration}
    if eg["decision"] != "proceed":
        return result
    try:
        stability = stats._stability(a_level, b_level)
        fingerprint(stability)
    except screen.NUMERICAL_ERRORS:
        return {**result, "rule": "STABILITY_NUMERICAL_FAILURE"}
    review = stats.assess("stability", {"result": stability})
    slope = cointegration["a_on_b"]["slope"]
    drift = abs(slope / beta - 1)
    passes = review["decision"] == "continue" and slope > 0 and drift <= 0.25
    return {**result, "passes": passes, "rule": "HEDGE_BETA_DRIFT" if review["decision"] == "continue" and not passes
            else review["rule_id"], "stability": stability, "beta_relative_drift": drift}


def _sessions_for_pair(keys, development_bars, bars, raw_execution, seed_days, schedule, candidate):
    expected = tuple(schedule)
    calendar = seed_days + expected
    levels, events = [], defaultdict(list)
    for key in keys:
        if set(bars[key]) != schedule.keys() or set(raw_execution[key]) != schedule.keys():
            raise ValueError(f"Incomplete holdout dates for {key}")
        rows = {**development_bars[key], **bars[key]}
        levels.append(screen.log_total_return(rows, calendar))
        legs = execution.reconciled_legs(key, raw_execution[key], rows, seed_days, expected)
        for stamp, leg in zip(expected, legs, strict=True):
            events[stamp].append(leg)
    a, b = levels
    beta, intercept = candidate["beta"], candidate["intercept"]
    z = screen.z_paths(a, b, calendar, intercept, beta)
    ends = {stamp[:7]: index for index, stamp in enumerate(calendar) if stamp in schedule}
    checks = {
        calendar[index]: _rolling_check(a, b, calendar[index - 179:index + 1], beta)
        for index in ends.values()
    }
    days = tuple(
        execution.Session(date.fromisoformat(stamp), z[stamp], checks.get(stamp, {"passes": True})["passes"],
                          tuple(events[stamp]), schedule[stamp])
        for stamp in expected
    )
    return days, checks, z[seed_days[-1]]


def _arm_reconciles(result):
    gross, net = result["gross_return"], result["net_return"]
    if gross is None or net is None:
        return False
    total_cost = sum(result["costs"].values())
    return isclose((gross - net) * execution.CAPITAL, total_cost, abs_tol=1e-8, rel_tol=1e-10)


def _run_pair(candidate, development_bars, holdout, seed_days, schedule, market):
    keys = candidate["pair"]
    row = {"pair": keys, "formation_intercept": candidate["intercept"], "formation_beta": candidate["beta"],
           "no_trade_reference": BASELINE, "tradable_under_tested_assumptions": False}
    try:
        days, monthly_checks, previous_z = _sessions_for_pair(keys, development_bars, holdout["bars.json"],
                                                              holdout["execution.json"], seed_days, schedule, candidate)
    except screen.NUMERICAL_ERRORS as error:
        return {**row, "verdict": "unresolved", "reason": str(error)}
    beta = candidate["beta"]
    base = execution.Replay(beta, False).run(days, previous_z)
    stress = execution.Replay(beta, True).run(days, previous_z)
    row.update(monthly_checks=monthly_checks, base=base, stress=stress)
    if any(check["rule"] in INCOMPLETE_CHECKS for check in monthly_checks.values()):
        return {**row, "verdict": "unresolved", "reason": "incomplete_monthly_statistical_evidence"}
    if base["status"] == stress["status"] == "complete" and not all(_arm_reconciles(arm) for arm in (base, stress)):
        return {**row, "verdict": "revise", "reason": "replay_cash_flow_does_not_reconcile"}
    decision = acceptance.decide(base, stress)
    row.update(decision)
    row["tradable_under_tested_assumptions"] = market and decision["passed_numeric_gates"]
    return row


def _publish(output_root, directory, record):
    root = Path(output_root).resolve(strict=True)
    _require(root.is_dir(), "Output root must be a trusted directory")
    target = root
    for part in directory.split("/"):
        child = target / part
        child.mkdir(exist_ok=True)
        _require(child.resolve(strict=True) == child and child.is_dir(), "Output directory is not in the trusted root")
        target = child
    report = target / "holdout-report.json"
    result = record["result"]
    raw = (json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if report.exists():
        _require(report.resolve(strict=True) == report and report.is_file() and report.read_bytes() == raw,
                 "The existing report differs from the committed result")
        return report
    pending = None
    try:
        with NamedTemporaryFile(dir=target, prefix=f".{result['run_id']}-", suffix=".pending", delete=False) as handle:
            pending = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if os.name == "nt":
                pending.rename(report)
            else:
                os.link(pending, report)
        except FileExistsError:
            _require(report.resolve(strict=True) == report and report.is_file() and report.read_bytes() == raw,
                     "The existing report differs from the committed result")
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return report


def run_holdout(authority, manifest, receipt, *, development_manifest_path, development_report_path,
                read_artifact, output_root, resume=False):
    """Evaluate exactly the approved plan. No broker, network, or approval issuer."""
    _require(type(authority) is HoldoutAuthority, "An authenticated holdout authority is required")
    _require(type(resume) is bool, "Resume must be an explicit Boolean")
    digest = validate_run_manifest(manifest)
    frozen = _decode(json.dumps(manifest, allow_nan=False).encode("utf-8"), "frozen manifest")
    _require(fingerprint(frozen) == digest, "Manifest changed before execution")
    completed = authority.completed_result(frozen, receipt)
    if completed is not None:
        _require(resume and "result" in completed, "The requested run has already completed")
        return _publish(output_root, frozen["output_directory"], completed)

    source, development = _load_development(frozen, development_manifest_path, development_report_path)
    market = source["classification"] == "equity-development"
    classification = "equity-holdout" if market else "synthetic-holdout"
    keys = tuple(sorted({key for entry in frozen["candidates"] for key in entry["pair"]}))
    _, formation, calibration = screen.venue_calendar(development["calendar"])
    with authority.open_run(frozen, receipt, resume=resume) as lease:
        holdout, schedule = _leased_payloads(lease, frozen, read_artifact, classification, keys)
        rows = [_run_pair(candidate, development["bars"], holdout, formation + calibration, schedule, market)
                for candidate in frozen["candidates"]]
        result = {"run_id": frozen["run_id"], "manifest_sha256": digest, "scope": classification,
                  "manifest": frozen, "calendar": holdout["calendar.json"]["calendar"], "sessions": len(schedule),
                  "runtime": stats.numerical_runtime(), "pairs": rows, "no_trade_reference": BASELINE,
                  "tradable_pairs": [row["pair"] for row in rows if row["tradable_under_tested_assumptions"]]}
        lease.finish(fingerprint(result), result=result)
        return _publish(output_root, frozen["output_directory"], {"result": result})
