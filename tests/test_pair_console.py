from contextlib import closing, redirect_stderr
from datetime import date
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from importlib.util import find_spec
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import uuid4

from edgelab.contracts import fingerprint
from edgelab.dashboard import handler, main as dashboard_main, run_view
from edgelab.history import FrozenHistory
from edgelab.journal import Journal
from edgelab.market_history import CALENDAR, calendar_2025, store_history
from edgelab.methods import local_notes
from edgelab.pair_console import PairConsole, PairConsoleError
from edgelab.planning import search
from edgelab.reporting import report_from_journal
from edgelab.research import ResearchService
from test_history import make_export


ROOT = Path(__file__).resolve().parents[1]
ROUTE = "/api/pair-console"
FIXTURE_RUNTIME = {"identity": "isolated test runtime"}


class PairConsoleTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.manifest, _, metadata = make_export(self.folder)
        metadata["source"] = "Synthetic fixture; A=AAPL, B=MSFT; test only"
        self.manifest.write_text(json.dumps(metadata), encoding="utf-8")
        self.history = FrozenHistory.open(self.manifest)
        self.journal = self.folder / "research.sqlite"
        self.stocks = {"entries": [{"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "NVDA"}]}
        self.cross = {"fx": {"entries": [{"symbol": "EUR/USD"}]}, "crypto": {"entries": []}}
        self.console = self.open_console()
        self.selection = {
            "a": "AAPL", "b": "MSFT", "manifest": self.history.manifest_sha256, "capture": None,
        }

    def open_console(self, journal=None, *, root=ROOT, histories=None):
        configured = ((self.manifest, self.history),) if histories is None else histories
        console = PairConsole(journal or self.journal, configured, self.stocks, self.cross, root=root)
        self.addCleanup(console.close)
        return console

    def offline_plan(self, journal, history):
        service = ResearchService(Journal(journal), history=history, runtime_identity=lambda: FIXTURE_RUNTIME)
        plan = service.record_plan(str(uuid4()), None, search(local_notes("cointegration")), "local")
        return json.loads(json.dumps(plan))

    def test_only_configured_assets_and_exact_history_can_be_prepared(self):
        bootstrap = self.console.bootstrap()
        self.assertIsNone(bootstrap["job"])
        self.assertEqual({entry["symbol"] for entry in bootstrap["assets"]}, {"AAPL", "MSFT", "NVDA", "EUR/USD"})
        lookup = self.console.history("AAPL", "MSFT")
        self.assertEqual(lookup["histories"], [{
            "digest": self.history.manifest_sha256, "symbols": ["AAPL", "MSFT"],
            "start": self.history.window.start, "end": self.history.window.end, "classification": "synthetic",
        }])
        self.assertTrue(lookup["capture_allowed"])
        self.assertFalse(self.console.history("AAPL", "EUR/USD")["capture_allowed"])

        capture = {"start": "2025-01-02", "end": "2025-10-31"}
        invalid = (
            {"b": "AAPL"}, {"b": "UNKNOWN"}, {"b": "NVDA"}, {"manifest": "0" * 64},
            {"manifest": None}, {"capture": capture}, {"unexpected": True},
            {"b": "EUR/USD", "manifest": None, "capture": capture},
            {"manifest": None, "capture": {**capture, "end": "2025-11-04"}},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(PairConsoleError):
                self.console.prepare({**self.selection, **changes})
        for job_id in ("not-a-job", str(uuid4())):
            with self.subTest(job_id=job_id), self.assertRaises(PairConsoleError):
                self.console.status(job_id)
        self.assertIsNone(self.console.bootstrap()["job"])
        self.assertFalse(self.journal.exists())

    def wait_for_state(self, job_id, expected):
        deadline = monotonic() + 35
        while monotonic() < deadline:
            job = self.console.status(job_id)
            if job["state"] == expected:
                return job
            if job["state"] in {"complete", "failed", "discarded"}:
                self.fail(f"Expected {expected}, got {job['state']}: {job['console']}")
            sleep(0.03)
        self.fail(f"Expected {expected}, last job state: {self.console.status(job_id)['state']}")

    @unittest.skipUnless(find_spec("langgraph") and find_spec("statsmodels"), "optional research dependencies absent")
    def test_verified_report_is_required_before_the_console_is_complete(self):
        report_started, release_report = Event(), Event()
        run_subprocess = subprocess.run

        def defer_report(args, **options):
            if "edgelab.capstone" in args and "report" in args:
                report_started.set()
                if not release_report.wait(35):
                    raise TimeoutError("Test did not release the report")
            return run_subprocess(args, **options)

        with patch("edgelab.pair_console.subprocess.run", side_effect=defer_report) as runner:
            try:
                started = self.console.prepare(self.selection)
                job_id = started["id"]
                plan = self.wait_for_state(job_id, "awaiting_approval")
                run_id = plan["run_id"]
                reader = Journal(self.journal, read_only=True)
                self.assertEqual([event["key"] for event in reader.events(run_id)], ["plan"])
                self.assertIsNone(plan["verdict"])
                self.assertEqual(plan["history"]["digest"], self.history.manifest_sha256)
                self.assertIn("--no-model", runner.call_args_list[0].args[0])

                # Recover the exact candidate protocols and history after losing process memory.
                self.console.close()
                self.console = self.open_console()
                restored = self.console.bootstrap()["job"]
                for key in ("id", "run_id", "state", "symbols", "history", "candidates"):
                    self.assertEqual(restored[key], plan[key])
                saved_candidates = reader.get(run_id, "plan")["candidates"]
                self.assertEqual(len(plan["candidates"]), len(saved_candidates))
                for candidate, saved in zip(plan["candidates"], saved_candidates, strict=True):
                    self.assertEqual(candidate["protocol"], saved["protocol"])
                    self.assertEqual(candidate["digest"], fingerprint(candidate["protocol"]))
                    self.assertEqual(candidate["protocol"]["history"], self.history.identity())

                with self.assertRaises(PairConsoleError):
                    self.console.prepare(self.selection)
                with self.assertRaises(PairConsoleError):
                    self.console.approve({"job_id": job_id, "digest": "0" * 64})
                self.assertIsNone(reader.get(run_id, "approval"))

                digest = plan["candidates"][0]["digest"]
                approval = {"job_id": job_id, "digest": digest}
                self.console.approve(approval)
                self.assertTrue(report_started.wait(35), self.console.status(job_id))
                executing = self.console.status(job_id)
                self.assertEqual(executing["state"], "executing")
                self.assertIsNone(executing["verdict"])
                self.assertEqual(reader.get(run_id, "approval")["digest"], digest)
                with self.assertRaises(PairConsoleError):
                    self.console.approve(approval)
                release_report.set()
                completed = self.wait_for_state(job_id, "complete")
            finally:
                release_report.set()

        verified = report_from_journal(reader, run_id)["report"]
        self.assertEqual(completed["verdict"], verified["verdict"])
        self.assertEqual(verified["holdout"], "locked and untested")
        chart = run_view(reader, run_id, history_by_hash={}, pair_console=self.console)["chart"]
        self.assertEqual(chart["days"], list(self.history.sessions))
        self.assertIn(run_id, "\n".join(completed["console"]))
        self.console.close()
        reopened = self.open_console().bootstrap()["job"]
        self.assertEqual(reopened["id"], job_id)
        self.assertEqual(reopened["state"], "complete")
        self.assertEqual(reopened["run_id"], run_id)
        self.assertEqual(reopened["verdict"], completed["verdict"])
        self.assertEqual(runner.call_count, 3)
        self.assertTrue(all("--allow-provider-io" not in invocation.args[0] for invocation in runner.call_args_list))

    def test_captured_history_can_be_selected_after_restart_without_provider_io(self):
        root = self.folder
        journal = root / "captured.sqlite"
        self.console = self.open_console(journal, root=root, histories=())
        start, end = date(2025, 1, 2), date(2025, 10, 31)
        sessions = calendar_2025(start, end)
        csv = ("day,a,b\n" + "".join(f"{day},{300 + index},{400 + index}\n"
                                    for index, day in enumerate(sessions))).encode("ascii")
        manifest = {
            "schema_version": 1, "classification": "public", "calendar": CALENDAR,
            "source": "Alpaca IEX daily raw USD closes; A=AAPL, B=MSFT; local use only",
            "file": "paired.csv", "sha256": sha256(csv).hexdigest(), "sessions": list(sessions),
            "window": {"start": str(start), "end": str(end), "as_of": str(end), "holdout_start": "2025-11-03"},
        }
        path = store_history(root / ".cache" / "history", manifest, csv)
        history = FrozenHistory.open(path)
        calls = []

        def command(module, arguments, **options):
            calls.append((module, arguments))
            if module == "edgelab.market_history":
                return {"manifest": str(path.relative_to(root))}
            if module == "edgelab.capstone" and "plan" in arguments:
                return self.offline_plan(journal, history)
            raise AssertionError(f"Unexpected command: {module} {arguments}")

        with patch.object(PairConsole, "_command", side_effect=command):
            capture = {"a": "AAPL", "b": "MSFT", "manifest": None, "capture": {"start": str(start), "end": str(end)}}
            self.assertEqual(self.console.history("AAPL", "MSFT")["histories"], [])
            started = self.console.prepare(capture)
            plan = self.wait_for_state(started["id"], "awaiting_approval")
            self.console.discard({"job_id": started["id"]})
            self.console.close()
            self.console = self.open_console(journal, root=root, histories=())
            self.assertEqual(self.console.history("AAPL", "MSFT")["histories"], [plan["history"]])
            reuse = self.console.prepare({**capture, "manifest": history.manifest_sha256, "capture": None})
            reused = self.wait_for_state(reuse["id"], "awaiting_approval")
            self.assertEqual(reused["history"], plan["history"])

        self.assertEqual([module for module, _ in calls], ["edgelab.market_history", "edgelab.capstone", "edgelab.capstone"])
        self.assertIn("--allow-provider-io", calls[0][1])
        self.assertTrue(all("--allow-provider-io" not in arguments for _, arguments in calls[1:]))

    def test_interrupted_execution_does_not_replay_an_uncertain_reservation(self):
        plan = self.offline_plan(self.journal, self.history)
        with patch.object(self.console, "_capstone", return_value=plan):
            started = self.console.prepare(self.selection)
            awaiting = self.wait_for_state(started["id"], "awaiting_approval")
        digest = awaiting["candidates"][0]["digest"]
        approval = {"job_id": started["id"], "digest": digest}

        # Save the console's execution admission, then leave a real journal reservation without a result.
        self.console.close()
        child = """
import json
from pathlib import Path
import sys
from unittest.mock import patch

from edgelab.history import FrozenHistory
from edgelab.pair_console import PairConsole

journal, manifest = map(Path, sys.argv[1:3])
console = PairConsole(journal, ((manifest, FrozenHistory.open(manifest)),),
                      {"entries": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]}, None, root=Path.cwd())
with patch.object(console, "_start"):
    assert console.approve(json.loads(sys.argv[3]))["state"] == "executing"
"""
        process = subprocess.run([sys.executable, "-E", "-s", "-B", "-c", child,
                                  str(self.journal), str(self.manifest), json.dumps(approval)],
                                 cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)

        # The old worker may still append to this journal before its first reservation is visible.
        with patch.object(PairConsole, "_start") as worker:
            restored_before_reservation = self.open_console()
            with self.assertRaisesRegex(PairConsoleError, "approved execution was interrupted") as conflict:
                restored_before_reservation.prepare(self.selection)
            self.assertEqual(conflict.exception.status, 409)
            worker.assert_not_called()
            restored_before_reservation.close()

        class Interrupted(BaseException):
            pass

        def stop_after_reservation(*args, **kwargs):
            raise Interrupted()

        journal = Journal(self.journal)
        run_id = awaiting["run_id"]
        service = ResearchService(journal, history=self.history, numerical_tool=stop_after_reservation,
                                  runtime_identity=lambda: FIXTURE_RUNTIME)
        service.approve(run_id, digest)
        with self.assertRaises(Interrupted):
            service.action(run_id, "diagnostics")
        original_events = journal.events(run_id)
        self.assertEqual([event["key"] for event in original_events], ["plan", "approval", "reserved:diagnostics"])

        with patch.object(PairConsole, "_command") as command:
            restored = self.open_console()
            job = restored.status(started["id"])
            self.assertEqual(job["state"], "failed")
            self.assertIsNone(job["verdict"])
            with self.assertRaises(PairConsoleError):
                restored.approve(approval)
            with self.assertRaisesRegex(PairConsoleError, "unresolved tool reservation") as conflict:
                restored.prepare(self.selection)
            self.assertEqual(conflict.exception.status, 409)
            command.assert_not_called()
        self.assertEqual(journal.events(run_id), original_events)

    def test_dashboard_rejects_a_console_journal_alias(self):
        Journal(self.journal)
        alias = self.journal.with_name("alias.sqlite")
        try:
            alias.hardlink_to(self.journal)
        except OSError:
            self.skipTest("Test filesystem does not support hard links")
        arguments = ["edgelab.dashboard", "--journal", str(self.journal), "--pair-console-journal", str(alias),
                     "--waitlist", str(self.journal.with_name("fixture-waitlist.json"))]
        with (patch.object(sys, "argv", arguments), patch("edgelab.dashboard.load_waitlist", return_value={}),
              patch("edgelab.dashboard.ThreadingHTTPServer") as server, redirect_stderr(StringIO()) as error):
            with self.assertRaises(SystemExit) as stopped:
                dashboard_main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("Choose a separate journal", error.getvalue())
        server.assert_not_called()

    def open_http(self):
        dashboard = handler((self.journal,), self.folder / "news.json", self.folder / ".env", False,
                            pair_console=self.console)
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), dashboard))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"

        def request(method, path, data=None, *, caller=None, host=None):
            headers = {"Content-Type": "application/json"}
            if caller is not None:
                headers["Origin"] = caller
            if host is not None:
                headers["Host"] = host
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as client:
                client.request(method, path, json.dumps(data) if data is not None else None, headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())

        return request, origin

    def test_http_console_requires_loopback_origin_for_writes(self):
        request, origin = self.open_http()
        self.assertEqual(request("GET", ROUTE), (200, self.console.bootstrap()))
        self.assertEqual(request("GET", ROUTE, host="foreign.example")[0], 403)
        self.assertEqual(request("GET", ROUTE + "/history?a=AAPL&b=MSFT"),
                         (200, self.console.history("AAPL", "MSFT")))
        self.assertEqual(request("POST", ROUTE + "/prepare", self.selection)[0], 403)
        self.assertEqual(request("POST", ROUTE + "/prepare", self.selection, caller="https://foreign.example")[0], 403)
        self.assertEqual(request("POST", ROUTE + "/prepare", {**self.selection, "b": "UNKNOWN"}, caller=origin)[0],
                         400)
        self.assertIsNone(self.console.bootstrap()["job"])
        self.assertFalse(self.journal.exists())

    def test_damaged_console_receipt_leaves_saved_runs_available(self):
        plan = self.offline_plan(self.journal, self.history)
        receipt = self.journal.with_suffix(".sqlite.console.json")
        damaged = b'{"state":'
        receipt.write_bytes(damaged)
        self.console = self.open_console()
        request, origin = self.open_http()

        with patch.object(self.console, "_start") as start:
            status, catalog = request("GET", "/api/runs")
            self.assertEqual(status, 200)
            self.assertEqual([run["id"] for run in catalog["runs"]], [plan["run_id"]])
            self.assertEqual(request("GET", ROUTE)[0], 503)
            self.assertEqual(request("POST", ROUTE + "/prepare", self.selection, caller=origin)[0], 503)
            start.assert_not_called()
        self.assertEqual(receipt.read_bytes(), damaged)
