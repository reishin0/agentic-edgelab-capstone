import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from edgelab.model_settings import local_catalog, openrouter_adviser, openrouter_catalog, test_model as probe_model
from edgelab.providers import ProviderError


FREE = "example/structured:free"
PAID = "example/structured"
SETTINGS = {"OPENROUTER_API_KEY": "fake-catalog-token"}


def local_response():
    return {"models": [
        {"type": "llm", "key": "example/downloaded", "loaded_instances": []},
        {"type": "llm", "key": "example/stored", "display_name": "Example",
         "loaded_instances": [{"id": "selected-alias"}, {"id": "other-alias"}]},
        {"type": "embedding", "key": "embed", "loaded_instances": [{"id": "embed"}]},
    ]}


def cloud_model(model_id, prompt="0", completion="0", request="0", **overrides):
    return {"id": model_id, "name": model_id, "context_length": 4096,
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": prompt, "completion": completion, "request": request},
            "supported_parameters": ["structured_outputs", "response_format"], **overrides}


def structured_reply(model, choice="match"):
    return {"model": model, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"choice": choice})}}]}


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory()))

    def test_local_disk_uses_configured_location_without_service_request(self):
        root = self.home / "chosen models"
        weights = root / "publisher" / "model" / "fixture.gguf"
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b"fixture")
        desktop = self.home / ".lmstudio" / "settings.json"
        desktop.parent.mkdir()
        desktop.write_text(json.dumps({"downloadsFolder": str(root), "unrelated_value": "fixture"}), encoding="utf-8")
        failed_fetch = Mock(side_effect=AssertionError("Unexpected provider access"))
        catalog = local_catalog({}, home=self.home, transport=failed_fetch)
        self.assertEqual(catalog["status"], "disabled")
        self.assertEqual(catalog["models_directory"], str(root))
        self.assertEqual(catalog["downloaded"], [{"file": "publisher/model/fixture.gguf", "model_key": "publisher/model",
                                                  "size_bytes": 7}])
        self.assertEqual(catalog["loaded_ids"], [])
        explicit = self.home / "explicit"
        override = local_catalog({"LM_STUDIO_MODELS_DIR": str(explicit)}, home=self.home, transport=failed_fetch)
        self.assertEqual(override["models_directory"], str(explicit))
        failed_fetch.assert_not_called()

    def test_fallback_and_authoritative_local_loaded_ids(self):
        fallback = self.home / ".cache/lm-studio/models"
        fallback.mkdir(parents=True)
        fetch = Mock(return_value=local_response())
        settings = {"LM_STUDIO_BASE_URL": "http://127.0.0.1:8765/v1", "LM_STUDIO_API_KEY": "fake-local-token"}
        catalog = local_catalog(settings, home=self.home, allow_provider_io=True, transport=fetch)
        self.assertEqual(catalog["models_directory"], str(fallback))
        self.assertEqual(catalog["status"], "available")
        self.assertEqual(catalog["loaded_ids"], ["other-alias", "selected-alias"])
        models = {row["id"]: row for row in catalog["models"]}
        self.assertEqual(set(models), {"other-alias", "selected-alias", "example/downloaded"})
        self.assertEqual(models["selected-alias"]["model_key"], "example/stored")
        self.assertFalse(models["example/downloaded"]["loaded"])
        fetch.assert_called_once_with("http://127.0.0.1:8765/api/v1/models", {"Authorization": "Bearer fake-local-token"}, 5)
        with self.assertRaises(ValueError):
            local_catalog({"LM_STUDIO_BASE_URL": "http://example.invalid:1234/v1"}, home=self.home,
                          allow_provider_io=True, transport=fetch)
        self.assertEqual(fetch.call_count, 1)

    def test_cloud_catalog_uses_account_view_and_filters_metadata(self):
        fetch = Mock(return_value={"data": [
            cloud_model(FREE), cloud_model(PAID, prompt="0.000001", completion="0.000002"),
            cloud_model("example/unstructured:free", supported_parameters=["response_format"]),
            cloud_model("example/nonzero:free", request="0.001"),
            cloud_model("example/unsuffixed-zero"), cloud_model("openrouter/auto", prompt="0.001"),
            cloud_model("example/vision-only:free", architecture={"input_modalities": ["image"], "output_modalities": ["text"]}),
            cloud_model("example/no-price:free", pricing={"prompt": "0"}),
        ]})
        self.assertEqual(openrouter_catalog(SETTINGS, allow_provider_io=False, transport=fetch)["status"], "disabled")
        self.assertEqual(openrouter_catalog({}, allow_provider_io=True, transport=fetch)["status"], "not_configured")
        fetch.assert_not_called()
        free = openrouter_catalog(SETTINGS, allow_provider_io=True, transport=fetch)
        self.assertEqual([row["id"] for row in free["models"]], [FREE])
        fetch.assert_called_once_with("https://openrouter.ai/api/v1/models/user", {"Authorization": "Bearer fake-catalog-token"}, 5)
        both = openrouter_catalog(SETTINGS, allow_provider_io=True, free_only=False, transport=fetch)
        self.assertEqual([row["id"] for row in both["models"]], [FREE, PAID])

    def test_paid_metadata_requires_explicit_selection_and_price_caps(self):
        fetch = Mock(return_value={"data": [cloud_model(PAID, prompt="0.000001", completion="0.000002", request="0.01")]})
        chat = Mock(return_value=structured_reply(PAID))
        with self.assertRaises(ValueError):
            openrouter_adviser(SETTINGS, PAID, allow_provider_io=False, catalog_transport=fetch, transport=chat)
        fetch.assert_not_called()
        with self.assertRaises(ProviderError):
            openrouter_adviser(SETTINGS, PAID, allow_provider_io=True, catalog_transport=fetch, transport=chat)
        chat.assert_not_called()
        model = openrouter_adviser(SETTINGS, PAID, allow_provider_io=True, free_only=False,
                                   catalog_transport=fetch, transport=chat)
        self.assertEqual(set(model.models.values()), {PAID})
        self.assertEqual(model.choose("critic", "Compare fixture labels.", ("match", "mismatch")), "match")
        _, _, payload, _ = chat.call_args.args
        self.assertEqual(payload["provider"]["max_price"], {"prompt": 1.0, "completion": 2.0, "request": 0.01, "image": 0.0})

    def test_cloud_probe_is_synthetic_strict_single_request_and_free(self):
        fetch = Mock(return_value={"data": [cloud_model(FREE)]})
        chat = Mock(return_value=structured_reply(FREE))
        result = probe_model(SETTINGS, "openrouter", FREE, allow_provider_io=True, catalog_transport=fetch, transport=chat)
        self.assertEqual((result["status"], result["choice"]), ("passed", "match"))
        chat.assert_called_once()
        url, headers, payload, timeout = chat.call_args.args
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(headers, {"Authorization": "Bearer fake-catalog-token"})
        self.assertEqual(timeout, 15)
        self.assertEqual(payload["model"], FREE)
        self.assertEqual(payload["provider"], {"require_parameters": True, "allow_fallbacks": False,
                                               "data_collection": "deny", "zdr": True,
                                               "max_price": dict.fromkeys(("prompt", "completion", "request", "image"), 0)})
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertFalse({"tools", "plugins", "models", "route"} & payload.keys())
        self.assertIn("both numbers are 7", payload["messages"][1]["content"])
        chat.return_value = structured_reply(FREE, "alter_verdict")
        with self.assertRaises(ProviderError):
            probe_model(SETTINGS, "openrouter", FREE, allow_provider_io=True, catalog_transport=fetch, transport=chat)

    def test_local_probe_rejects_unloaded_and_never_requests_a_load(self):
        fetch = Mock(return_value=local_response())
        chat = Mock(return_value=structured_reply("selected-alias", "mismatch"))
        with self.assertRaises(ProviderError):
            probe_model({}, "lm_studio", "example/downloaded", allow_provider_io=True, catalog_transport=fetch,
                        transport=chat, home=self.home)
        chat.assert_not_called()
        result = probe_model({}, "lm_studio", "selected-alias", allow_provider_io=True, catalog_transport=fetch,
                             transport=chat, home=self.home)
        self.assertEqual((result["status"], result["choice"]), ("failed", "mismatch"))
        self.assertEqual(chat.call_args.args[0], "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(chat.call_args.args[2]["model"], "selected-alias")


if __name__ == "__main__":
    unittest.main()
