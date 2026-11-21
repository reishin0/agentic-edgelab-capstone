"""Evaluate frozen economic gates from already-authorized replay evidence."""

from math import isfinite


THRESHOLDS = {
    "base": {"round_trips": 20, "entry_fill_fraction": 0.8, "quote_coverage_fraction": 0.95,
             "net_return": 0.02, "max_drawdown": 0.05},
    "stress": {"round_trips": 15, "net_return": 0.0, "max_drawdown": 0.075},
}


def decide(base, stress):
    """A research verdict from complete replay summaries. This API has no data access."""
    gates = {}
    for mode, result in (("base", base), ("stress", stress)):
        fields = THRESHOLDS[mode]
        if (result.get("status") != "complete" or result.get("missing_evidence") != []
                or type(result.get("stability_failure")) is not bool
                or not isinstance(result.get("terminal_shares"), (tuple, list))
                or len(result["terminal_shares"]) != 2
                or any(type(x) is not int for x in result["terminal_shares"])
                or any(type(result.get(key)) not in (int, float) or not isfinite(result[key]) for key in fields)
                or type(result["round_trips"]) is not int or result["round_trips"] < 0
                or result["max_drawdown"] < 0
                or any(not 0 <= result[key] <= 1 for key in fields if key.endswith("fraction"))):
            return {"verdict": "unresolved", "passed_numeric_gates": False, "reason": f"{mode}_evidence_incomplete"}
        gates[mode] = {"stability": not result["stability_failure"],
                       "flat": all(shares == 0 for shares in result["terminal_shares"])}
        for key, threshold in fields.items():
            measured = result[key]
            if key == "max_drawdown":
                passes = measured <= threshold
            elif mode == "stress" and key == "net_return":
                passes = measured > threshold
            else:
                passes = measured >= threshold
            gates[mode][key] = passes
    passed = all(passes for result in gates.values() for passes in result.values())
    return {"verdict": "continue researching" if passed else "stop", "passed_numeric_gates": passed, "gates": gates}
