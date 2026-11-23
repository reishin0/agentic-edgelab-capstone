from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from edgelab.providers import (
    LM_STUDIO_PLANNER_PROFILE_KEY, ORNITH_MAX_OUTPUT_TOKENS, ORNITH_PLANNER_MODEL, ORNITH_PLANNER_PROFILE,
    LMStudio, ProviderError, read_env,
)


SETTINGS = {
    "LM_STUDIO_BASE_URL": "http://127.0.0.1:8001/v1",
    "LM_STUDIO_API_KEY": "fixture-token",
    "LM_STUDIO_PLANNER_MODEL": ORNITH_PLANNER_MODEL,
    LM_STUDIO_PLANNER_PROFILE_KEY: ORNITH_PLANNER_PROFILE,
    "LM_STUDIO_CRITIC_MODEL": "critic-fixture",
}
LABELS = ("AIC", "BIC")
MESSAGE = {"type": "message", "content": '{"choice":"BIC"}'}
NATIVE_REPLY = {
    "model_instance_id": ORNITH_PLANNER_MODEL,
    "output": [MESSAGE],
    "stats": {"reasoning_output_tokens": 0, "total_output_tokens": 8},
}
COMPAT_REPLY = {"choices": [{"finish_reason": "stop", "message": {"content": '{"choice":"BIC"}'}}]}


class OrnithProviderTests(unittest.TestCase):
    def test_ornith_request_is_native_explicit_and_bounded(self):
        transport = Mock(return_value=NATIVE_REPLY)
        adviser = LMStudio.from_settings(SETTINGS, planner_model=ORNITH_PLANNER_MODEL, transport=transport)
        self.assertEqual(adviser.choose("planner", "Compare the protocols.", LABELS, timeout=7), "BIC")
        url, headers, payload, timeout = transport.call_args.args
        self.assertEqual(url, "http://127.0.0.1:8001/api/v1/chat")
        self.assertEqual(headers, {"Authorization": "Bearer fixture-token"})
        self.assertEqual(timeout, 7)
        instructions = payload.pop("system_prompt")
        self.assertIn('"choice"', instructions)
        self.assertIn('["AIC", "BIC"]', instructions)
        self.assertEqual(payload, {
            "model": ORNITH_PLANNER_MODEL, "input": "Compare the protocols.",
            "reasoning": "off", "store": False, "stream": False,
            "temperature": 0.6, "top_p": 0.95, "top_k": 20, "max_output_tokens": 128,
        })

    def test_profile_is_opt_in_and_limited_to_the_pinned_planner(self):
        transport = Mock(return_value=COMPAT_REPLY)
        adviser = LMStudio.from_settings(SETTINGS, transport=transport)
        override = LMStudio.from_settings(SETTINGS, planner_model="qwen-fixture", transport=transport)
        settings_without_profile = {**SETTINGS, LM_STUDIO_PLANNER_PROFILE_KEY: ""}
        default = LMStudio.from_settings(settings_without_profile, transport=transport)
        direct = LMStudio(SETTINGS["LM_STUDIO_BASE_URL"], {"planner": "direct-fixture"}, "", transport)
        cases = ((adviser, "critic", "critic-fixture"), (override, "planner", "qwen-fixture"),
                 (default, "planner", ORNITH_PLANNER_MODEL), (direct, "planner", "direct-fixture"))
        for client, role, model in cases:
            with self.subTest(role=role, model=model):
                self.assertEqual(client.choose(role, "Compare the protocols.", LABELS), "BIC")
                url, _, payload, _ = transport.call_args.args
                self.assertEqual(url, "http://127.0.0.1:8001/v1/chat/completions")
                self.assertEqual(payload["model"], model)
                self.assertEqual(payload["temperature"], 0)
                self.assertEqual(payload["response_format"]["type"], "json_schema")
                self.assertNotIn("reasoning", payload)

    def test_invalid_profile_cannot_be_hidden_by_override(self):
        for settings in ({LM_STUDIO_PLANNER_PROFILE_KEY: "unknown"}, {"LM_STUDIO_PLANNER_MODEL": "other"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                LMStudio.from_settings({**SETTINGS, **settings}, planner_model="qwen-fixture")

    def test_native_response_requires_exact_model_and_unambiguous_choice(self):
        invalid_content = (
            "", '```json\n{"choice":"AIC"}\n```', '{"choice":"AIC"}{"choice":"BIC"}',
            '{"choice":"AIC","choice":"BIC"}', '{"choice":"AIC","extra":0}', '{"choice":"UNKNOWN"}',
            '{"choice":false}', '<think>deliberation</think>{"choice":"AIC"}', b'{"choice":"AIC"}',
        )
        malformed_output = [[{"type": "message", "content": content}] for content in invalid_content]
        malformed_output.extend(([], [MESSAGE, MESSAGE],
                                 [{"type": "reasoning", "content": "private"}, MESSAGE], [{"type": "tool_call"}],
                                 [{**MESSAGE, "tool_calls": []}]))
        patches = [{"output": output} for output in malformed_output]
        patches.extend(({"model_instance_id": "different-instance"}, {"response_id": "resp_unexpected"},
                        {"stats": {}}, {"stats": {"reasoning_output_tokens": 1, "total_output_tokens": 8}},
                        {"stats": {"reasoning_output_tokens": False, "total_output_tokens": 8}},
                        {"stats": {"reasoning_output_tokens": 0, "total_output_tokens": ORNITH_MAX_OUTPUT_TOKENS}}))
        for patch in patches:
            with self.subTest(patch=patch):
                response = {**deepcopy(NATIVE_REPLY), **patch}
                transport = Mock(return_value=response)
                adviser = LMStudio.from_settings(SETTINGS, transport=transport)
                with self.assertRaisesRegex(ProviderError, "Ornith did not return one allowed Planner choice"):
                    adviser.choose("planner", "Compare the protocols.", LABELS)
                transport.assert_called_once()

    def test_settings_file_recognizes_profile_and_local_autouse(self):
        with TemporaryDirectory() as directory:
            settings = Path(directory) / "fixture.env"
            settings.write_text(f"{LM_STUDIO_PLANNER_PROFILE_KEY}=ornith\nLM_STUDIO_AUTO_USE=1\n", encoding="utf-8")
            self.assertEqual(read_env(settings), {LM_STUDIO_PLANNER_PROFILE_KEY: "ornith", "LM_STUDIO_AUTO_USE": "1"})


if __name__ == "__main__":
    unittest.main()
