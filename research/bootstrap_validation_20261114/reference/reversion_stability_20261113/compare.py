"""Paired, broker-free formation simulation; nothing is deployed."""

from collections import Counter
import csv
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
from random import Random

import numpy as np
from statsmodels.regression.linear_model import OLS
from statsmodels.stats.proportion import proportion_confint

from edgelab.stat_tools import Point, Window, assess, compute, numerical_runtime


HERE = Path(__file__).resolve().parent
N = 1000
SCENARIOS = ("independent_walks", "stable_strong", "stable_weak", "slope_reversal",
             "intercept_mid", "independent_persistent_ar", "intercept_off_grid")
ARMS = ((180, "cusum"), (180, "targeted"), (540, "cusum"), (540, "targeted"))
DAYS = tuple(str(date(2020, 1, 1) + timedelta(days=i)) for i in range(541))


def sample(case, seed, n):
    rng = Random(seed)
    a = b = spread = 0.
    phi = .98 if case == "independent_persistent_ar" else 1.
    rho = .92 if case == "stable_weak" else .4
    split = (42 * n // 100) if case == "intercept_off_grid" else n // 2
    rows = []
    for i, day in enumerate(DAYS[:n]):
        a = phi * a + rng.gauss(0, 1)
        b = phi * b + rng.gauss(0, 1)
        spread = rho * spread + rng.gauss(0, .6)
        beta = -1.2 if case == "slope_reversal" and i >= split else 1.3
        shift = 1.2 if case.startswith("intercept_") and i >= split else 0.
        value = b if case.startswith("independent_") else beta * a + spread + shift
        rows.append(Point(day, a, value))
    return tuple(rows)


def targeted(rows):
    a, b = np.asarray([(r.a, r.b) for r in rows]).T
    n = len(a)
    minimum = 1.
    for y, x in ((a, b), (b, a)):
        base = np.column_stack((np.ones(n), x))
        residual = OLS(y, base).fit().resid
        denom = residual[:-1] @ residual[:-1]
        if denom <= 0:
            raise ValueError("Degenerate residuals")
        phi = np.clip((residual[1:] @ residual[:-1]) / denom, -.98, .98)
        for split in (n // 3, n // 2, 2 * n // 3):
            post = np.arange(n) >= split
            design = np.column_stack((base, post, post * (x - x[split])))
            transformed = design[1:] - phi * design[:-1]
            if np.linalg.matrix_rank(transformed) != 4:
                raise ValueError("Degenerate break regression")
            fit = OLS(y[1:] - phi * y[:-1], transformed).fit(cov_type="HAC", cov_kwds={"maxlags": 6})
            if np.linalg.matrix_rank(fit.cov_params()[2:, 2:]) != 2:
                raise ValueError("Degenerate break covariance")
            p = float(fit.wald_test(np.eye(4)[2:], scalar=True).pvalue)
            if not 0 <= p <= 1:
                raise ValueError("Invalid break-test probability")
            minimum = min(minimum, p)
    return min(1., 6 * minimum)


def compare(case, seed, n):
    rows = sample(case, seed, n)
    window = Window(DAYS[0], DAYS[n - 1], DAYS[n - 1], DAYS[n])
    options = {"sessions": DAYS[:n]} if n != 180 else {}

    def run(step):
        evidence = compute(step, rows, window, "BIC", **options)
        return assess(step, evidence)

    for step in ("diagnostics", "engle_granger"):
        try:
            result = run(step)
        except (ArithmeticError, ValueError, RuntimeError, np.linalg.LinAlgError):
            return ((step, "tool_error", False, None),) * 2
        if result["decision"] != "proceed":
            return ((step, result["rule_id"], False, None),) * 2
    try:
        standard = run("stability")
        baseline = ("stability", standard["rule_id"], standard["decision"] == "continue",
                    standard["details"].get("cusum_p"))
    except (ArithmeticError, ValueError, RuntimeError, np.linalg.LinAlgError):
        baseline = ("stability", "tool_error", False, None)
    try:
        p = targeted(rows)
        challenger = ("stability", "targeted_reject" if p < .05 else "targeted_pass", p >= .05, p)
    except (ArithmeticError, ValueError, RuntimeError, np.linalg.LinAlgError):
        challenger = ("stability", "tool_error", False, None)
    return baseline, challenger


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    result = {"n_per_scenario": N, "runtime": numerical_runtime(), "protocol_sha256": digest(HERE / "PROTOCOL.md"),
              "runner_sha256": digest(Path(__file__)), "holdout": "locked; no holdout generated", "scenarios": {}}
    with (HERE / "trials.csv").open("x", newline="", encoding="ascii") as output:
        csv_out = csv.writer(output)
        csv_out.writerow(("scenario", "seed", "n", "stability_method", "terminal_stage", "rule", "advanced", "p"))
        for i, scenario in enumerate(SCENARIOS):
            rule_counts = [Counter() for _ in ARMS]
            stages = [Counter() for _ in ARMS]
            advances, gains, losses = ([0] * len(ARMS) for _ in range(3))
            for seed in range(200000 + i * N, 200000 + (i + 1) * N):
                outcomes = compare(scenario, seed, 180) + compare(scenario, seed, 540)
                for j, ((n, method), (stage, rule, advanced, p)) in enumerate(zip(ARMS, outcomes, strict=True)):
                    rule_counts[j][rule] += 1
                    stages[j][stage] += 1
                    advances[j] += advanced
                    gains[j] += advanced and not outcomes[0][2]
                    losses[j] += outcomes[0][2] and not advanced
                    csv_out.writerow((scenario, seed, n, method, stage, rule, int(advanced), p))
            result["scenarios"][scenario] = {
                f"{n}_{method}": {"advanced": advances[j], "wilson_95": list(map(float, proportion_confint(
                    advances[j], N, alpha=.05, method="wilson"))), "paired_gains": gains[j],
                    "paired_losses": losses[j], "stages": stages[j], "rules": rule_counts[j]}
                for j, (n, method) in enumerate(ARMS)}
            output.flush()
            print(scenario, dict(zip((f"{n}_{m}" for n, m in ARMS), advances)), flush=True)
    with (HERE / "results.json").open("x", encoding="ascii") as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()
