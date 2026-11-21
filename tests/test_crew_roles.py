"""Exercise the actual optional CrewAI framework with an offline model stub."""

from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from edgelab.crew_roles import CrewRoles, advisory_report
from edgelab.graph import build_graph
from edgelab.journal import Journal, JournalConflict
from edgelab.methods import local_notes
from edgelab.providers import LMStudio, ProviderError
from edgelab.reporting import report_from_journal
from edgelab.research import ResearchService


@unittest.skipUnless(find_spec("crewai"), "Install the optional crew extra")
class CrewTests(unittest.TestCase):
    def test_governed_graph_and_read_only_report_use_all_roles(self):
        calls = []

        def transport(url, headers, payload, timeout):
            self.assertEqual(url, "http://127.0.0.1:1234/v1/chat/completions")
            self.assertEqual(headers, {})
            self.assertLessEqual(timeout, 15)
            role = payload["model"]
            options = payload["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"]
            choice = next((label for label in ("evidence_first", "no_preference", "accept") if label in options),
                          options[0])
            if role == "reporter":
                context = payload["messages"][-1]["content"].split("\n", 1)[1]
                self.assertEqual(set(json.loads(context)), {"status", "verdict", "scope", "holdout", "review_rules"})
            calls.append(role)
            return {"choices": [{"message": {"content": json.dumps({"choice": choice})}}]}

        model = LMStudio("http://127.0.0.1:1234/v1", {r: r for r in ("planner", "critic", "reporter")},
                         transport=transport)
        with TemporaryDirectory() as temp, patch.dict("os.environ", {"CREWAI_DISABLE_TELEMETRY": "true",
                                                                    "PYTHON_DOTENV_DISABLED": "1",
                                                                    "OTEL_SDK_DISABLED": "true"}):
            journal = Journal(Path(temp) / "crew.sqlite")
            service = ResearchService(journal)
            roles = CrewRoles(model)
            graph = build_graph(service, local_notes, roles)
            run = str(uuid4())
            plan = graph.invoke({"mode": "plan", "run_id": run, "case": "independent", "backend": "local"})["output"]
            self.assertEqual(plan["status"], "awaiting_approval", plan["reason"])
            self.assertEqual(plan["role_backend"], "crewai")
            self.assertEqual([e["key"] for e in journal.events(run)], ["plan"])
            with self.assertRaises(JournalConflict):
                graph.invoke({"mode": "execute", "run_id": run, "digest": "0" * 64})
            before_execution = list(calls)
            graph.invoke({"mode": "execute", "run_id": run, "digest": plan["candidates"][0]["digest"]})
            self.assertEqual(calls, before_execution)
            reader = Journal(journal.path, read_only=True)
            canonical = report_from_journal(reader, run)
            decorated = advisory_report(canonical, roles)
            self.assertEqual(decorated["presentation"]["order"], "evidence_first")
            self.assertEqual(decorated["report"], canonical["report"])
            self.assertEqual(decorated["bundle"], canonical["bundle"])
            self.assertEqual(canonical, report_from_journal(reader, run))
            self.assertEqual(set(calls), {"planner", "critic", "reporter"})

    def test_invalid_choice_fails_closed(self):
        class InvalidModel:
            def choose(self, *args):
                return "approve_holdout"

        with patch.dict("os.environ", {"CREWAI_DISABLE_TELEMETRY": "true", "OTEL_SDK_DISABLED": "true",
                                       "PYTHON_DOTENV_DISABLED": "1"}):
            with self.assertRaises(ProviderError):
                CrewRoles(InvalidModel()).choose("critic", "Review", ("accept", "escalate"), 1)
