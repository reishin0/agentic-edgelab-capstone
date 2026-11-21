"""Run and verify the frozen EdgeLab synthetic confirmation study."""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import csv
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy

# Use one BLAS thread per worker. Set before importing numerical modules.
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

import numpy as np
from scipy.signal import lfilter
from statsmodels.stats.proportion import proportion_confint

from edgelab import contracts, stat_tools
from edgelab.stat_tools import Point, Window, assess, compute, numerical_runtime


HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "reference"
PRIOR = REFERENCE / "bootstrap_stability_20261114" / "run.py"
REFERENCE_HASHES = {
    PRIOR: "36a429eaa0ecbe5db7a12a893138322fd9b03f62cdd55b0ff152b9d941965b43",
    REFERENCE / "reversion_stability_20261113" / "compare.py":
        "2c176b96665e881173a45499817d79f8440ced96c338e23b0bf20d550de39404",
    Path(stat_tools.__file__):
        "f76340c5c3d18bb6c70f6bf9535ad99c314842ba07f983c2ce123bacaed69369",
    Path(contracts.__file__):
        "24096259beda9c153562eb9e0bca3d435187c2c0f33c90594d825bf675493fe1",
}
SCENARIOS = {
    "independent_walks": "ceiling",
    "stable_strong": "preserve",
    "stable_weak": "gain",
    "slope_reversal": "ceiling",
    "intercept_mid": "ceiling",
    "independent_persistent_ar": "ceiling",
    "intercept_off_grid": "ceiling",
    "stable_weak_arma": "gain",
    "stable_student_t": "gain",
    "stable_volatility_regimes": "gain",
    "stable_near_unit": "gain",
    "correlated_walks": "ceiling",
    "intercept_small_early": "ceiling",
    "intercept_large_late": "ceiling",
    "intercept_large_outside_grid": "ceiling",
    "intercept_weak_late": "ceiling",
    "slope_moderate_early": "ceiling",
    "slope_moderate_outside_grid": "ceiling",
}
BREAKS = {
    "intercept_small_early": (22, 0.6, 0),
    "intercept_large_late": (74, -2.4, 0),
    "intercept_large_outside_grid": (85, 2.4, 0),
    "intercept_weak_late": (74, 2.4, 0),
    "slope_moderate_early": (22, 0, 0.30),
    "slope_moderate_outside_grid": (15, 0, -0.30),
}
ARMS = ("180_cusum", "540_cusum", "540_bootstrap")
FIELDS = ("scenario", "seed", "arm", "stage", "rule", "advanced", "p", "split_index")
N = 1000
SEED_START = 510000
CHUNK_SIZE = 100
TOOL_ERRORS = (ValueError, RuntimeError, ArithmeticError, np.linalg.LinAlgError)


def file_digest(path):
    return sha256(path.read_bytes()).hexdigest()


def check_reference():
    for path, expected in REFERENCE_HASHES.items():
        if file_digest(path) != expected:
            raise ValueError(f"Frozen reference mismatch: {path}")


check_reference()
REFERENCE_RUN = runpy.run_path(str(PRIOR))
DAYS = REFERENCE_RUN["DAYS"]
BOOTSTRAP = REFERENCE_RUN["bootstrap"]


