from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from uuid import uuid4

from edgelab.contracts import fingerprint
from edgelab.journal import Journal, JournalConflict
from edgelab.methods import local_notes
from edgelab.planning import search
from edgelab.research import ResearchService
from edgelab.stat_tools import assess
from observation_fixtures import RUNTIME, bound, roots


class DecisionRules(unittest.TestCase):
    def test_diagnostic_decision_table_and_false_positive_controls(self):
        for a, b, expected in (
            ("I(1)", "I(1)", "proceed"), ("I(0)", "I(0)", "revise"),
            ("I(0)", "I(1)", "stop"), ("I(1)", "unresolved", "unresolved"),
        ):
            with self.subTest(a=a, b=b):
                finding = assess("diagnostics", {"result": {"a": roots(a), "b": roots(b)}})
                self.assertEqual(finding["decision"], expected)
        biased = roots()
        biased["difference"]["adf_p"] = 0.05
        finding = assess("diagnostics", {"result": {"a": biased, "b": roots()}})
        self.assertEqual(finding["rule_id"], "ROOT_LABEL_MISMATCH")
        self.assertEqual(finding["decision"], "unresolved")
        finding = assess("diagnostics", {"result": {"a": {"order": "I(1)"}, "b": roots()}})
        self.assertEqual(finding["rule_id"], "INVALID_EVIDENCE")

    def test_cusum_nonrejection_does_not_prove_mean_reversion(self):
        values = {"cusum_p": 0.5, "ecm_loading": 0.2, "ecm_loading_p": 0.01,
                  "ar1_phi": 1.02, "ou_half_life_observations": None}
        finding = assess("stability", {"result": values})
        self.assertEqual(finding["rule_id"], "CUSUM_NOT_REJECTED")
        self.assertIn("mean reversion are not established", finding["reason"])
        values["ou_half_life_observations"] = 10
        self.assertEqual(assess("stability", {"result": values})["decision"], "unresolved")
        values["cusum_p"] = 0.01
        self.assertEqual(assess("stability", {"result": values})["rule_id"], "CUSUM_REJECTS")

    def test_wrong_sample_digest_with_consistent_payload_hash_still_cannot_advance(self):
        with TemporaryDirectory() as folder:
            journal = Journal(Path(folder) / "journal.sqlite")

            def wrong_sample(*args):
                obs = bound(*args, {"a": roots(), "b": roots()})
                obs["config"]["sample_sha256"] = "1" * 64
                obs["config_sha256"] = fingerprint(obs["config"])
                return obs

            tool = Mock(side_effect=wrong_sample)
            service = ResearchService(journal, numerical_tool=tool, runtime_identity=lambda: RUNTIME)
            run = str(uuid4())
            plan = service.record_plan(run, "shared-trend", search(local_notes("cointegration")), "local")
            service.approve(run, plan["candidates"][0]["digest"])
            observation = service.action(run, "diagnostics")
            finding = service.critique(run, "diagnostics")
            self.assertEqual(finding["rule_id"], "EVIDENCE_BINDING_FAILED")
            self.assertEqual(finding["evidence_sha256"], fingerprint(observation))
            with self.assertRaises(JournalConflict):
                service.action(run, "engle_granger")
            self.assertEqual(service.finalize(run)["report"]["verdict"], "unresolved")
            tool.assert_called_once()
