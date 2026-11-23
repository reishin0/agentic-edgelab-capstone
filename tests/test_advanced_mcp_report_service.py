from contextlib import redirect_stderr
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from edgelab.history import FrozenHistory
from edgelab.journal import Journal
from edgelab.methods import local_notes
from edgelab.planning import search
from edgelab.reporting import report_from_journal
from edgelab.research import ResearchService
from edgelab.stat_tools import TOOL_SEQUENCE
from integrations.advanced_mcp.report_service import (
    TOKEN_ENV, ReportService, StudyUnavailable, load_manifest, main, make_handler, validate_token,
)
from observation_fixtures import RUNTIME, bound, roots
from test_history import make_export


TOKEN = "fake-adapter-test-token-0123456789ABCDEF"
HOST = "127.0.0.1:8799"
INTERNAL = "local-only-fixture-canary"


class MemorySocket:
    """Drive the actual HTTP handler without opening a network socket."""

    def __init__(self, request):
        self.request = BytesIO(request)
        self.response = BytesIO()

    def settimeout(self, _timeout):
        pass

    def makefile(self, _mode, _size):
        return self.request

    def sendall(self, data):
        self.response.write(data)


class ReportServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "synthetic.sqlite"
        self.manifest = self.root / "release.json"
        self.journal = Journal(self.path)

    def create_run(self, case, *, disposition="finished", history=None):
        values = {
            "diagnostics": {"a": roots(), "b": roots(), "private": INTERNAL},
            "engle_granger": {"a_on_b": {"p": 1e-7}, "b_on_a": {"p": 2e-7}},
            "stability": {"cusum_p": 0.001 if case == "spread-level-shift" else 0.25,
                          "ecm_loading": -0.25, "ecm_loading_p": 0.01, "ar1_phi": 0.5,
                          "ou_half_life_observations": 1.0},
        }

        def fixture(tool, rows, window, lag, **_):
            return bound(tool, rows, window, lag, values[tool])

        service = ResearchService(self.journal, history=history, numerical_tool=fixture,
                                  runtime_identity=lambda: RUNTIME)
        run = str(uuid4())
        notes = () if disposition == "no approval" else local_notes("cointegration")
        proposal = {**search(notes), "internal_path": INTERNAL}
        plan = service.record_plan(run, case if history is None else None, proposal, "local")
        if disposition != "no approval":
            approval = service.approve(run, plan["candidates"][0]["digest"])
            if disposition == "uncertain reservation":
                self.journal.append(run, "reserved:diagnostics", service._binding(approval, "diagnostics"))
            else:
                for tool in TOOL_SEQUENCE:
                    service.action(run, tool)
                    service.critique(run, tool)
                service.finalize(run)
        return report_from_journal(self.journal, run)

    def configure(self, **reports):
        self.manifest.write_text(json.dumps({"schema_version": 1, "studies": {
            alias: {"run_id": report["run_id"], "ledger_sha256": report["bundle"]["ledger_sha256"]}
            for alias, report in reports.items()
        }}), encoding="utf-8")
        return ReportService(self.path, self.manifest)

    def request(self, service, request_line, headers=()):
        raw = "\r\n".join((request_line + " HTTP/1.1", *headers, "", "")).encode("ascii")
        socket = MemorySocket(raw)
        make_handler(service, TOKEN)(socket, ("127.0.0.1", 12345), SimpleNamespace(server_port=8799))
        head, _, body = socket.response.getvalue().partition(b"\r\n\r\n")
        return int(head.splitlines()[0].split()[1]), json.loads(body) if body else None

    def test_projects_canonical_verdict_diagnostics_and_evidence_without_writes(self):
        continued = self.create_run("shared-trend")
        stopped = self.create_run("spread-level-shift")
        service = self.configure(**{"shared-trend": continued, "spread-level-shift": stopped})
        before = sha256(self.path.read_bytes()).hexdigest()

        listed = service.list_runs()
        self.assertEqual(listed["schema_version"], 1)
        self.assertEqual([(row["study_id"], row["verdict"]) for row in listed["runs"]],
                         [("shared-trend", "continue"), ("spread-level-shift", "stop")])
        for item, canonical, cusum, rule, loading, half_life in (
            (listed["runs"][0], continued, 0.25, "CUSUM_NOT_REJECTED", -0.25, 1.0),
            (listed["runs"][1], stopped, 0.001, "CUSUM_REJECTS", None, None),
        ):
            with self.subTest(study=item["study_id"]):
                projected = service.report(item["study_id"])
                self.assertEqual(set(item), {"study_id", "run_id", "verdict", "ledger_sha256"})
                self.assertEqual(projected["schema_version"], 1)
                self.assertEqual(projected["scope"], "synthetic in-sample")
                self.assertEqual(projected["holdout"], "locked and untested")
                self.assertEqual(projected["window"], canonical["report"]["window"])
                self.assertEqual(projected["reason"], canonical["report"]["reason"])
                self.assertEqual(projected["ledger_sha256"], canonical["bundle"]["ledger_sha256"])
                self.assertEqual(projected["diagnostics"], {"engle_granger_a_on_b_p": 1e-7,
                    "engle_granger_b_on_a_p": 2e-7, "cusum_p": cusum, "ecm_loading": loading,
                    "ou_half_life_observations": half_life})
                hashes = {event["key"]: event["sha256"] for event in canonical["bundle"]["events"]}
                refs = {row["event_key"]: row for row in projected["evidence"]}
                self.assertEqual(set(refs), {"verdict", *(f"{side}:{tool}" for side in ("result", "review")
                                                         for tool in TOOL_SEQUENCE)})
                self.assertEqual({key: row["sha256"] for key, row in refs.items()}, {key: hashes[key] for key in refs})
                self.assertEqual(refs["review:stability"]["critic_rule"], rule)
                self.assertNotIn(INTERNAL, json.dumps(projected))
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), before)

    def test_unapproved_uncertain_or_imported_studies_are_hidden(self):
        manifest_path, _, _ = make_export(self.root)
        imported = FrozenHistory.open(manifest_path)
        for forbidden in (
            self.create_run("shared-trend", disposition="no approval"),
            self.create_run("shared-trend", disposition="uncertain reservation"),
            self.create_run("shared-trend", history=imported),
        ):
            with self.subTest(run=forbidden["run_id"]):
                service = self.configure(pinned=forbidden)
                self.assertEqual(service.list_runs()["runs"], [])
                with self.assertRaisesRegex(StudyUnavailable, "^Study unavailable$"):
                    service.report("pinned")

    def test_canonical_case_aliases_reject_swapped_valid_pins(self):
        stable = self.create_run("shared-trend")
        shifted = self.create_run("spread-level-shift")
        service = self.configure(**{"shared-trend": shifted, "spread-level-shift": stable})
        self.assertEqual(service.list_runs()["runs"], [])
        for case in ("shared-trend", "spread-level-shift"):
            with self.subTest(case=case), self.assertRaises(StudyUnavailable):
                service.report(case)

    def test_pin_mismatch_and_corrupt_ledger_are_hidden(self):
        saved = self.create_run("shared-trend")
        wrong = {**saved, "bundle": {**saved["bundle"], "ledger_sha256": "0" * 64}}
        access = (f"Host: {HOST}", f"Authorization: Bearer {TOKEN}")
        service = self.configure(pinned=wrong)
        unknown = self.request(service, "GET /v1/runs/unknown", access)
        self.assertEqual(unknown, (404, {"error": "Study unavailable"}))
        self.assertEqual(self.request(service, "GET /v1/runs/pinned", access), unknown)
        service = self.configure(pinned=saved)
        with self.journal.connect() as db:
            db.execute("UPDATE edgelab_events SET body=? WHERE event_key='verdict'", ('{"changed":true}',))
        self.assertEqual(service.list_runs()["runs"], [])
        self.assertEqual(self.request(service, "GET /v1/runs/pinned", access), unknown)

    def test_http_requires_service_bearer_and_exact_loopback_read_routes(self):
        service = self.configure(pinned=self.create_run("shared-trend"))
        host, auth = f"Host: {HOST}", f"Authorization: Bearer {TOKEN}"
        with patch.object(service, "list_runs", wraps=service.list_runs) as list_runs:
            self.assertEqual(self.request(service, "GET /v1/runs", (host,))[0], 401)
            self.assertEqual(self.request(service, "GET /v1/runs", (host, "Authorization: Bearer wrong"))[0], 401)
            self.assertEqual(self.request(service, "GET /v1/runs", (host, auth, auth))[0], 401)
            self.assertEqual(self.request(service, "GET /v1/runs", ("Host: outside.example", auth))[0], 403)
            self.assertEqual(self.request(service, "GET /v1/runs", (host, auth, "Origin: https://outside.example"))[0], 403)
            self.assertEqual(self.request(service, "POST /v1/runs", (host, auth))[0], 405)
            for target in ("//v1/runs", "/v1/runs?run_id=other", "/v1/runs/pinned?fields=*",
                           "/v1/runs/pinned/", "/v1/runs/%70inned", "/.env"):
                self.assertEqual(self.request(service, f"GET {target}", (host, auth))[0], 404)
            self.assertEqual(self.request(service, "GET /v1/runs", (host, auth, "Content-Length: 1"))[0], 400)
            list_runs.assert_not_called()
            status, body = self.request(service, "GET /v1/runs", (host, auth))
            self.assertEqual(status, 200)
            self.assertEqual([row["study_id"] for row in body["runs"]], ["pinned"])
            list_runs.assert_called_once_with()
        self.assertEqual(self.request(service, "GET /v1/runs/pinned", (host, auth))[0], 200)

    def test_manifest_rejects_extra_fields_noncanonical_ids_and_duplicate_entries(self):
        run = str(uuid4())
        pin = {"run_id": run, "ledger_sha256": "0" * 64}
        for invalid in (json.dumps({"schema_version": True, "studies": {"pinned": pin}}),
                        json.dumps({"schema_version": 1, "studies": {"pinned": {**pin, "path": "other.sqlite"}}}),
                        json.dumps({"schema_version": 1, "studies": {"pinned": {**pin, "run_id": run.upper()}}}),
                        json.dumps({"schema_version": 1, "studies": {"pinned": pin, "alternate": pin}}),
                        '{"schema_version":1,"studies":{},"studies":{}}'):
            with self.subTest(invalid=invalid):
                self.manifest.write_text(invalid, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_manifest(self.manifest)

    def test_cli_requires_explicit_token_source_and_binds_loopback_only(self):
        self.configure(pinned=self.create_run("shared-trend"))
        argv = ["report_service", "--journal", str(self.path), "--manifest", str(self.manifest), "--port", "9821"]
        for weak in ("", "oneword", "A" * 64, "bad / token in file"):
            with self.assertRaisesRegex(ValueError, "Invalid dedicated adapter service token"):
                validate_token(weak)
        with patch.dict("os.environ", {TOKEN_ENV: TOKEN}), patch("sys.argv", argv), redirect_stderr(StringIO()):
            with patch("integrations.advanced_mcp.report_service.ThreadingHTTPServer") as server:
                with self.assertRaises(SystemExit):
                    main()
                server.assert_not_called()
        with patch.dict("os.environ", {TOKEN_ENV: TOKEN}), patch("sys.argv", [*argv, "--token-env"]):
            with patch("integrations.advanced_mcp.report_service.ThreadingHTTPServer") as server:
                main()
                self.assertEqual(server.call_args.args[0], ("127.0.0.1", 9821))
                server.return_value.__enter__.return_value.serve_forever.assert_called_once_with()
