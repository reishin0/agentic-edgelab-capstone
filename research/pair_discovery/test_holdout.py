"""Synthetic tests for exact, one-use EdgeLab holdout orchestration."""

from copy import deepcopy
from hashlib import sha256
import json
from math import exp
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from edgelab import stat_tools
from edgelab.journal import Journal
from research.pair_discovery import execution, holdout, screen
from research.pair_discovery.approval import ACTION, LEDGER, HoldoutAuthority, HoldoutDenied, VerifiedApproval, manifest_digest
from research.pair_discovery.test_screen import fixture


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _quote(close, at_ms):
    return {"at_ms": at_ms, "bid": close - 0.01, "ask": close + 0.01, "bid_size": 10_000, "ask_size": 10_000}


def _leg(close, opening, close_time):
    return {
        "quotes": [_quote(opening, execution.OPEN_MS), _quote(opening, execution.OPEN_MS + execution.LEG_DELAY_MS)],
        "mark": _quote(close, close_time), "median_volume": 1_000_000,
        "borrow": [{"at_ms": 0, "available_shares": 10_000, "rate_bps": 50}],
        "financing_bps": 100, "commission_per_share": 0.001, "sell_fee_bps": 0.02,
        "halted": False, "split_ratio": 1, "cash_in_lieu": None, "dividend": 0, "settlement": None,
    }


