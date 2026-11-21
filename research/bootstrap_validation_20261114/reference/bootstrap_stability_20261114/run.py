"""Fixed, isolated successor study; writes only synthetic evidence."""

from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sys

import numpy as np
from scipy.signal import lfilter
from statsmodels.stats.proportion import proportion_confint

from edgelab.stat_tools import Point, Window, assess, compute, numerical_runtime


HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / "reversion_stability_20261113" / "compare.py"
PRIOR = runpy.run_path(str(PREVIOUS))
SCENARIOS = PRIOR["SCENARIOS"] + ("stable_weak_arma",)
DAYS = PRIOR["DAYS"]
N = 500
DRAWS = 199
ARMS = ("180_cusum", "540_cusum", "540_bootstrap")


def series(case, seed, n):
    if case != "stable_weak_arma":
        return PRIOR["sample"](case, seed, n)
    from random import Random
    rng = Random(seed)
    x = spread = last = 0.
    rows = []
    for day in DAYS[:n]:
        x += rng.gauss(0, 1)
        rng.gauss(0, 1)
        innovation = rng.gauss(0, .6)
        spread = .92 * spread + innovation + .5 * last
        last = innovation
        rows.append(Point(day, x, 1.3 * x + spread))
    return tuple(rows)


def scan_basis(x):
    n = len(x)
    base = np.column_stack((np.ones(n), x))
    if np.linalg.matrix_rank(base) != 2:
        raise ValueError("Degenerate null design")
    q = np.linalg.qr(base)[0]
    lo, hi = n // 5, 4 * n // 5
    dates = sorted(set(range(lo, hi + 1, 5)) | {hi})
    bases = []
    for k in dates:
        post = np.arange(n) >= k
        z = np.column_stack((post, post * (x - x[k])))
        z = z - q @ (q.T @ z)
        if np.linalg.matrix_rank(z) != 2:
            raise ValueError("Degenerate split design")
        bases.append(np.linalg.qr(z)[0].T)
    return q, np.vstack(bases), dates


def bootstrap(rows, seed, draws=DRAWS):
    x, y = np.asarray([(r.a, r.b) for r in rows]).T
    q, design, splits = scan_basis(x)
    residual = y - q @ (q.T @ y)
    denom = residual[:-1] @ residual[:-1]
    if denom <= 0:
        raise ValueError("Degenerate residual")
    phi = (residual[1:] @ residual[:-1]) / denom
    if not -.98 < phi < .98:
        raise ValueError("Persistence outside bootstrap support")
    innovation = residual[1:] - phi * residual[:-1]
    innovation -= innovation.mean()
    rng = np.random.default_rng(np.random.SeedSequence([seed, 917]))
    signs = rng.choice((-1., 1.), size=(draws, len(innovation)))
    innovations = np.column_stack((np.zeros(draws), signs * innovation))
    # Condition on the observed first residual and simulate under the no-break fit.
    innovations[:, 0] = residual[0]
    simulations = lfilter([1.], [1., -phi], innovations, axis=1).T
    values = np.column_stack((residual, simulations))
    values -= q @ (q.T @ values)
    ss = (values**2).sum(axis=0)
    if np.any(ss <= 0):
        raise ValueError("Degenerate bootstrap path")
    power = (design @ values).reshape(len(splits), 2, -1)
    reductions = (power**2).sum(axis=1) / ss
    stat = reductions.max(axis=0)
    p = (1 + np.count_nonzero(stat[1:] >= stat[0])) / (draws + 1)
    return float(p), splits[int(reductions[:, 0].argmax())]


def evaluate(case, seed, n):
    rows = series(case, seed, n)
    window = Window(DAYS[0], DAYS[n - 1], DAYS[n - 1], DAYS[n])
    opts = {} if n == 180 else {"sessions": DAYS[:n]}
    def check(step):
        return assess(step, compute(step, rows, window, "BIC", **opts))
    try:
        for step in ("diagnostics", "engle_granger"):
            found = check(step)
            if found["decision"] != "proceed":
                return ((found["rule_id"], False, "", ""),) * 2
        found = check("stability")
        current = (found["rule_id"], found["decision"] == "continue", found["details"].get("cusum_p", ""), "")
    except (ValueError, RuntimeError, ArithmeticError, np.linalg.LinAlgError):
        return (("baseline_tool_error", False, "", ""),) * 2
    if n == 180:
        return current, None
    try:
        p, split = bootstrap(rows, seed)
        target = ("bootstrap_clear" if p >= .05 else "bootstrap_break", p >= .05, p, split)
    except (ValueError, ArithmeticError, np.linalg.LinAlgError):
        target = ("bootstrap_unresolved", False, "", "")
    return current, target


def self_check():
    rows = series("stable_strong", 1, 540)
    p, k = bootstrap(rows, 1)
    assert (p, k) == bootstrap(rows, 1)
    assert 1 / 200 <= p <= 1 and 108 <= k <= 432
    x = np.asarray([r.a for r in rows])
    q, z, _ = scan_basis(x)
    assert np.max(np.abs(z @ q)) < 1e-10
    try:
        bootstrap(tuple(Point(r.day, 0., r.b) for r in rows), 1)
    except ValueError:
        pass
    else:
        raise AssertionError("Constant regressor accepted")
    print("Component checks passed", flush=True)


def main():
    self_check()
    def digest(path):
        return sha256(path.read_bytes()).hexdigest()
    result = {"runtime": numerical_runtime(), "draws": DRAWS, "n_per_scenario": N,
              "holdout": "locked; none accessed or generated", "hashes": {
                  p.name: digest(p) for p in (HERE / "PROTOCOL.md", Path(__file__), PREVIOUS,
                                              Path(sys.modules[compute.__module__].__file__))}, "scenarios": {}}
    with (HERE / "trials.csv").open("x", encoding="ascii", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("scenario", "seed", "arm", "rule", "advanced", "p", "split_index"))
        for i, case in enumerate(SCENARIOS):
            counts = {a: Counter() for a in ARMS}
            for seed in range(310000 + i * N, 310000 + (i + 1) * N):
                base = evaluate(case, seed, 180)[0]
                outcomes = (base, *evaluate(case, seed, 540))
                for arm, (rule, advanced, p, split) in zip(ARMS, outcomes, strict=True):
                    counts[arm][rule] += 1
                    counts[arm]["advanced"] += advanced
                    counts[arm]["gain"] += advanced and not base[1]
                    counts[arm]["loss"] += base[1] and not advanced
                    writer.writerow((case, seed, arm, rule, int(advanced), p, split))
            for count in counts.values():
                count["n"] = N
            result["scenarios"][case] = {
                a: {**c, "wilson_95": list(map(float, proportion_confint(c["advanced"], N, method="wilson")))}
                for a, c in counts.items()}
            output.flush()
            print(case, {a: c["advanced"] for a, c in counts.items()}, flush=True)
    with (HERE / "results.json").open("x", encoding="ascii") as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        self_check()
    else:
        main()
