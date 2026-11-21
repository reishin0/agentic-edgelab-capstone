"""Repeatable diagnostic, not a unit test or a calibrated power study."""

from collections import Counter
from datetime import date, timedelta
import json
from random import Random

from edgelab.stat_tools import Point, TOOL_SEQUENCE, assess, compute, window_for_demo


SCENARIOS = ("shared_trend", "independent_walks", "slope_reversal", "near_unit_root")
SEEDS = tuple(range(1050, 1070))


def samples(case, seed):
    rng = Random(seed)
    a = b = noise = 0.0
    rows = []
    for i in range(180):
        phi = .98 if case == "near_unit_root" else 1
        a = phi * a + rng.gauss(0, 1)
        b = phi * b + rng.gauss(0, 1)
        noise = .4 * noise + rng.gauss(0, .6)
        slope = -1.2 if case == "slope_reversal" and i >= 90 else 1.3
        value = slope * a + noise if case in {"shared_trend", "slope_reversal"} else b
        rows.append(Point(str(date(2020, 1, 1) + timedelta(days=i)), a, value))
    return tuple(rows)


def main():
    results = {}
    for case in SCENARIOS:
        decisions, rules = Counter(), Counter()
        for seed in SEEDS:
            sample = samples(case, seed)
            for step in TOOL_SEQUENCE:
                observation = compute(step, sample, window_for_demo(), "AIC")
                finding = assess(step, observation)
                if finding["decision"] != "proceed":
                    decisions[finding["decision"]] += 1
                    rules[finding["rule_id"]] += 1
                    break
        results[case] = {"decisions": dict(decisions), "terminal_rules": dict(rules)}
    print(json.dumps({"scope": "fixed-seed synthetic screen; continue is not validation",
                      "seeds_per_case": len(SEEDS), "outcomes": results}, indent=2))


if __name__ == "__main__":
    main()
