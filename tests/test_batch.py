from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from edgelab import batch
from edgelab.history import FrozenHistory
from edgelab.journal import Journal, JournalConflict
from edgelab.reporting import report_from_journal
from test_history import make_export


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.journal = Journal(self.root / "batch.sqlite")

    def test_identical_batch_is_reused_and_results_persist_without_reexecution(self):
        cases = ("shared-trend", "spread-level-shift")
        plan = batch.plan_batch(self.journal, cases=cases)
        self.assertEqual(plan["status"], "awaiting_approval")
        self.assertEqual(batch.plan_batch(self.journal, cases=cases[::-1]), plan)
        with self.assertRaisesRegex(JournalConflict, "exact displayed"):
            batch.execute_batch(self.journal, plan["batch_id"], "0" * 64)
        self.assertIsNone(self.journal.get(plan["batch_id"], "batch:approval"))
        result = batch.execute_batch(self.journal, plan["batch_id"], plan["batch_sha256"])
        self.assertEqual(result["status"], "finished")
        self.assertEqual({r["label"]: r["verdict"] for r in result["runs"]},
                         {"shared-trend": "continue", "spread-level-shift": "stop"})
        self.assertTrue(all(r["holdout"] == "locked and untested" for r in result["runs"]))
        reader = Journal(self.journal.path, read_only=True)
        before = self.journal.path.read_bytes()
        with patch("edgelab.batch._invoke", side_effect=AssertionError("must not run again")):
            self.assertEqual(batch.execute_batch(reader, plan["batch_id"], plan["batch_sha256"]), result)
            self.assertEqual(batch.show_batch(reader, plan["batch_id"]), result)
        for run in result["runs"]:
            packet = report_from_journal(reader, run["run_id"])
            self.assertEqual(packet["bundle"]["ledger_sha256"], run["ledger_sha256"])
        self.assertEqual(before, self.journal.path.read_bytes())

    def test_history_is_validated_before_any_member_is_claimed(self):
        manifest, csv, _ = make_export(self.root)
        history = FrozenHistory.open(manifest)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            batch.plan_batch(self.journal, histories=(history, history))
        before = csv.read_bytes()
        csv.unlink()
        plan = batch.plan_batch(self.journal, histories=(history,))
        with self.assertRaisesRegex(JournalConflict, "exactly the frozen"):
            batch.execute_batch(self.journal, plan["batch_id"], plan["batch_sha256"])
        csv.write_bytes(before + b"\n")
        with patch("edgelab.batch._invoke", side_effect=AssertionError("must not call statistics")):
            with self.assertRaisesRegex(ValueError, "frozen SHA-256"):
                batch.execute_batch(self.journal, plan["batch_id"], plan["batch_sha256"], histories=(history,))
        self.assertIsNone(self.journal.get(plan["batch_id"], "batch:approval"))
        run_id = plan["runs"][0]["run_id"]
        self.assertIsNone(self.journal.get(run_id, "approval"))
        csv.write_bytes(before)
        result = batch.execute_batch(self.journal, plan["batch_id"], plan["batch_sha256"], histories=(history,))
        self.assertEqual(result["status"], "finished")
        approval = self.journal.get(run_id, "approval")
        self.assertEqual(approval["protocol"]["history"], history.identity())
        self.assertEqual(approval["sample_n"], len(history.sessions))

    def test_blocked_batch_preserves_completed_runs_and_does_not_retry(self):
        plan = batch.plan_batch(self.journal, cases=("shared-trend", "spread-level-shift", "independent"))
        original = batch._invoke
        failed_id = plan["runs"][1]["run_id"]

        def fail_second(journal, history, **state):
            if state["run_id"] == failed_id:
                raise RuntimeError("fixture infrastructure failure")
            return original(journal, history, **state)

        with patch("edgelab.batch._invoke", side_effect=fail_second) as invoked:
            result = batch.execute_batch(self.journal, plan["batch_id"], plan["batch_sha256"])
            self.assertEqual(invoked.call_count, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked"], {"run_id": failed_id, "error": "RuntimeError"})
        self.assertEqual([r["status"] for r in result["runs"]],
                         ["finished", "awaiting_approval", "awaiting_approval"])
        with patch("edgelab.batch._invoke", side_effect=AssertionError("must not retry")):
            self.assertEqual(batch.execute_batch(self.journal, plan["batch_id"], plan["batch_sha256"]), result)
