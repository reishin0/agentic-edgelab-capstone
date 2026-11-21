from dataclasses import asdict

from edgelab.contracts import fingerprint
from edgelab.stat_tools import ALPHA, sample_fingerprint


RUNTIME = {"test": "fixture"}


def roots(order="I(1)"):
    a, k = {"I(1)": (0.6, 0.01), "I(0)": (0.01, 0.1), "unresolved": (0.6, 0.1)}[order]
    return {"order": order, "level": {"adf_p": a, "kpss_p": k},
            "difference": {"adf_p": 0.01, "kpss_p": 0.1}}


def bound(tool, rows, window, lag, result, runtime=RUNTIME):
    config = {"tool": tool, "lag_policy": lag, "alpha": ALPHA, "window": asdict(window),
              "n": len(rows), **runtime, "sample_sha256": sample_fingerprint(rows)}
    return {"config": config, "config_sha256": fingerprint(config),
            "result": result, "result_sha256": fingerprint(result)}
