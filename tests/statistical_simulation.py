"""Run the frozen BIC study in SIMULATION_PLAN.rst; no holdout is generated."""

from collections import Counter
import csv
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
from random import Random
import sys

from statsmodels.stats.proportion import proportion_confint

from edgelab.stat_tools import Point, assess, compute, numerical_runtime, window_for_demo


CASES = ("independent_walks", "stable_strong", "stable_weak", "slope_reversal", "intercept_shift",
         "independent_persistent_ar")
N = 1000
STEPS = ("diagnostics", "engle_granger", "stability")
WINDOW = window_for_demo()
DAYS = tuple(str(date(2020, 1, 1) + timedelta(days=i)) for i in range(180))


def sample(case, seed):
    rng = Random(seed)
    a = b = spread = 0.0
    rows = []
    for i, day in enumerate(DAYS):
        phi = .98 if case == "independent_persistent_ar" else 1.
        a = phi * a + rng.gauss(0, 1)
        b = phi * b + rng.gauss(0, 1)
        spread = (.92 if case == "stable_weak" else .4) * spread + rng.gauss(0, .6)
        if case not in ("independent_walks", "independent_persistent_ar"):
            beta = -1.2 if case == "slope_reversal" and i >= 90 else 1.3
            b_value = beta * a + spread + (1.2 if case == "intercept_shift" and i >= 90 else 0)
        else:
            b_value = b
        rows.append(Point(day, a, b_value))
    return tuple(rows)


def reference(step, data):
    if data is None:
        return "unresolved"
    if step == "diagnostics":
        def stationary(x):
            return x["adf_p"] < .05 <= x["kpss_p"]
        def classify(x):
            level = x["level"]
            if stationary(level):
                return 0
            if level["kpss_p"] < .05 <= level["adf_p"] and stationary(x["difference"]):
                return 1
            return None
        a, b = (classify(data[label]) for label in ("a", "b"))
        return ("unresolved" if None in (a, b) else "stop" if a != b else
                "proceed" if a == 1 else "revise")
    if step == "engle_granger":
        reject = sum(data[d]["p"] < .05 for d in ("a_on_b", "b_on_a"))
        return ("stop", "unresolved", "proceed")[reject]
    return "stop" if data["cusum_p"] < .05 else "continue"


def trial(case, seed):
    rows = sample(case, seed)
    agree, errors = True, 0
    for step in STEPS:
        try:
            observation = compute(step, rows, WINDOW, "BIC")
        except (ArithmeticError, ValueError, RuntimeError):
            observation = None
            errors += 1
        finding = assess(step, observation)
        decision = finding["decision"]
        agree &= reference(step, observation["result"] if observation else None) == decision
        if decision != "proceed":
            return step, finding["rule_id"], decision, agree, errors
    raise RuntimeError("No terminal decision")


def main(output):
    summary = {"runtime": numerical_runtime(), "trials_per_scenario": N, "lag_policy": "BIC",
               "plan_sha256": sha256((Path(__file__).parents[1] / "SIMULATION_PLAN.rst").read_bytes()).hexdigest(),
               "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
               "holdout": "locked; no holdout generated", "scenarios": {}}
    with (output / "trials.csv").open("x", encoding="ascii", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(("scenario", "seed", "terminal_stage", "rule", "decision", "reference_agrees", "tool_errors"))
        for index, case in enumerate(CASES):
            counts, rules, agrees, errors = Counter(), Counter(), 0, 0
            for seed in range(100000 + index * N, 100000 + (index + 1) * N):
                stage, rule, decision, match, error = trial(case, seed)
                writer.writerow((case, seed, stage, rule, decision, int(match), error))
                counts[decision] += 1
                rules[rule] += 1
                agrees += int(match)
                errors += error
            positive = counts["continue"]
            stable = case in ("stable_strong", "stable_weak")
            misses = N - positive if stable else positive
            lo, hi = proportion_confint(misses, N, alpha=.05, method="wilson")
            summary["scenarios"][case] = {"decisions": counts, "rules": rules, "positive": positive,
                "error_metric": "missed_detection" if stable else "false_advancement", "errors": misses,
                "error_rate": misses / N, "wilson_95": [float(lo), float(hi)],
                "reference_agreements": agrees, "invalid_tool_runs": errors}
            print(f"{case}: {positive}/{N} continue; {agrees}/{N} reference matches", flush=True)
    summary["reference_agreement"] = sum(r["reference_agreements"] for r in summary["scenarios"].values()) / (N * len(CASES))
    with (output / "simulation-results.json").open("x", encoding="ascii") as out:
        json.dump(summary, out, indent=2, allow_nan=False)
        out.write("\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
