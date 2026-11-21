"""Checkpoint 5.1 acceptance cases. Every subcase must pass."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from edgelab.contracts import fingerprint
from edgelab.graph import build_graph
from edgelab.journal import Journal, JournalConflict
from edgelab.mcp_service import dispatch
from edgelab.methods import local_notes
from edgelab.reporting import report_from_journal
from edgelab.research import ResearchService
from observation_fixtures import RUNTIME, bound, roots


class ContractAcceptance(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.journal = Journal(Path(temp.name) / "test.sqlite")
        self.numerical = Mock()
        self.retrieve = Mock(side_effect=local_notes)
        self.service = ResearchService(self.journal, numerical_tool=self.numerical,
                                       runtime_identity=lambda: RUNTIME)
        self.graph = build_graph(self.service, self.retrieve)
        self.run_id = str(uuid4())
        self.request = {"version": 1, "mode": "plan", "run_id": self.run_id,
                        "backend": "local", "case": "shared-trend"}

    def plan(self):
        return self.graph.invoke(self.request)["output"]

    def execute(self, plan):
        return self.graph.invoke({"version": 1, "mode": "execute", "run_id": self.run_id,
                                  "digest": plan["candidates"][0]["digest"]})["output"]

    def test_authorization_rejects_before_retrieval_or_protected_reads(self):
        for changes in ({"version": 2}, {"version": True}, {"mode": "holdout"}, {"holdout": True},
                        {"run_id": []}, {"backend": []}, {"case": None}, {"output": {}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.graph.invoke({**self.request, **changes})
        self.retrieve.assert_not_called()
        self.assertEqual(self.journal.events(self.run_id), [])
        plan = self.plan()
        with patch.object(self.service, "_sample", side_effect=AssertionError("protected read")):
            for run, token in ((str(uuid4()), plan["candidates"][0]["digest"]), (self.run_id, "0" * 64)):
                with self.assertRaises(JournalConflict):
                    self.graph.invoke({"version": 1, "mode": "execute", "run_id": run, "digest": token})
            with self.assertRaises(JournalConflict):
                self.service.action(self.run_id, "holdout")
        self.numerical.assert_not_called()

    def test_mcp_denials_do_not_read_journal(self):
        journal = Mock()
        for name, args in (("branch_records", {}), ("branch_records", {"version": True}),
                           ("branch_records", {"version": 1, "run_id": str(uuid4())}),
                           ("branch_records", {"version": 2}), ("approve", {"version": 1}),
                           ("diagnostics", {"version": 1}), ("holdout", {"version": 1})):
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                dispatch(name, args, journal, self.run_id)
        journal.get.assert_not_called()
        with self.assertRaises(ValueError):
            dispatch("branch_records", {"version": 1})
        self.plan()
        response = dispatch("branch_records", {"version": 1}, self.journal, self.run_id)
        self.assertEqual(set(response), {"version", "status", "branches"})
        self.assertEqual(response["status"], "found")

    def test_reference_protocols_agree(self):
        # Frozen expected allowlist, not computed by the production planner.
        expected = {("AIC", ("conditional_i1", "eg_aic", "cusum_ecm_ou", "chronological_locked")),
                    ("BIC", ("conditional_i1", "eg_bic", "cusum_ecm_ou", "chronological_locked"))}
        for case in ("independent", "shared-trend", "structural-break", "spread-level-shift"):
            with self.subTest(case=case):
                self.request.update(case=case, run_id=str(uuid4()))
                plans = self.plan()["candidates"]
                self.assertEqual({(p["protocol"]["lag_policy"], tuple(p["protocol"]["decisions"])) for p in plans},
                                 expected)
                for item in plans:
                    self.assertEqual(item["digest"], fingerprint(item["protocol"]))
                    self.assertEqual(item["protocol"]["evidence_ids"], ["adf", "eg", "kpss"])
        self.numerical.assert_not_called()

    def test_safe_stopping_fault_matrix(self):
        for fault in ("exception", "not_object", "wrong_hash", "conflicting_roots", "direction_disagreement",
                      "unstable"):
            with self.subTest(fault=fault):
                self.run_id = self.request["run_id"] = str(uuid4())
                self.numerical.reset_mock()
                def tool(step, rows, window, lag):
                    if step == "diagnostics":
                        if fault == "exception":
                            raise ValueError("fixture")
                        if fault == "not_object":
                            return []
                        data = bound(step, rows, window, lag, {"a": roots(),
                                     "b": roots("unresolved" if fault == "conflicting_roots" else "I(1)")})
                        if fault == "wrong_hash":
                            data["result_sha256"] = "0" * 64
                        return data
                    if step == "engle_granger":
                        return bound(step, rows, window, lag, {"a_on_b": {"p": .01},
                                     "b_on_a": {"p": .9 if fault == "direction_disagreement" else .01}})
                    return bound(step, rows, window, lag, {"cusum_p": .001})
                self.numerical.side_effect = tool
                verdict = self.execute(self.plan())["report"]
                count = {"direction_disagreement": 2, "unstable": 3}.get(fault, 1)
                self.assertEqual(self.numerical.call_count, count)
                self.assertEqual(verdict["verdict"], "stop" if fault == "unstable" else "unresolved")
                self.assertEqual(verdict["holdout"], "locked and untested")
                self.assertEqual(report_from_journal(self.journal, self.run_id)["report"]["verdict"],
                                 verdict["verdict"])

    def test_evidence_fidelity_rejects_rehashed_alterations(self):
        self.numerical.side_effect = lambda step, rows, window, lag: bound(
            step, rows, window, lag, {"a": roots(), "b": roots("unresolved")})
        self.execute(self.plan())
        events = self.journal.events(self.run_id)
        for mutation in ("cross_run", "observation", "finding", "citation", "verdict"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(events)
                by_key = {event["key"]: event["value"] for event in changed}
                if mutation == "cross_run":
                    by_key["result:diagnostics"]["run_id"] = str(uuid4())
                if mutation == "observation":
                    by_key["result:diagnostics"]["observation"]["result"]["a"]["level"]["adf_p"] = .12
                    by_key["review:diagnostics"]["evidence_sha256"] = fingerprint(by_key["result:diagnostics"])
                if mutation == "finding":
                    by_key["review:diagnostics"]["details"] = {}
                if mutation == "citation":
                    by_key["plan"]["method_evidence"][0]["url"] = "https://example.invalid/"
                    by_key["approval"]["plan_sha256"] = fingerprint(by_key["plan"])
                if mutation == "verdict":
                    by_key["verdict"]["verdict"] = "continue"
                with patch.object(self.journal, "events", return_value=changed), self.assertRaises(JournalConflict):
                    report_from_journal(self.journal, self.run_id)