def synthetic_plan(folder):
    """Freeze separate development and lease-only synthetic holdout artifacts."""
    root = Path(folder, "development")
    root.mkdir(parents=True)
    case = fixture(True)
    _, _, calibration = screen.development_sessions()
    case["execution"] = {"A": {}, "B": {}}
    holdout_bars = {"A": {}, "B": {}}
    holdout_execution = {"A": {}, "B": {}}
    schedule = holdout.holdout_calendar()
    rng = Random(27)
    trend = spread = previous_offset = 0.0
    sessions = tuple(case["calendar"]["sessions"]) + tuple(schedule)
    for index, stamp in enumerate(sessions):
        trend += rng.gauss(0, 0.018)
        spread = 0.74 * spread + rng.gauss(0, 0.005)
        offset = 0.0
        if (stamp in calibration or stamp in schedule) and index % 9 == 1:
            offset = 0.024 if (index // 9) % 2 else -0.024
        for key, level in (("A", 1.2 * trend + spread + offset), ("B", trend)):
            close = 100 * exp(level)
            if stamp not in schedule:
                case["bars"][key][stamp]["close"] = close
            opening = close * exp(previous_offset * 0.75) if key == "A" else close
            if stamp in schedule:
                holdout_bars[key][stamp] = {"close": close, "volume": 1_000_000, "split": 1,
                                           "distribution": 0, "action_known_on": stamp}
                holdout_execution[key][stamp] = _leg(close, opening, schedule[stamp])
            elif stamp in calibration:
                close_time = (13 if stamp == screen.EARLY_CLOSE_2025 else 16) * 3_600_000
                case["execution"][key][stamp] = _leg(close, opening, close_time)
        previous_offset = offset
    files = {}
    for key, value in case.items():
        name = key + ".json"
        raw = encode(value)
        (root / name).write_bytes(raw)
        files[key] = {"name": name, "sha256": sha256(raw).hexdigest(), "source": "synthetic holdout test",
                      "through": screen.CALIBRATION_END}
    source = {"schema_version": 1, "classification": "synthetic-development", "through": screen.CALIBRATION_END,
              "holdout_start": screen.HOLDOUT_START, "files": files}
    source_path = root / "manifest.json"
    source_path.write_bytes(encode(source))
    report = screen.screen_development(source, case, execution=screen.calibration_replay)
    report["execution_sha256"] = holdout.artifact_digest(execution.__file__)
    if report["shortlist"] != [["A", "B"]]:
        raise AssertionError(f"Synthetic development pair did not qualify: {[(r['pair'], r['reason']) for r in report['pairs']]}")
    report_path = root / "report.json"
    report_path.write_bytes(encode(report))
    input_pins = {"manifest": holdout.artifact_digest(source_path), "report": holdout.artifact_digest(report_path),
                  **{key: value["sha256"] for key, value in files.items()}}
    data = {"calendar.json": {"calendar": holdout.CALENDAR_ID, "close_ms": schedule},
            "bars.json": holdout_bars, "execution.json": holdout_execution}
    scope = {"start": screen.HOLDOUT_START, "end": holdout.HOLDOUT_END, "snapshot_id": "fixture-window"}
    artifacts = {
        name: encode({"schema_version": 1, "classification": "synthetic-holdout", "instrument_ids": ["A", "B"],
                      **scope, "fields": list(holdout.ARTIFACT_FIELDS[name]), "data": packet})
        for name, packet in data.items()
    }
    pins = {}
    edgelab_package = Path(stat_tools.__file__).parent
    for name in holdout.REQUIRED_CODE_PATHS:
        base = Path(holdout.__file__).parent if name.startswith("research/") else edgelab_package
        pins[name] = holdout.artifact_digest(base / Path(name).name)
    plan = {"schema": 1, "run_id": str(uuid4()), "action": ACTION, "output_directory": "fixtures/result",
            "protocol": {"id": holdout.PROTOCOL_ID, "sha256": holdout.artifact_digest(holdout.PROTOCOL_PATH)},
            "code": {"revision": "1" * 40, "files": pins, "dependency_lock_sha256": "2" * 64},
            "development": {"start": holdout.development_start(), "end": screen.CALIBRATION_END, "inputs": input_pins},
            "holdout": {**scope, "universe": "us-equity",
                        "artifacts": [{"path": name, "fields": list(holdout.ARTIFACT_FIELDS[name]),
                                       "sha256": sha256(raw).hexdigest()} for name, raw in artifacts.items()]},
            "parameters": holdout.frozen_parameters(), "candidates": holdout.development_candidates(report)}
    return plan, source_path, report_path, artifacts


class HoldoutRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.fixture = synthetic_plan(Path(directory.name))

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plan, self.source, self.report, self.artifacts = deepcopy(self.fixture)
        self.reads = []
        self.db = self.root / "authority.db"
        self.receipts = {}
        self.authority = HoldoutAuthority(self.db, expected_principal="fixture-researcher",
                                          verify_receipt=self.receipts.get, verify_environment=lambda _: True)

    def ticket(self):
        receipt = str(uuid4())
        self.receipts[receipt] = VerifiedApproval(receipt, "fixture-researcher", "2026-11-14T12:00:00Z",
                                                  self.plan["run_id"], manifest_digest(self.plan), ACTION)
        return receipt

    def reader(self, request):
        self.reads.append(request)
        claim = Journal(self.db).get(LEDGER, f"claim/{self.plan['run_id']}")
        self.assertEqual(claim["manifest_sha256"], manifest_digest(self.plan))
        self.assertEqual(request.instrument_ids, ("A", "B"))
        self.assertEqual(request.fields, holdout.ARTIFACT_FIELDS[request.path])
        return self.artifacts[request.path]

    def run_fixture(self, ticket, **kwargs):
        return holdout.run_holdout(self.authority, self.plan, ticket, development_manifest_path=self.source,
                                   development_report_path=self.report, read_artifact=self.reader,
                                   output_root=self.root, **kwargs)

    def test_complete_holdout_replays_and_journal_recovers_publication(self):
        ticket = self.ticket()
        with patch.object(holdout, "_publish", side_effect=OSError("interrupted publication")):
            with self.assertRaisesRegex(OSError, "interrupted publication"):
                self.run_fixture(ticket)
        self.assertEqual(len(self.reads), 3)
        self.assertFalse((self.root / self.plan["output_directory"]).exists())
        done = self.authority.completed_result(self.plan, ticket)
        pair = done["result"]["pairs"][0]
        self.assertEqual(pair["pair"], ["A", "B"])
        self.assertEqual(len(pair["monthly_checks"]), 12)
        for mode in ("base", "stress"):
            self.assertEqual(pair[mode]["status"], "complete")
            self.assertGreater(pair[mode]["round_trips"], 0)
            self.assertGreater(pair[mode]["net_return"], 0)
            self.assertGreater(pair[mode]["max_drawdown"], 0)
            self.assertTrue(holdout._arm_reconciles(pair[mode]))
        self.assertGreater(pair["base"]["net_return"], pair["stress"]["net_return"])
        self.assertFalse(pair["tradable_under_tested_assumptions"])
        self.assertEqual(done["result"]["tradable_pairs"], [])
        result_path = self.run_fixture(ticket, resume=True)
        self.assertEqual(json.loads(result_path.read_bytes()), done["result"])
        self.assertEqual(len(self.reads), 3)
        with self.assertRaises(HoldoutDenied):
            self.run_fixture(ticket)

    def test_no_payload_access_for_missing_approval_or_changed_development(self):
        for invalid in (None, "invalid"):
            with self.assertRaises(HoldoutDenied):
                self.run_fixture(invalid)
        self.plan["candidates"][0]["beta"] += 0.01
        with self.assertRaisesRegex(HoldoutDenied, "Frozen shortlist or formation hedge differs"):
            self.run_fixture(self.ticket())
        self.assertEqual(self.reads, [])
        self.assertEqual(Journal(self.db).events(LEDGER), [])

    def test_rejects_out_of_scope_artifacts_even_with_matching_hash(self):
        name = "bars.json"
        value = json.loads(self.artifacts[name])
        value["data"]["unapproved-id"] = {}
        self.artifacts[name] = changed = encode(value)
        item = next(item for item in self.plan["holdout"]["artifacts"] if item["path"] == name)
        item["sha256"] = sha256(changed).hexdigest()
        ticket = self.ticket()
        with self.assertRaisesRegex(HoldoutDenied, "Unexpected instrument set"):
            self.run_fixture(ticket)
        self.assertEqual(len(self.reads), 3)
        self.assertIsNone(self.authority.completed_result(self.plan, ticket))
        self.assertFalse((self.root / self.plan["output_directory"]).exists())

    def test_numeric_pass_does_not_label_a_synthetic_pair(self):
        ticket = self.ticket()
        with patch.object(holdout.acceptance, "decide", return_value={"verdict": "continue researching",
                                                                      "passed_numeric_gates": True}):
            report = json.loads(self.run_fixture(ticket).read_bytes())
        self.assertTrue(report["pairs"][0]["passed_numeric_gates"])
        self.assertFalse(report["pairs"][0]["tradable_under_tested_assumptions"])
        self.assertEqual(report["tradable_pairs"], [])
