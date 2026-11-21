from math import exp
from hashlib import sha256
import json
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest

from research.pair_discovery import screen


def fixture(missing_formation=False):
    eligibility, formation, calibration = screen.development_sessions()
    members = [{"id": key, "issuer": key, "kind": "common_stock", "sector": "fixture", "country": "US",
                "primary_listing": "fixture", "known_on": screen.ELIGIBILITY_END} for key in ("A", "B", "C")]
    sessions = eligibility + formation + calibration
    rng = Random(2224)
    bars = {key: {} for key in ("A", "B", "C")}
    shared = independent = spread = 0
    for stamp in sessions:
        shared += rng.gauss(0, .012)
        independent += rng.gauss(0, .012)
        spread = .82 * spread + rng.gauss(0, .006)
        for key, value in zip(bars, (shared, shared * 1.2 + spread, independent), strict=True):
            bars[key][stamp] = {"close": 100 * exp(value), "volume": 1_000_000,
                                "split": 1, "distribution": 0, "action_known_on": stamp}
    if missing_formation:
        del bars["C"][formation[5]]
    return {"calendar": {"calendar": "synthetic", "sessions": list(sessions)}, "bars": bars,
            "universe": {"index": "IWB", "effective": "2023-09-30", "published": "2023-11-24", "members": members}}


def executable_fixture():
    case = fixture(True)
    rng = Random(27)
    trend = spread = 0.0
    for stamp in case["calendar"]["sessions"]:
        trend += rng.gauss(0, .012 * 1.5)
        spread = .74 * spread + rng.gauss(0, .005)
        for key, level in (("A", 1.2 * trend + spread), ("B", trend)):
            case["bars"][key][stamp]["close"] = 100 * exp(level)
    case["execution"] = {"A": {}, "B": {}}
    for stamp in screen.development_sessions()[2]:
        close_ms = (13 if stamp == screen.EARLY_CLOSE_2025 else 16) * 3_600_000
        opening = (9 * 60 + 35) * 60_000
        for key, records in case["execution"].items():
            close = case["bars"][key][stamp]["close"]
            quote = {"bid": close - .01, "ask": close + .01, "bid_size": 10_000, "ask_size": 10_000}
            records[stamp] = {
                "quotes": [{**quote, "at_ms": opening}, {**quote, "at_ms": opening + 60_000}],
                "mark": {**quote, "at_ms": close_ms}, "median_volume": 1_000_000,
                "borrow": [{"at_ms": 0, "available_shares": 10_000, "rate_bps": 50}],
                "financing_bps": 100, "commission_per_share": .001, "sell_fee_bps": .02,
            }
    return case


