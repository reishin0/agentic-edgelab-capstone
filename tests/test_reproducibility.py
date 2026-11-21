from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from edgelab.journal import Journal, JournalConflict
from edgelab.methods import PineconeMethods, local_notes
from edgelab.planning import search
from edgelab.providers import LMStudio
from edgelab.research import ResearchService
from edgelab.stat_tools import synthetic


RUN = "daff3a76-6c18-4238-82b3-a2ec2774073b"


class ReproducibilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.journal = Journal(self.folder / "run.sqlite")
        self.tool = Mock(return_value={"result": {"a": {"order": "I(1)"}, "b": {"order": "I(1)"}}})
        self.versions = {"statsmodels": "0.14-fixture", "numpy": "2.0-fixture", "scipy": "1.0-fixture"}
        self.enterContext(patch("edgelab.stat_tools.version", side_effect=self.versions.__getitem__))
        self.service = ResearchService(self.journal, numerical_tool=self.tool)

    def approve(self):
        plan = self.service.record_plan(RUN, "shared-trend", search(local_notes("cointegration")), "local")
        return self.service.approve(RUN, plan["candidates"][0]["digest"])

    def test_numpy_and_scipy_drift_block_resume(self):
        approval = self.approve()
        for package in ("numpy", "scipy"):
            with self.subTest(package=package):
                original = self.versions[package]
                self.versions[package] += "-changed"
                try:
                    restarted = ResearchService(Journal(self.journal.path), numerical_tool=self.tool)
                    with self.assertRaises(JournalConflict):
                        restarted.approve(RUN, approval["digest"])
                    with self.assertRaises(JournalConflict):
                        restarted.action(RUN, "diagnostics")
                finally:
                    self.versions[package] = original
        self.tool.assert_not_called()
        self.assertIsNone(self.journal.get(RUN, "reserved:diagnostics"))

    def test_same_window_with_changed_values_cannot_execute(self):
        self.approve()
        first, *rest = synthetic("shared-trend")
        changed = (replace(first, a=first.a + 0.5), *rest)
        with patch("edgelab.research.synthetic", return_value=changed):
            with self.assertRaises(JournalConflict):
                self.service.action(RUN, "diagnostics")
        self.tool.assert_not_called()
        self.assertIsNone(self.journal.get(RUN, "reserved:diagnostics"))

    def test_orphaned_approval_cannot_authorize_a_tool(self):
        approval = self.approve()
        other = Journal(self.folder / "orphan.sqlite")
        other.append(RUN, "approval", approval)
        with self.assertRaises(JournalConflict):
            ResearchService(other, numerical_tool=self.tool).action(RUN, "diagnostics")
        self.tool.assert_not_called()

    def test_plan_requires_retained_branch_expected_score_and_consistent_status(self):
        for mutation in ("branch", "score", "status"):
            with self.subTest(mutation=mutation):
                plan = search(local_notes("cointegration"))
                if mutation == "branch":
                    plan["branches"] = [row for row in plan["branches"] if row["depth"] != 4]
                elif mutation == "score":
                    plan["candidates"][0]["score"] = 10
                else:
                    plan["status"] = "unresolved"
                with self.assertRaises(JournalConflict):
                    self.service.record_plan(RUN, "shared-trend", plan, "local")
                self.assertEqual(self.journal.events(RUN), [])


class ConnectorRegressions(unittest.TestCase):
    def test_endpoint_and_role_configuration_do_not_drift_after_validation(self):
        models = {"planner": "fixture-model"}
        lm = LMStudio("http://127.0.0.1:1234/v1", models)
        pinecone = PineconeMethods("methods-000.svc.us-east1.pinecone.io", "fixture-key")
        models["planner"] = "other-model"
        self.assertEqual(lm.models["planner"], "fixture-model")
        for client, name in ((lm, "base_url"), (pinecone, "index_host")):
            with self.subTest(client=type(client).__name__), self.assertRaises(FrozenInstanceError):
                setattr(client, name, "https://outside.example")

    def test_invalid_endpoint_does_not_echo_configuration(self):
        marker = "should-not-appear"
        with self.assertRaises(ValueError) as caught:
            LMStudio(f"http://127.0.0.1:{marker}/v1", {})
        self.assertNotIn(marker, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