def sampled_paths(case, seed):
    if case in REFERENCE_RUN["SCENARIOS"]:
        return tuple(REFERENCE_RUN["series"](case, seed, n) for n in (180, 540))
    rng = np.random.default_rng(seed)
    n = 540
    trend_shock, independent_shock, spread_shock = rng.normal(size=(3, n))
    trend = np.cumsum(trend_shock)
    if case == "correlated_walks":
        second = np.cumsum(0.8 * trend_shock + 0.6 * independent_shock)
    else:
        phi = 0.92 if case in {
            "stable_student_t", "stable_volatility_regimes", "intercept_weak_late",
        } else 0.4
        if case == "stable_near_unit":
            phi = 0.96
        if case == "stable_student_t":
            spread_shock = rng.standard_t(3, size=n) / np.sqrt(3)
        if case == "stable_volatility_regimes":
            spread_shock *= np.repeat([0.5, 1.5, 0.5], n // 3)
        spread = lfilter([0.6], [1.0, -phi], spread_shock)
        second = 1.3 * trend + spread
        if case in BREAKS:
            fraction, intercept_jump, hedge_jump = BREAKS[case]
            split = n * fraction // 100
            second[split:] += intercept_jump + hedge_jump * trend[split:]
    long_rows = tuple(Point(day, float(a), float(b)) for day, a, b in
                      zip(DAYS[:n], trend, second, strict=True))
    return long_rows[:180], long_rows


def evaluate(rows, seed):
    n = len(rows)
    window = Window(DAYS[0], DAYS[n - 1], DAYS[n - 1], DAYS[n])
    sessions = DAYS[:n] if n == 540 else None

    def check(stage):
        return assess(stage, compute(stage, rows, window, "BIC", sessions=sessions))

    for stage in ("diagnostics", "engle_granger"):
        try:
            found = check(stage)
            outcome = (stage, found["rule_id"], 0, "", "")
            if found["decision"] == "proceed":
                continue
        except TOOL_ERRORS as exc:
            outcome = (stage, f"tool_error:{type(exc).__name__}", 0, "", "")
        return (outcome,) * (2 if n == 540 else 1)
    try:
        found = check("stability")
        cusum = ("stability", found["rule_id"], int(found["decision"] == "continue"),
                 found["details"].get("cusum_p", ""), "")
    except TOOL_ERRORS as exc:
        cusum = ("stability", f"tool_error:{type(exc).__name__}", 0, "", "")
    if n == 180:
        return (cusum,)
    try:
        p, split = BOOTSTRAP(rows, seed)
        clear = p >= 0.05
        bootstrap = ("stability", "bootstrap_clear" if clear else "bootstrap_break",
                     int(clear), p, split)
    except TOOL_ERRORS as exc:
        bootstrap = ("stability", f"bootstrap_unresolved:{exc}", 0, "", "")
    return cusum, bootstrap


def batches():
    for i, case in enumerate(SCENARIOS):
        start = SEED_START + N * i
        for begin in range(start, start + N, CHUNK_SIZE):
            yield case, begin, begin + CHUNK_SIZE


def run_batch(batch):
    case, begin, end = batch
    result = []
    for seed in range(begin, end):
        short_rows, long_rows = sampled_paths(case, seed)
        outcomes = evaluate(short_rows, seed) + evaluate(long_rows, seed)
        result.extend((case, seed, arm, *outcome)
                      for arm, outcome in zip(ARMS, outcomes, strict=True))
    return result


def exact_limit(count, total, tail, side):
    low, high = proportion_confint(count, total, alpha=2 * tail, method="beta")
    return float(low if side == "lower" else high)


def paired_comparison(test, control):
    differences = [a - b for a, b in zip(test, control, strict=True)]
    gains, losses = differences.count(1), differences.count(-1)
    n = len(differences)
    return {
        "gains": gains,
        "losses": losses,
        "difference": (gains - losses) / n,
        "lower_97_5": exact_limit(gains, n, 0.0125, "lower") -
                      exact_limit(losses, n, 0.0125, "upper"),
    }


def summarize(ledger):
    winners = {"180_cusum": "CUSUM_NOT_REJECTED", "540_cusum": "CUSUM_NOT_REJECTED",
               "540_bootstrap": "bootstrap_clear"}
    rows = {(row["scenario"], int(row["seed"]), row["arm"]): row for row in ledger}
    expected = len(SCENARIOS) * N * len(ARMS)
    if len(ledger) != expected or len(rows) != expected:
        raise ValueError("Missing or duplicate ledger entries")
    result = {}
    for i, (case, target) in enumerate(SCENARIOS.items()):
        start = SEED_START + i * N
        counts, bits = {}, {}
        for arm in ARMS:
            records = [rows[case, seed, arm] for seed in range(start, start + N)]
            rules = Counter(record["rule"] for record in records)
            stability = Counter(record["rule"] for record in records
                                if record["stage"] == "stability")
            for record in records:
                if (record["stage"] not in {"diagnostics", "engle_granger", "stability"}
                        or record["advanced"] != str(int(record["rule"] == winners[arm]))):
                    raise ValueError(f"Invalid verdict or stage in {record}")
                if arm == "540_bootstrap" and record["rule"] in {"bootstrap_clear", "bootstrap_break"}:
                    p, split = float(record["p"]), int(record["split_index"])
                    if (not 0.005 <= p <= 1 or not np.isclose(p * 200, round(p * 200))
                            or not (split in range(108, 433, 5) or split == 432)
                            or int(p >= .05) != int(record["advanced"])):
                        raise ValueError(f"Invalid bootstrap result in {record}")
            bits[arm] = [int(record["advanced"]) for record in records]
            count = sum(bits[arm])
            counts[arm] = {
                "advanced": count,
                "advanced_rate": count / N,
                "wilson_95": list(map(float, proportion_confint(count, N, method="wilson"))),
                "terminal_rules": dict(rules),
                "stability_reached": sum(stability.values()),
                "stability_counts": dict(stability),
            }
        upper = exact_limit(sum(bits["540_bootstrap"]), N, .025, "upper")
        comparisons = {control: paired_comparison(bits["540_bootstrap"], bits[control])
                       for control in ARMS[:2]}
        if target == "ceiling":
            checks = {"upper_97_5_at_most_0.02": upper <= .02}
        elif target == "gain":
            checks = {"lower_97_5_vs_180_at_least_0.10":
                      comparisons["180_cusum"]["lower_97_5"] >= .10}
        else:
            checks = {f"lower_97_5_vs_{arm}_at_least_-0.05": values["lower_97_5"] >= -.05
                      for arm, values in comparisons.items()}
        result[case] = {"arms": counts, "bootstrap_upper_97_5": upper,
                        "paired_comparisons": comparisons, "checks": checks,
                        "passed": all(checks.values())}
    return result


def hashes():
    return {
        "protocol": file_digest(HERE / "PROTOCOL.md"),
        "runner": file_digest(Path(__file__)),
        "bootstrap_reference": file_digest(PRIOR),
        "generator_reference": file_digest(REFERENCE / "reversion_stability_20261113" / "compare.py"),
        "stat_tools": file_digest(Path(stat_tools.__file__)),
        "contracts": file_digest(Path(contracts.__file__)),
    }


def ledger_summary(directory):
    with (directory / "trials.csv").open(newline="", encoding="utf-8") as handle:
        return summarize(list(csv.DictReader(handle)))


def main(directory, workers):
    directory.mkdir(parents=False, exist_ok=False)
    with (directory / "trials.csv").open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for result in executor.map(run_batch, batches()):
                writer.writerows(result)
                if result[-1][1] % N == N - 1:
                    handle.flush()
                    print(f"Completed {result[-1][0]}", flush=True)
    scenarios = ledger_summary(directory)
    packet = {"runtime": numerical_runtime(), "n_per_scenario": N,
              "bootstrap_draws": REFERENCE_RUN["DRAWS"], "hashes": hashes(),
              "ledger_sha256": file_digest(directory / "trials.csv"),
              "market_holdout": "locked; only synthetic formation rows used",
              "passed": all(case["passed"] for case in scenarios.values()),
              "scenarios": scenarios}
    with (directory / "results.json").open("x", encoding="utf-8") as handle:
        json.dump(packet, handle, indent=2, allow_nan=False)
        handle.write("\n")


def verify(directory):
    packet = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    scenarios = ledger_summary(directory)
    if (packet["hashes"] != hashes() or packet["scenarios"] != scenarios
            or packet["ledger_sha256"] != file_digest(directory / "trials.csv")
            or packet["passed"] != all(case["passed"] for case in scenarios.values())):
        raise ValueError("Source or ledger mismatch, or summary disagrees with outcomes")
    print(f"Verified {len(SCENARIOS) * N * len(ARMS)} synthetic arm outcomes")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    mode = cli.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--verify", type=Path)
    mode.add_argument("--self-check", action="store_true")
    cli.add_argument("--workers", type=int, default=4)
    args = cli.parse_args()
    if args.workers < 1:
        cli.error("--workers must be positive")
    if args.self_check:
        REFERENCE_RUN["self_check"]()
        assert exact_limit(0, 1000, .025, "upper") < .02
        assert exact_limit(20, 1000, .025, "upper") > .02
        assert 0 < paired_comparison([1] * 1000, [0] * 1000)["lower_97_5"] < 1
    elif args.verify:
        verify(args.verify)
    else:
        main(args.output, args.workers)
