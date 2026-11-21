from dataclasses import replace
import unittest
from unittest.mock import patch

from edgelab.contracts import ActionRequest, Finding, FixtureApproval, Signal, Status, Step, Verdict, fingerprint
from edgelab.controller import RejectedTransition, ResearchRun
from edgelab.fixtures import Fixture, METHOD, QUESTION
from edgelab.roles import plan_protocol


def start(name="conflict", run_id="run-1"):
    return ResearchRun(run_id, Fixture(name), METHOD)


def approve(run):
    run.approve(FixtureApproval(run.binding("approval")))


def finish(run):
    while run.status == Status.READY:
        run.execute(run.propose_next(f"action-{run.version}"))
        run.review_milestone()
    run.finalize()
    return run.packet()


class WorkflowTests(unittest.TestCase):
    def test_planning_feedback_changes_the_candidate_without_reading_observations(self):
        with patch.object(Fixture, "observe", side_effect=AssertionError("unexpected tool call")):
            start("conflict")
            start("support")
            plan = plan_protocol(QUESTION, METHOD)
        first, revised = plan.branches
        self.assertFalse(first.accepted)
        self.assertEqual(revised.parent_id, first.branch_id)
        self.assertTrue(revised.accepted)
        self.assertEqual(plan.protocol.steps, (Step.DIAGNOSTICS, Step.RELATIONSHIP))

    def test_missing_method_evidence_abstains_before_approval(self):
        run = ResearchRun("run-1", Fixture("support"), None)
        with self.assertRaises(RejectedTransition):
            run.binding("approval")
        packet = finish(run)
        self.assertEqual(packet["report"]["verdict"], Verdict.UNRESOLVED)
        self.assertEqual(packet["bundle"]["manifest"]["protocol"]["status"], "not_produced")
        self.assertEqual(packet["bundle"]["entries"]["observations"], [])

    def test_tool_cannot_run_before_approval(self):
        run = start()
        packet = run.packet()
        self.assertEqual(packet["status"], Status.AWAITING_APPROVAL)
        self.assertEqual(packet["protocol_digest"], fingerprint(packet["proposed_protocol"]))
        self.assertEqual(len(packet["reviewed_branches"]), 2)
        with patch.object(Fixture, "observe") as observe:
            with self.assertRaises(RejectedTransition):
                run.execute(ActionRequest(run.binding("action"), Step.DIAGNOSTICS))
            observe.assert_not_called()

    def test_stale_or_mismatched_approval_is_rejected_without_mutation(self):
        run = start()
        valid = run.binding("approval")
        before = run.ledger
        for binding in (
            replace(valid, expected_version=0),
            replace(valid, protocol_digest="wrong-protocol"),
            replace(valid, fixture_digest="wrong-fixture"),
            replace(valid, run_id="other-run"),
        ):
            with self.subTest(binding=binding), self.assertRaises(RejectedTransition):
                run.approve(FixtureApproval(binding))
        self.assertEqual(run.ledger, before)
        self.assertEqual(run.status, Status.AWAITING_APPROVAL)

    def test_out_of_order_and_unreviewed_actions_do_not_reach_tool(self):
        run = start("support")
        approve(run)
        with patch.object(Fixture, "observe", return_value=Signal.ASSUMPTIONS_MET) as observe:
            with self.assertRaises(RejectedTransition):
                run.execute(ActionRequest(run.binding("early"), Step.RELATIONSHIP))
            run.execute(run.propose_next("diagnostics"))
            with self.assertRaises(RejectedTransition):
                run.execute(ActionRequest(run.binding("too-soon"), Step.RELATIONSHIP))
            observe.assert_called_once_with(Step.DIAGNOSTICS)
        run.review_milestone()
        self.assertEqual(run.propose_next("next").step, Step.RELATIONSHIP)

    def test_replayed_and_stale_actions_do_not_duplicate_observations(self):
        run = start("support")
        approve(run)
        run.execute(run.propose_next("first"))
        run.review_milestone()
        second = run.propose_next("second")
        before = run.ledger
        with patch.object(Fixture, "observe") as observe:
            for request in (
                replace(second, binding=replace(second.binding, message_id="first")),
                replace(second, binding=replace(second.binding, expected_version=0)),
            ):
                with self.assertRaises(RejectedTransition):
                    run.execute(request)
            observe.assert_not_called()
        self.assertEqual(run.ledger, before)

    def test_conflict_and_tool_error_block_the_relationship_action(self):
        for scenario, signal in (("conflict", Signal.CONFLICT), ("tool-error", Signal.ERROR)):
            with self.subTest(scenario=scenario):
                run = start(scenario)
                approve(run)
                packet = finish(run)
                self.assertEqual(packet["report"]["verdict"], Verdict.UNRESOLVED)
                observations = packet["bundle"]["entries"]["observations"]
                self.assertEqual([(row["step"], row["signal"]) for row in observations],
                                 [(Step.DIAGNOSTICS, signal)])

    def test_controller_does_not_adopt_an_unsupported_critic_recommendation(self):
        run = start("conflict")
        approve(run)
        observed = run.execute(run.propose_next("diagnostics"))
        with patch("edgelab.roles.critique", return_value=Finding(observed.result_id, "incorrect", Verdict.CONTINUE)):
            run.review_milestone()
        packet = finish(run)
        self.assertEqual(packet["bundle"]["entries"]["verdict"]["recommendation"], Verdict.CONTINUE)
        self.assertEqual(packet["report"]["verdict"], Verdict.UNRESOLVED)

    def test_verdict_report_and_manifest_match_the_record(self):
        for scenario, verdict in (("support", Verdict.CONTINUE), ("no-support", Verdict.STOP)):
            with self.subTest(scenario=scenario):
                run = start(scenario)
                approve(run)
                packet = finish(run)
                entries = packet["bundle"]["entries"]
                self.assertEqual(packet["report"]["verdict"], verdict)
                self.assertEqual(entries["verdict"]["recorded"], verdict)
                self.assertEqual(len(entries["observations"]), 2)
                self.assertEqual(set(packet["report"]["evidence_refs"]),
                                 {item["result_id"] for item in entries["observations"]})
                for key, manifest_entry in packet["bundle"]["manifest"].items():
                    self.assertEqual(manifest_entry, {"status": "present", "sha256": fingerprint(entries[key])})


if __name__ == "__main__":
    unittest.main()
