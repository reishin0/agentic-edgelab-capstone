from contextlib import redirect_stderr, redirect_stdout
from importlib.util import find_spec
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from edgelab.capstone import main
from edgelab.providers import ORNITH_PLANNER_MODEL


@unittest.skipUnless(find_spec("langsmith"), "optional research dependencies absent")
class LocalPlannerStartupTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        self.journal = folder / "isolated.sqlite"
        settings = folder / "providers.env"
        settings.write_text(
            "LM_STUDIO_AUTO_USE=1\nLM_STUDIO_BASE_URL=http://127.0.0.1:8001/v1\n"
            f"LM_STUDIO_PLANNER_MODEL={ORNITH_PLANNER_MODEL}\nLM_STUDIO_PLANNER_PROFILE=ornith\n",
            encoding="utf-8",
        )
        self.enterContext(patch("edgelab.capstone.LOCAL_PLANNER_SETTINGS", settings))
        self.catalog = self.enterContext(patch("edgelab.capstone.local_catalog",
                                               return_value={"loaded_ids": [ORNITH_PLANNER_MODEL]}))
        self.graph = self.enterContext(patch("edgelab.capstone.build_graph"))
        self.graph.return_value.invoke.return_value = {"output": {"status": "fixture"}}

    def call_plan(self, *arguments):
        with (patch.object(sys, "argv", ["capstone", "--journal", str(self.journal), "plan", *arguments]),
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            main()

    def test_configured_local_planner_is_used_without_provider_flags(self):
        self.call_plan()
        model = self.graph.call_args.args[2]
        self.assertEqual(model.models["planner"], ORNITH_PLANNER_MODEL)
        self.assertEqual(model.planner_profile, "ornith")
        self.assertEqual(self.graph.call_args.kwargs["adviser_provider"], "lm_studio")
        self.assertFalse(self.graph.call_args.kwargs["use_model_critic"])
        self.catalog.assert_called_once()

    def test_no_model_stays_offline_with_automatic_profile_configured(self):
        with patch("edgelab.capstone.read_env") as read_settings:
            self.call_plan("--no-model")
        read_settings.assert_not_called()
        self.catalog.assert_not_called()
        self.assertIsNone(self.graph.call_args.args[2])

    def test_unloaded_default_stops_before_creating_a_plan(self):
        self.catalog.return_value = {"loaded_ids": ["another-model"]}
        with self.assertRaises(SystemExit) as error:
            self.call_plan()
        self.assertEqual(error.exception.code, 2)
        self.graph.assert_not_called()
        self.assertFalse(self.journal.exists())
