"""Regression checks for paired cases, blinding, and qualification mechanics."""

from collections import Counter
import unittest

from edgelab.providers import LMStudio, ProviderError, ROLES
from research.workflow_comparison import benchmark


class BenchmarkTests(unittest.TestCase):
    def test_cases_balance_family_and_flaw_and_smoke_is_a_subset(self):
        cases = benchmark.fixtures(False)
        smoke = benchmark.fixtures(True)
        self.assertEqual(len(cases), 24)
        self.assertEqual(len({case["id"] for case in cases}), 24)
        self.assertEqual(len({case["sample_seed"] for case in cases}), 24)
        self.assertEqual(set(Counter(case["family"] for case in cases).values()), {6})
        self.assertEqual(set(Counter(case["flaw"] for case in cases).values()), {4})
        self.assertEqual(len({case["family"] for case in smoke}), 4)
        self.assertTrue(all(case in cases for case in smoke))

    def test_model_prompt_omits_identity_and_gold_and_offer_is_balanced(self):
        case = next(case for case in benchmark.fixtures(False) if case["flaw"] == "no_flaw")
        evaluation = benchmark.evaluate_case(case)
        questions = list(benchmark.phase_questions(case, evaluation))
        self.assertEqual([q[0] for q in questions], list(ROLES))
        for _, prompt, choices, _ in questions:
            self.assertNotIn("gold", prompt)
            self.assertNotIn(case["id"], repr(prompt))
            self.assertNotIn(str(case["sample_seed"]), repr(prompt))
            self.assertNotIn(case["family"], repr(prompt))
            self.assertLessEqual(len(choices), 8)
        offers = questions[-1][1]["report_options"].values()
        self.assertIn((evaluation["gold"]["evidence"], evaluation["gold"]["reproducibility"]), offers)
        self.assertEqual(set(Counter(path for path, _ in offers).values()), {2})

    def test_transport_caps_completion_calls_and_options(self):
        transport = benchmark.MeteredTransport("stub")
        model = LMStudio("http://127.0.0.1:1234/v1", {r: "test" for r in ROLES}, transport=transport)
        for role in ROLES:
            self.assertEqual(model.choose(role, "test", ("b", "a"), benchmark.TIMEOUT), "b")
        with self.assertRaises(ProviderError):
            model.choose("critic", "repeat", ("b", "a"), benchmark.TIMEOUT)
        self.assertEqual(len(transport.calls), benchmark.MODEL_CALLS)
        self.assertTrue(all(call["request"]["max_tokens"] == 128 for call in transport.calls))

    def test_blind_scores_and_paired_sign_test(self):
        gold = {measure: measure for measure in benchmark.MEASURES}
        reports = {"opaque-1": {"case_id": "case-1", **gold},
                   "opaque-2": {"case_id": "case-1", **gold, "verdict": None}}
        scores = benchmark.blinded_scores(reports, {"case-1": gold})
        self.assertEqual(sum(scores["opaque-1"].values()), 4)
        self.assertEqual(sum(scores["opaque-2"].values()), 3)
        result = benchmark.compare({"case-1": scores["opaque-1"]}, {"case-1": scores["opaque-2"]})
        self.assertEqual(result, {"mean_improvement": 0.25, "wins": 1, "losses": 0,
                                  "ties": 0, "sign_p_one_sided": 0.5})


if __name__ == "__main__":
    unittest.main()
