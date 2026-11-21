import json
import unittest
from unittest.mock import Mock

from edgelab.methods import BY_ID, local_notes
from edgelab.planning import CRITIC_LABELS, critic_prompt, protocol_issues, search


class ModelRoleTests(unittest.TestCase):
    def test_planner_only_ranks_eligible_alternatives_without_hiding_pruned_branches(self):
        model = Mock()
        model.choose.side_effect = lambda role, prompt, options, timeout: (
            "no_preference" if "no_preference" in options else options[0]
        )
        plan = search(local_notes("cointegration"), model)
        planner = [call.args for call in model.choose.call_args_list if call.args[0] == "planner"]
        self.assertEqual(len(planner), 1)
        self.assertEqual(planner[0][2], ("eg_aic", "eg_bic"))
        self.assertIn("both directions", planner[0][1])
        self.assertEqual(plan["thoughts"], 18)
        self.assertTrue(any(row["id"] == "correlation_only" and not row["retained"] for row in plan["branches"]))
        self.assertEqual(plan["model_attempts"], 4)
        self.assertEqual(plan["status"], "awaiting_approval")
        self.assertIsNone(plan["model_preference"])

    def test_critic_rubric_describes_the_decisions_and_never_approves_execution(self):
        notes = local_notes("cointegration")
        doc = json.loads(critic_prompt((("conditional_i1", "eg_aic", "cusum_ecm_ou", "reuse_holdout"),), notes))
        self.assertEqual(set(doc["method_evidence"]), {"adf", "eg", "kpss"})
        self.assertIn("Reuse holdout", doc["plans"][0][-1])
        self.assertIn("never execute", doc["decision_rule"]["accept"])

    def test_critic_escalation_remains_terminal(self):
        model = Mock()
        model.choose.side_effect = lambda role, prompt, options, timeout: (
            "escalate" if options == CRITIC_LABELS else options[0]
        )
        plan = search(local_notes("cointegration"), model)
        self.assertEqual(plan["status"], "unresolved")
        self.assertFalse(plan["candidates"])
        self.assertEqual(plan["model_decisions"][-1]["choice"], "escalate")

    def test_invalid_or_ungrounded_model_calls_fail_closed(self):
        model = Mock()
        model.choose.return_value = "unrestricted_lags"
        self.assertEqual(search(local_notes("cointegration"), model)["status"], "unresolved")
        model.reset_mock()
        plan = search((BY_ID["adf"], BY_ID["kpss"]), model)
        self.assertEqual(plan["status"], "unresolved")
        model.choose.assert_not_called()

    def test_planner_only_uses_a_deterministic_critic_without_model_votes(self):
        model = Mock()
        model.choose.return_value = "eg_bic"
        plan = search(local_notes("cointegration"), model, use_model_critic=False)
        self.assertEqual(plan["status"], "awaiting_approval")
        self.assertEqual(plan["protocol_review"], {"mode": "deterministic", "outcome": "passed", "issues": []})
        self.assertEqual(plan["candidates"][0]["decisions"][1], "eg_bic")
        model.choose.assert_called_once()
        self.assertEqual(model.choose.call_args.args[0], "planner")

    def test_deterministic_critic_detects_the_four_model_false_accepts(self):
        notes = local_notes("cointegration")
        base = ("conditional_i1", "eg_aic", "cusum_ecm_ou", "chronological_locked")
        self.assertEqual(protocol_issues((base,), notes), ())
        cases = (
            (("correlation_only", *base[1:]), notes, "candidate_1:unit_root_prerequisite"),
            ((*base[:-1], "reuse_holdout"), notes, "candidate_1:locked_holdout"),
            (base, tuple(note for note in notes if note.id != "eg"), "missing_method_evidence"),
            ((base[0], "unrestricted_lags", *base[2:]), notes, "candidate_1:fixed_bidirectional_method"),
        )
        for decisions, evidence, issue in cases:
            with self.subTest(issue=issue):
                self.assertEqual(protocol_issues((decisions,), evidence), (issue,))


if __name__ == "__main__":
    unittest.main()
