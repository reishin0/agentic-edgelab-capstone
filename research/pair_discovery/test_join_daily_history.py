import csv
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.pair_discovery import join_daily_history as history


class JoinDailyHistory(unittest.TestCase):
    def packets(self):
        raw = {"schema": 1, "feed": "sip", "adjustment": "raw", "window": {"start": "2024-01-02", "end": "2024-01-08"},
               "bars": {"A": {stamp: {"close": 80, "volume": 500_000} for stamp in ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")},
                        "B": {"2024-01-02": {"close": 60, "volume": 250_000}}}}
        actions = {"schema": 1, "source": "fixture archive", "instruments": {"A": {
            "covered": [["2024-01-02", "2024-01-04"], ["2024-01-08", "2024-01-08"]],
            "events": {"2024-01-03": {"split": 2, "distribution": .5, "known_on": "2024-01-02", "evidence": "fixture issuer"}},
            "unresolved": {"2024-01-04": "merger terms not reconciled"},
        }}}
        return raw, actions

    def test_only_certified_sessions_become_screen_rows(self):
        raw, actions = self.packets()
        encoded, coverage = history.join(raw, actions)
        rows = list(csv.DictReader(encoded.decode().splitlines()))
        self.assertEqual([(row["session"], row["split_ratio"], row["action_known_on"]) for row in rows],
                         [("2024-01-02", "1", "2024-01-02"), ("2024-01-03", "2", "2024-01-02")])
        self.assertEqual(coverage["A"]["counts"], {"written": 2, "unresolved_action": 1, "action_coverage_missing": 1, "bar_missing": 1})
        self.assertEqual(coverage["B"]["counts"], {"action_coverage_missing": 5})

    def test_denies_future_actions_and_bars(self):
        raw, actions = self.packets()
        actions["instruments"]["A"]["events"]["2024-01-03"]["known_on"] = "2024-01-05"
        with self.assertRaisesRegex(ValueError, "Unverifiable effective corporate action"):
            history.join(raw, actions)
        raw["bars"]["A"]["2025-11-03"] = {"close": 1, "volume": 1}
        with self.assertRaisesRegex(ValueError, "out-of-scope session"):
            history.join(raw, actions)

    def test_receipt_pins_inputs_and_output_bytes(self):
        raw, actions = self.packets()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = []
            for number, packet in enumerate((raw, actions)):
                path = root / f"packet-{number}.json"
                path.write_text(json.dumps(packet), encoding="utf-8")
                files.extend((path, sha256(path.read_bytes()).hexdigest()))
            exported = root / "output"
            result = history.assemble(*files, exported)
            self.assertEqual(result["output_sha256"], sha256((exported / "daily_history.csv").read_bytes()).hexdigest())
            files[-1] = "0" * 64
            with self.assertRaisesRegex(ValueError, "Source digest mismatch"):
                history.assemble(*files, root / "invalid")
            self.assertFalse((root / "invalid").exists())
