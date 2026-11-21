from http.client import IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

from edgelab.journal import Journal, JournalConflict
from edgelab.methods import NAMESPACE, PineconeMethods, local_notes
from edgelab.planning import search
from edgelab.providers import LMStudio, ProviderError, post_json
from edgelab.research import ResearchService
from edgelab.stat_tools import review
from observation_fixtures import bound, roots


RUN = "36306295-bd95-4bb6-8a95-ab7dd45cc7dc"
HOST = "methods-000.svc.us-east1.pinecone.io"


class ResearchRegressions(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(TemporaryDirectory())) / "test.sqlite"
        self.journal = Journal(self.path)
        self.values = {"a": roots(), "b": roots()}
        self.tool = Mock(side_effect=lambda *args: bound(*args, self.values))
        self.service = ResearchService(self.journal, numerical_tool=self.tool, runtime_identity=lambda: {"test": "fixture"})
        plan = self.service.record_plan(RUN, "shared-trend", search(local_notes("cointegration")), "local")
        self.approval = self.service.approve(RUN, plan["candidates"][0]["digest"])

    def test_adapter_only_receives_the_approved_sample(self):
        self.service.action(RUN, "diagnostics")
        _, rows, window, _ = self.tool.call_args.args
        self.assertEqual(len(rows), 180)
        self.assertEqual(rows[-1].day, window.end)
        self.assertTrue(all(row.day < window.holdout_start for row in rows))

    def test_stored_review_cannot_unlock_a_tool_without_evidence(self):
        self.journal.append(RUN, "review:diagnostics", {
            "result_id": "result:diagnostics", "decision": "proceed", "reason": "not verified",
        })
        with self.assertRaises(JournalConflict):
            self.service.action(RUN, "engle_granger")
        self.tool.assert_not_called()

    def test_wrong_approval_result_cannot_be_replayed(self):
        self.journal.append(RUN, "reserved:diagnostics", {
            "approval": self.approval["digest"], "tool": "diagnostics",
            "window": self.approval["protocol"]["window"], "sample_sha256": self.approval["sample_sha256"],
        })
        self.journal.append(RUN, "result:diagnostics", {
            "id": "result:diagnostics", "approval": "different", "tool": "diagnostics",
            "window": self.approval["protocol"]["window"], "sample_sha256": self.approval["sample_sha256"],
            "observation": {"result": self.values},
        })
        with self.assertRaises(JournalConflict):
            self.service.action(RUN, "diagnostics")
        self.tool.assert_not_called()

    def test_conflicting_review_cannot_override_a_bound_result(self):
        self.values["a"]["order"] = "unresolved"
        self.service.action(RUN, "diagnostics")
        self.journal.append(RUN, "review:diagnostics", {
            "result_id": "result:diagnostics", "decision": "proceed", "reason": "not verified",
        })
        with self.assertRaisesRegex(JournalConflict, "Review conflicts"):
            self.service.action(RUN, "engle_granger")
        self.tool.assert_called_once()

    def test_nonfinite_tool_result_is_audited_and_does_not_strand_reservation(self):
        self.tool.side_effect = None
        self.tool.return_value = {"result": {"score": float("nan")}}
        self.service.action(RUN, "diagnostics")
        self.assertEqual(self.service.critique(RUN, "diagnostics")["decision"], "unresolved")
        self.assertEqual(self.service.finalize(RUN)["report"]["verdict"], "unresolved")

    def test_read_only_view_cannot_modify_the_journal(self):
        before = self.path.read_bytes()
        reader = Journal(self.path, read_only=True)
        self.assertEqual(reader.get(RUN, "approval"), self.approval)
        with self.assertRaises(JournalConflict):
            reader.append(RUN, "other", {})
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_statistics_cannot_be_reported_as_continue_or_stop(self):
        for p in (float("nan"), True, -0.1, 1.1, None):
            with self.subTest(p=p):
                self.assertEqual(review("stability", {"result": {"cusum_p": p}})[0], "unresolved")
                self.assertEqual(review("engle_granger", {"result": {
                    "a_on_b": {"p": p}, "b_on_a": {"p": p},
                }})[0], "unresolved")


class PlanningRegressions(unittest.TestCase):
    def test_late_model_response_cannot_yield_approvable_candidates(self):
        elapsed = 0
        votes = 0

        def respond(role, prompt, options, timeout):
            nonlocal elapsed, votes
            # Expire during the last response so no later request can catch it.
            if role == "critic" and prompt.startswith("Advisory tie vote"):
                votes += 1
                if votes == 2:
                    elapsed = 226
            return options[0]

        model = Mock()
        model.choose.side_effect = respond
        with patch("edgelab.planning.monotonic", side_effect=lambda: elapsed):
            packet = search(local_notes("cointegration"), model)
        self.assertEqual(packet["status"], "unresolved")
        self.assertEqual(packet["candidates"], [])

    def test_budget_exhaustion_keeps_partial_branch_trace(self):
        with patch("edgelab.planning.THOUGHT_LIMIT", 4):
            packet = search(local_notes("cointegration"))
        self.assertEqual(packet["thoughts"], 4)
        self.assertFalse(packet["branches"][-1]["retained"])


class ProviderRegressions(unittest.TestCase):
    def test_incomplete_http_response_is_a_provider_error(self):
        response = MagicMock()
        response.__enter__.return_value.read.side_effect = IncompleteRead(b"partial")
        with patch("edgelab.providers.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(ProviderError):
                post_json("http://127.0.0.1:1234/v1/chat/completions", {}, {}, 1)

    def test_truncated_or_ambiguous_model_choice_is_rejected(self):
        for finish, content in (
            ("length", '{"choice":"a"}'), ("stop", '{"choice":"b","choice":"a"}'),
        ):
            with self.subTest(finish=finish, content=content):
                transport = Mock(return_value={"choices": [
                    {"finish_reason": finish, "message": {"content": content}},
                ]})
                client = LMStudio("http://127.0.0.1:1234/v1", {"planner": "fake"}, transport=transport)
                with self.assertRaises(ProviderError):
                    client.choose("planner", "Choose", ("a", "b"))

    def test_pinecone_rejects_wrong_namespace_and_non_boolean_review_metadata(self):
        note = local_notes("cointegration")[0]
        vector = {"data": [{"values": [0.1] * 1024}]}
        hit = {"id": note.id, "score": 0.9, "metadata": note.metadata()}
        wrong_namespace = Mock(side_effect=[vector, {"namespace": "wrong", "matches": [hit]}])
        with self.assertRaises(ProviderError):
            PineconeMethods(HOST, "fake", wrong_namespace).search("cointegration")
        self.assertEqual(wrong_namespace.call_count, 2)
        forged = {**hit, "metadata": {**note.metadata(), "reviewed": 1}}
        wrong_type = Mock(side_effect=[vector, {"namespace": NAMESPACE, "matches": [forged]}])
        self.assertEqual(PineconeMethods(HOST, "fake", wrong_type).search("cointegration"), ())
        self.assertEqual(wrong_type.call_count, 2)


if __name__ == "__main__":
    unittest.main()
