from contextlib import redirect_stdout
from datetime import date, timedelta
from hashlib import sha256
from importlib.util import find_spec
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from edgelab.capstone import main
from edgelab.history import FrozenHistory
from edgelab.journal import Journal, JournalConflict
from edgelab.methods import local_notes
from edgelab.planning import search
from edgelab.research import ResearchService
from edgelab.stat_tools import synthetic


RUN = "10abb839-4e59-45b4-b23b-f1346d322b31"


def make_export(folder: Path) -> tuple[Path, Path, dict]:
    start = date(2020, 1, 2)
    calendar = tuple(str(start + timedelta(days=i)) for i in range(270)
                     if (start + timedelta(days=i)).weekday() < 5 and i != 4)
    sessions, holdout = calendar[:180], calendar[180]
    source = synthetic("shared-trend")
    csv = folder / "paired.csv"
    csv.write_bytes(("day,a,b\n" + "".join(f"{day},{row.a!r},{row.b!r}\n"
                     for day, row in zip(sessions, source))).encode())
    manifest = folder / "manifest.json"
    doc = {"schema_version": 1, "classification": "synthetic", "source": "Fixed-seed test fixture",
           "calendar": "Illustrative weekday sessions, not a real exchange calendar",
           "file": csv.name, "sha256": sha256(csv.read_bytes()).hexdigest(), "sessions": list(sessions),
           "window": {"start": sessions[0], "end": sessions[-1], "as_of": sessions[-1], "holdout_start": holdout}}
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    return manifest, csv, doc


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.manifest, self.csv, self.doc = make_export(self.folder)
        self.journal = Journal(self.folder / "journal.sqlite")
        self.tool = Mock(return_value={"result": {"a": {"order": "I(1)"}, "b": {"order": "unresolved"}}})

    def service(self):
        return ResearchService(self.journal, history=FrozenHistory.open(self.manifest),
                               numerical_tool=self.tool, runtime_identity=lambda: {"test": "fixture"})

    def plan(self, service):
        return service.record_plan(RUN, None, search(local_notes("cointegration")), "local")["candidates"][0]

    def test_plan_and_wrong_approval_do_not_open_the_csv(self):
        self.csv.unlink()
        service = self.service()
        candidate = self.plan(service)
        self.assertNotIn(self.folder.name, json.dumps(candidate))
        with self.assertRaises(JournalConflict):
            service.approve(RUN, "0" * 64)
        with self.assertRaises(FileNotFoundError):
            service.approve(RUN, candidate["digest"])
        self.assertIsNone(self.journal.get(RUN, "approval"))
        self.tool.assert_not_called()

    def test_manifest_or_csv_drift_blocks_execution(self):
        service = self.service()
        candidate = self.plan(service)
        service.approve(RUN, candidate["digest"])
        original = self.manifest.read_text(encoding="utf-8")
        self.manifest.write_text(original + "\n", encoding="utf-8")
        with self.assertRaisesRegex(JournalConflict, "same frozen historical manifest"):
            self.service().action(RUN, "diagnostics")
        self.manifest.write_text(original, encoding="utf-8")
        self.csv.write_bytes(self.csv.read_bytes().replace(b"day,a,b", b"day,b,a"))
        with self.assertRaisesRegex(ValueError, "frozen SHA-256"):
            self.service().action(RUN, "diagnostics")
        self.assertIsNone(self.journal.get(RUN, "reserved:diagnostics"))
        self.tool.assert_not_called()

    def test_session_gaps_stay_gaps_and_only_approved_rows_reach_the_tool(self):
        service = self.service()
        candidate = self.plan(service)
        service.approve(RUN, candidate["digest"])
        service.action(RUN, "diagnostics")
        days = tuple(row.day for row in self.tool.call_args.args[1])
        self.assertEqual(days, tuple(self.doc["sessions"]))
        self.assertEqual(self.tool.call_args.kwargs["sessions"], days)
        self.assertEqual(len(days), 180)
        self.assertNotIn("2020-01-04", days)
        self.assertNotIn(self.doc["window"]["holdout_start"], days)

    def test_missing_session_and_appended_holdout_are_rejected_even_with_matching_hash(self):
        original = self.csv.read_bytes().splitlines(keepends=True)
        holdout = self.doc["window"]["holdout_start"].encode() + b",1,2\n"
        for rows in (original[:-1], original + [holdout], [original[0], original[1], *original[1:-1]]):
            with self.subTest(count=len(rows)):
                raw = b"".join(rows)
                self.csv.write_bytes(raw)
                self.doc["sha256"] = sha256(raw).hexdigest()
                self.manifest.write_text(json.dumps(self.doc), encoding="utf-8")
                with self.assertRaises(ValueError):
                    FrozenHistory.open(self.manifest).sample()

    @unittest.skipUnless(find_spec("langgraph") and find_spec("statsmodels"), "optional research dependencies absent")
    def test_historical_cli_runs_the_real_numerical_tools(self):
        def invoke(*arguments):
            with patch.object(sys, "argv", ["edgelab.capstone", *arguments]), redirect_stdout(StringIO()) as stdout:
                main()
            return json.loads(stdout.getvalue())

        with patch("edgelab.capstone.JOURNAL_PATH", self.journal.path):
            plan = invoke("plan", "--no-model", "--history-manifest", str(self.manifest))
            run = plan["run_id"]
            packet = invoke("execute", "--run", run, "--approve", plan["candidates"][0]["digest"],
                            "--history-manifest", str(self.manifest))
        self.assertEqual(packet["report"]["verdict"], "continue")
        self.assertEqual(packet["report"]["holdout"], "locked and untested")
        self.assertEqual(packet["report"]["evidence_refs"], ["result:diagnostics", "result:engle_granger", "result:stability"])
        events = {event["key"]: event["value"] for event in packet["bundle"]["events"]}
        for key in packet["report"]["evidence_refs"]:
            config = events[key]["observation"]["config"]
            self.assertEqual(config["n"], 180)
            self.assertEqual(config["sample_sha256"], events["approval"]["sample_sha256"])


if __name__ == "__main__":
    unittest.main()
