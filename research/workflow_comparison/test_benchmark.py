"""Regression checks for paired cases, blinding, and qualification mechanics."""

from collections import Counter
from contextlib import redirect_stderr
from io import StringIO
import json
import sys
import unittest
from unittest.mock import patch

from edgelab.providers import LMStudio, ProviderError, ROLES
from research.workflow_comparison import benchmark


class BenchmarkTests(unittest.TestCase):
    BASE_URL = "http://127.0.0.1:1234/v1"
    MODEL_ID = "ornith-test-model"

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

    def test_reasoning_off_is_explicit_and_preserves_the_caller_request(self):
        payload = {
            "model": self.MODEL_ID, "temperature": 0, "max_tokens": 128,
            "messages": [{"role": "user", "content": "prompt"}],
            "response_format": {"type": "json_schema"},
        }
        reply = {
            "model": self.MODEL_ID,
            "choices": [{"finish_reason": "stop", "message": {"content": '{"choice":"a"}'}}],
            "usage": {"prompt_tokens": 18, "completion_tokens": 5,
                      "completion_tokens_details": {"reasoning_tokens": 0}},
        }
        url = self.BASE_URL + "/chat/completions"
        for reasoning_off in (False, True):
            with self.subTest(reasoning_off=reasoning_off):
                transport = benchmark.MeteredTransport("lm-studio", reasoning_off=reasoning_off)
                with patch.object(benchmark, "post_json", return_value=reply) as post:
                    self.assertIs(transport(url, {}, payload, benchmark.TIMEOUT), reply)
                expected = {**payload, "reasoning_effort": "none"} if reasoning_off else payload
                post.assert_called_once_with(url, {}, expected, benchmark.TIMEOUT)
                self.assertNotIn("reasoning_effort", payload)
                call, = transport.calls
                self.assertEqual(call["request"], expected)
                self.assertEqual(call["usage"], {"prompt_tokens": 18, "completion_tokens": 5})
                if reasoning_off:
                    self.assertEqual(call["response_receipt"], {"finish_reason": "stop", "reasoning_tokens": 0})

    def test_reasoning_off_meters_invalid_provider_replies_as_failures(self):
        usage = {"prompt_tokens": 18, "completion_tokens": 5,
                 "completion_tokens_details": {"reasoning_tokens": 0}}
        choice = {"message": {"content": '{"choice":"a"}'}}
        reply = {"model": self.MODEL_ID, "choices": [{**choice, "finish_reason": "stop"}], "usage": usage}
        invalid_replies = {
            "null choices": {**reply, "choices": None},
            "invalid choice": {**reply, "choices": [None]},
            "missing finish reason": {**reply, "choices": [choice]},
            "truncation": {**reply, "choices": [{**choice, "finish_reason": "length"}]},
            "wrong model": {**reply, "model": "different-model"},
            "null usage": {**reply, "usage": None},
            "missing reasoning": {**reply, "usage": {**usage, "completion_tokens_details": {}}},
            "boolean reasoning": {**reply, "usage": {**usage, "completion_tokens_details": {"reasoning_tokens": False}}},
            "nonzero reasoning": {**reply, "usage": {**usage, "completion_tokens_details": {"reasoning_tokens": 1}}},
        }
        for label, invalid_reply in invalid_replies.items():
            with self.subTest(label=label):
                transport = benchmark.MeteredTransport("lm-studio", reasoning_off=True)
                model = LMStudio(self.BASE_URL, {"planner": self.MODEL_ID}, transport=transport)
                with patch.object(benchmark, "post_json", return_value=invalid_reply):
                    with self.assertRaises(ProviderError):
                        model.choose("planner", "prompt", ("a", "b"), benchmark.TIMEOUT)
                call, = transport.calls
                self.assertTrue(call["transport_failed"])
                expected_usage = {} if invalid_reply["usage"] is None else {"prompt_tokens": 18, "completion_tokens": 5}
                self.assertEqual(call["usage"], expected_usage)

    def test_reasoning_off_reaches_all_phases_of_a_model_arm(self):
        case = benchmark.fixtures(True)[0]
        evaluation = benchmark.evaluate_case(case)

        def respond(url, headers, payload, timeout):
            choices = payload["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"]
            return {
                "model": payload["model"],
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"choice": choices[0]})}}],
                "usage": {"prompt_tokens": 18, "completion_tokens": 5,
                          "completion_tokens_details": {"reasoning_tokens": 0}},
            }

        with patch.object(benchmark, "post_json", side_effect=respond):
            _, execution = benchmark.arm_result(
                "single_agent", case, evaluation, "lm-studio", self.BASE_URL, self.MODEL_ID, reasoning_off=True,
            )
        self.assertEqual(execution["model_calls"], benchmark.MODEL_CALLS)
        self.assertEqual(execution["missing_usage"], 0)
        self.assertTrue(all("choice" in phase for phase in execution["phase_trace"]))
        for call in execution["transport"]:
            self.assertEqual(call["request"]["reasoning_effort"], "none")
            self.assertEqual(call["response_receipt"]["reasoning_tokens"], 0)

    def test_stub_cli_rejects_reasoning_off(self):
        argv = ["benchmark", "run", "--mode", "stub", "--output", "unused", "--reasoning-off"]
        with patch.object(sys, "argv", argv), patch.object(benchmark, "run") as run, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as failure:
                benchmark.main()
        self.assertEqual(failure.exception.code, 2)
        run.assert_not_called()

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
