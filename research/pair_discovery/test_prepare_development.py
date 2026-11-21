import csv
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.pair_discovery import prepare_development as prep, screen
from research.pair_discovery.test_screen import executable_fixture


class DevelopmentExportTests(unittest.TestCase):
    def sources(self, folder, *, late_scope=False, late_row=False):
        fixture = executable_fixture()
        (folder / "universe.json").write_text(json.dumps(fixture["universe"]), encoding="utf-8")
        (folder / "execution.json").write_text(json.dumps(fixture["execution"]), encoding="utf-8")
        with (folder / "daily_history.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(prep.NORMALIZED_BAR_FIELDS)
            for instrument, rows in fixture["bars"].items():
                for stamp, row in rows.items():
                    writer.writerow((instrument, stamp, row["close"], row["volume"], row["split"],
                                     row["distribution"], row["action_known_on"]))
            if late_row:
                writer.writerow(("A", screen.HOLDOUT_START, 100, 10, 1, 0, screen.HOLDOUT_START))
        records = {}
        for key, name in {**prep.REQUIRED, **prep.OPTIONAL}.items():
            through = screen.ELIGIBILITY_END if key == "universe" else screen.CALIBRATION_END
            if late_scope and key == "daily_history":
                through = screen.HOLDOUT_START
            records[key] = {"name": name, "sha256": sha256((folder / name).read_bytes()).hexdigest(),
                            "source": "synthetic fixture", "through": through}
        receipt = folder / "receipt.json"
        receipt.write_text(json.dumps({"schema": 1, "classification": "synthetic-development", "files": records}),
                           encoding="utf-8")
        return receipt

    def test_normalized_sources_produce_replayable_manifest(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            target = folder / "frozen"
            receipt = self.sources(folder)
            manifest = prep.prepare(receipt, target)
            loaded, payloads = screen.load_development(target / "manifest.json")
            self.assertEqual(loaded, manifest)
            self.assertIn("input SHA-256", manifest["files"]["bars"]["source"])
            report = screen.screen_development(loaded, payloads, execution=screen.calibration_replay)
            self.assertEqual(report["pairs"][0]["calibration"]["base"]["round_trips"], 7)
            self.assertEqual(report["holdout"], "locked and untested")
            coverage = json.loads((target / "coverage.json").read_bytes())["instruments"]
            self.assertEqual(coverage["C"]["missing_formation"], 1)

    def test_denies_late_metadata_before_opening_sources(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            receipt = self.sources(folder, late_scope=True)
            (folder / "daily_history.csv").unlink()
            with self.assertRaisesRegex(ValueError, "Unsafe or incomplete normalized source scope"):
                prep.prepare(receipt, folder / "frozen")

    def test_denies_an_out_of_scope_observation_with_matching_digest(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            receipt = self.sources(folder, late_row=True)
            with self.assertRaisesRegex(ValueError, "Daily history outside declared development scope"):
                prep.prepare(receipt, folder / "frozen")
            self.assertFalse((folder / "frozen").exists())
