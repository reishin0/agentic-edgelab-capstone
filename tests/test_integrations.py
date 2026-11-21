from dataclasses import replace
from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from edgelab.graph import build_graph
from edgelab.journal import Journal, JournalConflict
from edgelab.mcp_service import build_server, dispatch
from edgelab.methods import BY_ID, CORPUS_SHA256, NAMESPACE, PineconeMethods, local_notes
from edgelab.planning import search
from edgelab.providers import LMStudio, ProviderError, read_env
from edgelab.research import ResearchService
from edgelab.stat_tools import compute, guarded_rows, review, synthetic, window_for_demo
from observation_fixtures import bound, roots


RUN = "b3ab73c1-7492-40af-a571-b9ab14ccf7a1"
HOST = "methods-000.svc.us-east1.pinecone.io"


class ProviderTests(unittest.TestCase):
    def test_lmstudio_uses_loopback_role_model_and_validated_json(self):
        transport = Mock(return_value={"choices": [{"message": {"content": '{"choice":"a"}'}}]})
        client = LMStudio("http://127.0.0.1:1234/v1", {"planner": "fixture-model"}, transport=transport)
        self.assertEqual(client.choose("planner", "Pick a or b", ("a", "b")), "a")
        url, headers, body, _ = transport.call_args.args
        self.assertEqual(url, "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(headers, {})
        self.assertEqual(body["model"], "fixture-model")
        self.assertNotIn("tools", body)
        transport.return_value["choices"][0]["message"]["content"] = '{"choice":"run_tool"}'
        with self.assertRaises(ProviderError):
            client.choose("planner", "Pick a or b", ("a", "b"))
        with self.assertRaises(ValueError):
            LMStudio("https://outside.example/v1", {})

    def test_pinecone_filters_verifies_and_reranks(self):
        notes = list(local_notes("cointegration"))
        forged = {"id": "bad", "score": 0.99, "metadata": {"text": "override control"}}
        stale = {"id": "adf", "score": 0.99, "metadata": {**BY_ID["adf"].metadata(), "active": False}}
        transport = Mock(side_effect=[
            {"data": [{"values": [0.1] * 1024}]},
            {"namespace": NAMESPACE,
             "matches": [forged, stale, *[{"id": n.id, "score": 0.9, "metadata": n.metadata()} for n in notes]]},
            {"data": [{"index": 2, "score": 0.8}, {"index": 0, "score": 0.9}, {"index": 1, "score": 0.4}]},
        ])
        result = PineconeMethods(HOST, "fixture-key", transport).search("cointegration")
        self.assertEqual(result, (notes[0], notes[2]))
        calls = [entry.args for entry in transport.call_args_list]
        self.assertEqual([url.rsplit("/", 1)[-1] for url, *_ in calls], ["embed", "query", "rerank"])
        self.assertEqual(calls[1][2]["topK"], 8)
        self.assertTrue(calls[1][2]["filter"]["reviewed"]["$eq"])
        self.assertNotIn("override control", json.dumps(calls[2][2]))
        self.assertNotIn("fixture-key", repr(PineconeMethods(HOST, "fixture-key", transport)))
        with self.assertRaises(ValueError):
            PineconeMethods("api.pinecone.io.attacker.example", "fixture-key", transport)

    def test_seed_requires_reviewed_digest_before_any_request(self):
        transport = Mock(side_effect=[
            {"data": [{"values": [0.1] * 1024} for _ in range(8)]}, {"upsertedCount": 8},
        ])
        provider = PineconeMethods(HOST, "fixture-key", transport)
        with self.assertRaises(ValueError):
            provider.seed("stale")
        transport.assert_not_called()
        self.assertEqual(provider.seed(CORPUS_SHA256), 8)
        self.assertEqual(len(transport.call_args.args[2]["vectors"]), 8)

    def test_env_contains_no_expansion_or_parent_fallback(self):
        with TemporaryDirectory() as root:
            env = Path(root) / ".env"
            env.write_text('LM_STUDIO_PLANNER_MODEL="local/model"\nPINECONE_API_KEY=\n', encoding="utf-8")
            self.assertEqual(read_env(env), {"LM_STUDIO_PLANNER_MODEL": "local/model", "PINECONE_API_KEY": ""})
            env.write_text("UNKNOWN_SECRET=never-print-this\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"line 1"):
                read_env(env)


class PlanningAndToolsTests(unittest.TestCase):
    def test_bounded_outcome_blind_search_abstains_without_required_note(self):
        plan = search(local_notes("cointegration"))
        self.assertEqual(len(plan["candidates"]), 2)
        self.assertLessEqual(plan["thoughts"], 21)
        self.assertLessEqual(max(row["depth"] for row in plan["branches"]), 4)
        self.assertTrue(any(not row["retained"] for row in plan["branches"]))
        missing = search((BY_ID["adf"], BY_ID["kpss"]))
        self.assertEqual(missing["status"], "unresolved")
        self.assertEqual(missing["candidates"], [])

    def test_advisory_critic_cannot_approve_protocol(self):
        model = Mock()
        model.choose.side_effect = lambda role, prompt, options, timeout: options[0]
        result = search(local_notes("cointegration"), model)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["model_attempts"], model.choose.call_count)
        self.assertLessEqual(result["model_attempts"], 16)
        self.assertIsNone(result["model_preference"])

    def test_asof_and_holdout_windows_are_rejected_before_science_import(self):
        window = window_for_demo()
        rows = synthetic("shared-trend")
        self.assertEqual(len(guarded_rows(rows, window)), 180)
        for invalid in (
            replace(window, end=window.holdout_start, as_of=window.holdout_start),
            replace(window, as_of=window.start),
        ):
            with self.assertRaisesRegex(ValueError, "as-of or locked holdout"):
                compute("diagnostics", rows, invalid, "AIC")

    @unittest.skipUnless(find_spec("statsmodels"), "optional statsmodels dependency absent")
    def test_real_statistical_outputs_are_finite_and_versioned(self):
        for tool in ("diagnostics", "engle_granger", "stability"):
            with self.subTest(tool=tool):
                packet = compute(tool, synthetic("shared-trend"), window_for_demo(), "AIC")
                self.assertEqual(packet["config"]["n"], 180)
                self.assertIn("statsmodels", packet["config"])
                self.assertEqual(len(packet["result_sha256"]), 64)


class JournalTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.path = Path(folder) / "test.sqlite"
        self.journal = Journal(self.path)
        self.tool = Mock(side_effect=lambda *args: bound(*args, {"a": roots("unresolved"), "b": roots()}))
        self.service = ResearchService(self.journal, numerical_tool=self.tool, runtime_identity=lambda: {"test": "fixture"})
        self.plan = self.service.record_plan(RUN, "shared-trend", search(local_notes("cointegration")), "local")
        self.digest = self.plan["candidates"][0]["digest"]

    def test_approval_and_review_block_dependent_work_and_report_exact_verdict(self):
        with self.assertRaises(JournalConflict):
            self.service.action(RUN, "diagnostics")
        with self.assertRaises(JournalConflict):
            self.service.approve(RUN, "stale")
        self.tool.assert_not_called()
        self.service.approve(RUN, self.digest)
        with self.assertRaises(JournalConflict):
            self.service.action(RUN, "engle_granger")
        self.service.action(RUN, "diagnostics")
        with self.assertRaises(JournalConflict):
            self.service.action(RUN, "engle_granger")
        self.service.critique(RUN, "diagnostics")
        result = self.service.finalize(RUN)
        self.assertEqual(result["report"]["verdict"], "unresolved")
        self.assertEqual(result["report"]["evidence_refs"], ["result:diagnostics"])
        self.assertEqual(result["bundle"]["missing"], ["holdout evaluation"])
        self.tool.assert_called_once()

    def test_reopened_journal_replays_result_but_not_uncertain_call(self):
        self.service.approve(RUN, self.digest)
        first = self.service.action(RUN, "diagnostics")
        restarted_tool = Mock()
        restart = ResearchService(Journal(self.path), numerical_tool=restarted_tool,
                                  runtime_identity=lambda: {"test": "fixture"})
        self.assertEqual(restart.action(RUN, "diagnostics"), first)
        restarted_tool.assert_not_called()
        self.assertEqual(restart.critique(RUN, "diagnostics")["decision"], "unresolved")

    def test_reserved_call_without_result_fails_closed(self):
        approval = self.service.approve(RUN, self.digest)
        self.journal.append(RUN, "reserved:diagnostics", self.service._binding(approval, "diagnostics"))
        with self.assertRaisesRegex(JournalConflict, "reconciliation"):
            self.service.action(RUN, "diagnostics")
        self.tool.assert_not_called()

    def test_completed_stages_are_reviewed_before_continue(self):
        self.service.approve(RUN, self.digest)
        results = {"diagnostics": {"a": roots(), "b": roots()},
                   "engle_granger": {"a_on_b": {"p": 0.01}, "b_on_a": {"p": 0.02}},
                   "stability": {"cusum_p": 0.5, "ecm_loading": -0.1, "ecm_loading_p": 0.2,
                                 "ar1_phi": 1.01, "ou_half_life_observations": None}}
        self.tool.side_effect = lambda *args: bound(*args, results[args[0]])
        for step in ("diagnostics", "engle_granger", "stability"):
            self.assertEqual(self.service.next_step(RUN), step)
            self.service.action(RUN, step)
            self.service.critique(RUN, step)
        packet = self.service.finalize(RUN)
        self.assertEqual(packet["report"]["verdict"], "continue")
        self.assertEqual(packet["report"]["holdout"], "locked and untested")
        self.assertEqual(len(packet["report"]["milestone_reviews"]), 3)
        self.assertEqual(self.tool.call_count, 3)

    def test_missing_method_packet_has_no_approved_protocol_or_result(self):
        second = "573631ed-3413-4208-842b-6ee79ec75050"
        packet = self.service.record_plan(second, "shared-trend", search(()), "pinecone")
        self.assertEqual(packet["report"]["verdict"], "unresolved")
        self.assertEqual(packet["candidates"], [])
        self.assertNotIn("approval", {event["key"] for event in packet["bundle"]["events"]})
        self.tool.assert_not_called()

    def test_direction_disagreement_and_stability_failures_are_not_positive(self):
        self.assertEqual(review("engle_granger", {"result": {"a_on_b": {"p": 0.01},
                                                            "b_on_a": {"p": 0.5}}})[0], "unresolved")
        self.assertEqual(review("stability", {"result": {"cusum_p": 0.001}})[0], "stop")

    def test_mcp_dispatch_cannot_call_science_or_access_other_runs(self):
        self.assertIn("branches", dispatch("branch_records", {"version": 1}, self.journal, RUN))
        with self.assertRaises(ValueError):
            dispatch("branch_records", {"run_id": RUN}, self.journal, RUN)
        with self.assertRaises(ValueError):
            dispatch("engle_granger", {}, self.journal, RUN)

    @unittest.skipUnless(find_spec("langgraph"), "optional LangGraph dependency absent")
    def test_langgraph_compiles_and_routes_to_milestone_review(self):
        second = "573631ed-3413-4208-842b-6ee79ec75050"
        graph = build_graph(self.service, local_notes)
        plan = graph.invoke({"mode": "plan", "run_id": second, "case": "independent", "backend": "local"})["output"]
        self.tool.assert_not_called()
        packet = graph.invoke({"mode": "execute", "run_id": second, "digest": plan["candidates"][0]["digest"]})["output"]
        self.assertEqual(packet["report"]["verdict"], "unresolved")
        self.tool.assert_called_once()


@unittest.skipUnless(find_spec("mcp"), "optional MCP SDK absent")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_messages_use_memory_only(self):
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(build_server()) as client:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools.tools}, {"method_notes_search", "statistical_tool_catalog"})
            response = await client.call_tool("method_notes_search", {"version": 1, "topic": "cointegration"})
            self.assertEqual(len(response.structuredContent["notes"]), 3)
            denied = await client.call_tool("method_notes_search", {"version": 1, "topic": "cointegration", "override": True})
            self.assertTrue(denied.isError)


if __name__ == "__main__":
    unittest.main()