class DiscoveryScreen(unittest.TestCase):
    def test_fixed_family_counts_missing_formation_as_one(self):
        report = screen.screen_development({"classification": "synthetic-development"}, fixture(True))
        self.assertEqual(len(report["pairs"]), 3)
        incomplete = [row for row in report["pairs"] if "C" in row["pair"]]
        self.assertTrue(all(row["formation"]["p"] == 1 and row["holm_p"] == 1 and row["verdict"] == "unresolved"
                            for row in incomplete))
        self.assertEqual(report["shortlist"], [])
        self.assertEqual(report["tradable_pairs"], [])

    def test_rejects_incomplete_eligibility(self):
        case = fixture()
        del case["bars"]["A"][case["calendar"]["sessions"][0]]
        report = screen.screen_development({}, case)
        self.assertEqual(report["verdict"], "unresolved")
        self.assertEqual(report["pairs"], [])

    def test_frozen_hedge_runs_both_execution_arms(self):
        report = screen.screen_development({"classification": "synthetic-development"}, executable_fixture(),
                                           execution=screen.calibration_replay)
        row = report["pairs"][0]
        self.assertEqual(row["pair"], ["A", "B"])
        self.assertTrue(all(check["passes"] for check in row["rolling_checks"].values()))
        for mode in ("base", "stress"):
            data = row["calibration"][mode]
            self.assertEqual(data["status"], "complete")
            self.assertGreater(data["round_trips"], 0)
            self.assertIsNotNone(data["max_drawdown"])
        self.assertEqual(report["tradable_pairs"], [])

    def test_calibration_rejects_execution_drift_and_keeps_missing_record_unresolved(self):
        _, _, calibration = screen.development_sessions()
        stamp = calibration[0]
        cases = (("split_ratio", 2, "corporate actions differ"),
                 ("dividend", .20, "corporate actions differ"),
                 ("median_volume", 2_000_000, "Trailing consolidated volume differs"),
                 ("median_volume", None, "Trailing consolidated volume differs"),
                 (None, None, "missing_execution_leg_session"))
        for field, value, reason in cases:
            with self.subTest(field=field, value=value):
                case = executable_fixture()
                if field is None:
                    del case["execution"]["A"][stamp]
                else:
                    case["execution"]["A"][stamp][field] = value
                report = screen.screen_development({"classification": "synthetic-development"}, case,
                                                   execution=screen.calibration_replay)
                row = report["pairs"][0]
                self.assertEqual(row["pair"], ["A", "B"])
                self.assertTrue(all(check["passes"] for check in row["rolling_checks"].values()))
                self.assertEqual(row["verdict"], "unresolved")
                self.assertEqual(row["reason"], "execution_evidence_incomplete")
                self.assertEqual(report["shortlist"], [])
                for arm in row["calibration"].values():
                    self.assertEqual(arm["status"], "unresolved")
                    self.assertIn(reason, arm["reason"])
                    if field is not None:
                        self.assertIn(f"on {stamp} for A", arm["reason"])

    def test_calibration_trailing_volume_excludes_current_session_and_matches_actions(self):
        _, formation, calibration = screen.development_sessions()
        sessions = calibration[:3]
        case = executable_fixture()
        bars, records = case["bars"]["A"], case["execution"]["A"]
        for offset, stamp in enumerate(formation[-20:], 1):
            bars[stamp]["volume"] = offset * 100_000
        for offset, stamp in enumerate(sessions):
            bars[stamp]["volume"] = 9_900_000
            records[stamp]["median_volume"] = (10.5 + offset) * 100_000
        bars[sessions[0]].update(split=2, distribution=.25)
        records[sessions[0]].update(split_ratio=2, dividend=.25)
        z = dict.fromkeys((formation[-1], *sessions), 0.0)
        base, stress = screen.calibration_replay(case["execution"], "A", "B", sessions, z, formation[-1], {}, 1,
                                                 bars=case["bars"], formation=formation)
        for arm in (base, stress):
            self.assertEqual(arm["status"], "complete")
            self.assertEqual(arm["entry_attempts"], 0)

    def test_all_hypotheses_remain_in_holm_denominator(self):
        actual = screen.holm_adjust({("A", "B"): .01, ("A", "C"): .03, ("B", "C"): 1})
        self.assertEqual(actual, {("A", "B"): .03, ("A", "C"): .06, ("B", "C"): 1})

    def test_rejects_late_scope_before_any_payload_access(self):
        with TemporaryDirectory() as temp:
            manifest = {"schema_version": 1, "classification": "synthetic-development", "through": screen.CALIBRATION_END,
                        "holdout_start": screen.HOLDOUT_START, "files": {}}
            for kind, filename in screen.PAYLOADS.items():
                manifest["files"][kind] = {"name": filename, "source": "fixture", "sha256": "0" * 64,
                                           "through": screen.HOLDOUT_START if kind == "bars" else screen.CALIBRATION_END}
            path = Path(temp, "manifest.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsafe development input record for bars"):
                screen.load_development(path)

    def test_frozen_files_detect_tampering(self):
        with TemporaryDirectory() as temp:
            manifest = {"schema_version": 1, "classification": "synthetic-development", "through": screen.CALIBRATION_END,
                        "holdout_start": screen.HOLDOUT_START, "files": {}}
            for kind, filename in screen.PAYLOADS.items():
                raw = json.dumps(fixture()[kind]).encode()
                Path(temp, filename).write_bytes(raw)
                manifest["files"][kind] = {"name": filename, "source": "fixture", "sha256": sha256(raw).hexdigest(),
                                           "through": screen.CALIBRATION_END}
            path = Path(temp, "manifest.json")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(screen.load_development(path)[0], manifest)
            Path(temp, "bars.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest mismatch for bars"):
                screen.load_development(path)

    def test_forward_adjustment_uses_actions_only_when_effective(self):
        stamps = ("2024-01-02", "2024-01-03", "2024-01-04")
        bars = {stamp: {"split": split, "distribution": dist, "close": close}
                for stamp, split, dist, close in zip(stamps, (1, 2, 1), (0, 0, 1), (100, 50, 49), strict=True)}
        actual = screen.log_total_return(bars, stamps)
        for value in actual.values():
            self.assertAlmostEqual(value, 0)


if __name__ == "__main__":
    unittest.main()
