from contextlib import closing
from hashlib import sha256
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from edgelab.dashboard import configuration, demo_runs, handler, news_view, recent_runs, run_view, seed_demo_news
from edgelab.history import FrozenHistory
from edgelab.journal import Journal, JournalConflict
from edgelab.methods import local_notes
from edgelab.news import NEWS_CACHE, cache, fetch
from edgelab.planning import search
from edgelab.providers import ProviderError
from edgelab.reporting import report_from_journal
from edgelab.research import ResearchService
from edgelab.stat_tools import ALPHA
from observation_fixtures import RUNTIME, bound, roots
from test_history import make_export


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "journal.sqlite"
        self.news = self.root / "news.json"
        self.env = self.root / ".env"

    def test_http_is_local_read_only_and_serves_only_allowlisted_assets(self):
        self.env.write_text("PINECONE_API_KEY=test-secret\n", encoding="utf-8")
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), handler((self.path,), self.news, self.env, False)))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        def request(method, route, headers=None):
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                client.request(method, route, headers=headers or {})
                response = client.getresponse()
                return response.status, response.read()
            finally:
                client.close()

        self.assertEqual(request("GET", "/")[0], 200)
        self.assertEqual(request("GET", "/.env")[0], 404)
        self.assertEqual(request("GET", "/api/run")[0], 400)
        self.assertEqual(request("POST", "/api/run")[0], 501)
        self.assertEqual(request("GET", "/api/runs", {"Host": "external.example"})[0], 403)
        self.assertNotIn(b"test-secret", request("GET", "/api/runs")[1])
        self.assertFalse(self.path.exists())

    def test_configuration_badges_use_the_diagnostic_settings_file(self):
        self.env.write_text("PINECONE_INDEX_HOST=old.svc.test.pinecone.io\nPINECONE_API_KEY=fixture\n"
                            "EDGELAB_NEWS_KEY=fixture\nEDGELAB_NEWS_SECRET=fixture\n", encoding="utf-8")
        providers = self.root / "providers.env"
        self.assertEqual(configuration(self.env, provider_env=providers), {
            "lm_studio": "Not configured", "pinecone": "Not configured", "news": "Configured",
        })
        providers.write_text("PINECONE_INDEX_HOST=new.svc.test.pinecone.io\nPINECONE_API_KEY=fixture\n", encoding="utf-8")
        self.assertEqual(configuration(self.env, provider_env=providers)["pinecone"], "Configured")

    def test_settings_diagnostics_require_origin_and_never_open_research_journal(self):
        providers = self.root / "providers.env"
        providers.write_text("OPENROUTER_API_KEY=fixture-private-token\n", encoding="utf-8")
        remote = self.enterContext(patch("edgelab.dashboard.openrouter_catalog", return_value={
            "provider": "openrouter", "status": "available", "models": [],
        }))
        dashboard = handler((self.path,), self.news, self.env, False, provider_settings_path=providers)
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), dashboard))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        host = f"127.0.0.1:{server.server_port}"

        def request(method, path, data=None, origin=None):
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as client:
                client.request(method, path, json.dumps(data) if data is not None else None, headers)
                response = client.getresponse()
                return response.status, response.read()

        url = "/api/settings/catalog"
        body = {"provider": "openrouter", "free_only": True}
        self.assertEqual(request("GET", url)[0], 404)
        self.assertEqual(request("POST", url, body)[0], 403)
        self.assertEqual(request("POST", url, body, "https://example.com")[0], 403)
        origin = "http://" + host
        self.assertEqual(request("POST", url, {**body, "prompt": "free text"}, origin)[0], 400)
        remote.assert_not_called()
        status, raw = request("POST", url, body, origin)
        self.assertEqual(status, 200)
        self.assertNotIn(b"fixture-private-token", raw)
        remote.assert_called_once_with({"OPENROUTER_API_KEY": "fixture-private-token"},
                                       allow_provider_io=True, free_only=True)
        status, raw = request("POST", "/api/settings/test-retrieval", {"backend": "local"}, origin)
        self.assertEqual(status, 200)
        self.assertEqual({row["id"] for row in json.loads(raw)["citations"]}, {"adf", "kpss", "eg"})
        self.assertFalse(self.path.exists())

    def test_entry_cost_route_exposes_only_pinned_summary(self):
        symbols = ("SPY", "IVV", "VOO")
        result_path = self.root / "result.json"
        summary = {"scheduled_sessions": 1, "matched_sessions": 1,
                   "spread_bps": {symbol: {"median": "0.20"} for symbol in symbols},
                   "scenarios": {mode: {symbol: {"cost_bps": {"mean": "1.50"}} for symbol in symbols}
                                 for mode in ("base", "stress")}}
        session = {"day": "2025-01-02", "status": "matched", "clocks_examined": 31, "all_orders_fit_clocks": 20,
                   "scenarios": {mode: {symbol: {"cost_usd": "3.75", "quote_packet_utc": "private packet"}
                                        for symbol in symbols} for mode in ("base", "stress")}}
        raw = json.dumps({"summary": summary, "sessions": [session],
                          "capital_usd_per_hypothetical_purchase": "25000"}).encode()
        result_path.write_bytes(raw)
        self.enterContext(patch("edgelab.etf_entry_costs.RESULT_SHA256", sha256(raw).hexdigest()))

        def listen(dashboard):
            server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), dashboard))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            return server

        def request(server, route):
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as client:
                client.request("GET", route)
                response = client.getresponse()
                return response.status, json.loads(response.read())

        route = "/api/etf-entry-costs"
        configured = listen(handler((self.path,), self.news, self.env, False, etf_entry_costs_path=result_path))
        status, data = request(configured, route)
        self.assertEqual(status, 200)
        self.assertEqual((data["matched_sessions"], data["clocks_fit"], data["budget_usd"]), (1, 20, 25000))
        self.assertEqual(tuple(row["symbol"] for row in data["rows"]), symbols)
        self.assertEqual(data["rows"][0]["base_mean_cost_usd"], 3.75)
        self.assertNotIn("private packet", json.dumps(data))
        self.assertEqual(request(configured, route + "?path=result.json")[0], 404)
        self.assertEqual(request(configured, "/result.json")[0], 404)
        result_path.write_bytes(raw + b"\n")
        self.assertEqual(request(configured, route)[0], 503)
        demo = listen(handler((self.path,), self.news, self.env, True, etf_entry_costs_path=result_path))
        self.assertEqual(request(demo, route), (200, None))
        self.assertFalse(self.path.exists())

    def test_projection_does_not_plot_unapproved_samples_and_uses_read_only_journal(self):
        journal = Journal(self.path)
        tool = Mock(side_effect=lambda *args: bound(*args, {"a": roots(), "b": roots()}))
        service = ResearchService(journal, numerical_tool=tool, runtime_identity=lambda: RUNTIME)
        run = str(uuid4())
        plan = service.record_plan(run, "shared-trend", search(local_notes("cointegration")), "local")
        view = Journal(self.path, read_only=True)
        self.assertEqual(recent_runs(view), [{
            "id": run, "label": "shared-trend", "status": "awaiting_approval", "verdict": None,
        }])
        self.assertIsNone(run_view(view, run)["chart"])
        service.approve(run, plan["candidates"][0]["digest"])
        self.assertIsNone(run_view(view, run)["chart"])
        service.action(run, "diagnostics")
        service.critique(run, "diagnostics")
        before = self.path.read_bytes()
        data = run_view(view, run)
        self.assertEqual(len(data["chart"]["days"]), 180)
        self.assertLess(data["chart"]["days"][-1], data["approval"]["protocol"]["window"]["holdout_start"])
        self.assertEqual(data["reviews"]["diagnostics"]["decision"], "proceed")
        self.assertEqual(before, self.path.read_bytes())
        tool.assert_called_once()

    def test_recent_runs_reflect_verified_verdicts_and_pending_execution(self):
        journal = Journal(self.path)
        reader = Journal(self.path, read_only=True)
        states = [
            ([], "awaiting_approval", None),
            (["approval"], "approved", None),
            (["approval", "reserved:diagnostics"], "reconciliation required", None),
            (["approval", "reserved:diagnostics", "result:diagnostics"], "approved", None),
            (["verdict"], "finished", "unresolved"),
            (["approval", "verdict"], "finished", "continue"),
            (["approval", "verdict"], "finished", "stop"),
        ]
        expected = []
        for keys, status, verdict in states:
            run = str(uuid4())
            journal.append(run, "plan", {"status": "awaiting_approval", "input": {"case": "shared-trend"}})
            for key in keys:
                journal.append(run, key, {"verdict": verdict} if key == "verdict" else {"fixture": key})
            expected.append({"id": run, "label": "shared-trend", "status": status, "verdict": verdict})
        before = self.path.read_bytes()
        self.assertEqual(recent_runs(reader), list(reversed(expected)))
        self.assertEqual(self.path.read_bytes(), before)
        with journal.connect() as db:
            db.execute("UPDATE edgelab_events SET body = '{}' WHERE run_id = ? AND event_key = 'verdict'", (run,))
        with self.assertRaises(JournalConflict):
            recent_runs(reader)

    def test_malformed_spread_fit_preserves_recorded_verdict_and_price_chart(self):
        journal = Journal(self.path)
        reader = Journal(self.path, read_only=True)
        dashboard = handler((self.path,), self.news, self.env, False)
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), dashboard))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        fit = {"p": 0.5, "slope": 1.0, "intercept": 0.0}
        valid_result = {"a_on_b": fit, "b_on_a": fit}
        cases = (
            ("configuration", valid_result, {"config": []}, "unresolved"),
            ("result", [], {}, "unresolved"),
            ("fit", {**valid_result, "a_on_b": []}, {}, "unresolved"),
            ("overflowing coefficient", {**valid_result, "a_on_b": {**fit, "slope": 10**400}}, {}, "stop"),
            ("nonfinite spread", {**valid_result, "a_on_b": {**fit, "slope": 1e308}}, {}, "stop"),
        )
        for label, result, overrides, verdict in cases:
            with self.subTest(label=label):
                def compute(tool, rows, window, lag):
                    if tool == "diagnostics":
                        return bound(tool, rows, window, lag, {"a": roots(), "b": roots()})
                    return {**bound(tool, rows, window, lag, result), **overrides}

                service = ResearchService(journal, numerical_tool=compute, runtime_identity=lambda: RUNTIME)
                run = str(uuid4())
                plan = service.record_plan(run, "shared-trend", search(local_notes("cointegration")), "local")
                service.approve(run, plan["candidates"][0]["digest"])
                for step in ("diagnostics", "engle_granger"):
                    service.action(run, step)
                    service.critique(run, step)
                service.finalize(run)

                report = report_from_journal(reader, run)["report"]
                self.assertEqual(report["verdict"], verdict)
                with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as client:
                    client.request("GET", f"/api/run?id={run}")
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    view = json.loads(response.read())
                self.assertEqual(view["verdict"], {key: report[key] for key in ("verdict", "reason", "evidence_refs")})
                self.assertEqual(len(view["chart"]["days"]), 180)
                self.assertNotIn("spread", view["chart"])

    def test_demo_detects_planted_spread_level_shift(self):
        demo_runs(self.path)
        journal = Journal(self.path, read_only=True)
        runs = {run["label"]: run["id"] for run in recent_runs(journal)}
        self.assertIn("structural-break", runs)
        run_id = runs["spread-level-shift"]
        report = report_from_journal(journal, run_id)["report"]
        self.assertEqual([review["rule_id"] for review in report["milestone_reviews"]],
                         ["ROOT_BOTH_I1", "EG_BOTH_REJECT", "CUSUM_REJECTS"])
        self.assertLess(report["milestone_reviews"][-1]["details"]["cusum_p"], ALPHA)
        self.assertEqual(report["verdict"], "stop")
        self.assertEqual(report["holdout"], "locked and untested")
        view = run_view(journal, run_id)
        self.assertEqual(view["verdict"]["verdict"], report["verdict"])
        self.assertEqual(len(view["chart"]["spread"]), 180)

    def test_multiple_journals_use_their_exact_approved_samples(self):
        paths, histories, ids = [], [], []
        for name, pair in (("stock", ("SPY", "QQQ")), ("fx", ("EUR/USD", "GBP/USD"))):
            folder = self.root / name
            folder.mkdir()
            path, _, doc = make_export(folder)
            doc["source"] = f"Test fixture; A={pair[0]}, B={pair[1]}; fixture only"
            path.write_text(json.dumps(doc), encoding="utf-8")
            history = FrozenHistory.open(path)
            journal = Journal(folder / "research.sqlite")
            service = ResearchService(journal, history=history,
                                      numerical_tool=lambda *args, **_: bound(*args, {"a": roots(), "b": roots()}),
                                      runtime_identity=lambda: RUNTIME)
            run_id = str(uuid4())
            plan = service.record_plan(run_id, None, search(local_notes("cointegration")), "local")
            service.approve(run_id, plan["candidates"][0]["digest"])
            service.action(run_id, "diagnostics")
            paths.append(journal.path)
            histories.append(history)
            ids.append(run_id)
        before = [path.read_bytes() for path in paths]
        server = self.enterContext(ThreadingHTTPServer(
            ("127.0.0.1", 0), handler(tuple(paths), self.news, self.env, False, tuple(reversed(histories)))))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        def get(route):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("GET", route)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        code, index = get("/api/runs")
        self.assertEqual(code, 200)
        self.assertEqual([(row["id"], row["symbols"]) for row in index["runs"]],
                         [(ids[0], ["SPY", "QQQ"]), (ids[1], ["EUR/USD", "GBP/USD"])])
        for run_id, history in zip(ids, histories):
            code, data = get(f"/api/run?id={run_id}")
            self.assertEqual(code, 200)
            self.assertEqual(data["chart"]["source"], history.source)
        self.assertIsNone(run_view(Journal(paths[1], read_only=True), ids[1], histories[0])["chart"])
        self.assertEqual(get(f"/api/run?id={uuid4()}")[0], 404)
        self.assertEqual(before, [path.read_bytes() for path in paths])
        first, second = map(Journal, paths)
        second.append(ids[0], "plan", first.get(ids[0], "plan"))
        self.assertEqual(get(f"/api/run?id={ids[0]}")[0], 503)
        self.assertEqual(get("/api/runs")[0], 503)

    def test_public_demo_does_not_read_env_or_use_live_cache(self):
        demo_cache = self.root / "demo" / NEWS_CACHE
        seed_demo_news(demo_cache)
        self.assertEqual(demo_cache.parent.name, ".cache")
        self.assertEqual(news_view(demo_cache)["status"], "News cache unreadable")
        view = news_view(demo_cache, demo=True)
        self.assertEqual(view["status"], "Synthetic fixture")
        self.assertTrue(all(not row["url"] for row in view["articles"]))
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), handler((self.path,), demo_cache, self.env, True)))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        with patch("edgelab.dashboard.configuration") as read_configuration:
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                client.request("GET", "/api/runs")
                response = client.getresponse()
                self.assertIn(b"Disabled in demo", response.read())
                client.request("GET", "/api/news")
                response = client.getresponse()
                self.assertIn(b"Synthetic fixture", response.read())
            finally:
                client.close()
            read_configuration.assert_not_called()

    def test_news_snapshot_deduplication_safety_and_freshness(self):
        raw = {"id": 123, "headline": "<script>untrusted</script>", "source": "example",
               "url": "javascript:alert(1)", "symbols": ["AAPL"], "created_at": "2026-01-01T00:00:00Z",
               "updated_at": "2026-01-01T00:00:00Z", "content": "not stored"}
        transport = Mock(return_value={"news": [raw, {**raw, "headline": "Updated", "updated_at": "2026-01-01T00:01:00Z"}]})
        with self.assertRaises(ProviderError):
            fetch({}, ("AAPL",), transport)
        transport.assert_not_called()
        data = fetch({"EDGELAB_NEWS_KEY": "fixture-key", "EDGELAB_NEWS_SECRET": "fixture-secret"}, ("AAPL",), transport)
        self.assertIn("include_content=false", transport.call_args.args[0])
        self.assertEqual(len(data["articles"]), 1)
        self.assertEqual(data["articles"][0]["headline"], "Updated")
        self.assertEqual(data["articles"][0]["url"], "")
        self.assertNotIn("content", data["articles"][0])
        self.assertEqual(data["origin"], "alpaca")
        cache(self.news, data)
        self.assertNotIn("fixture-secret", self.news.read_text(encoding="utf-8"))
        self.assertEqual(news_view(self.news)["status"], "Latest snapshot")
        self.assertEqual(news_view(self.news, demo=True)["status"], "News cache unreadable")
        cache(self.news, {**data, "ingested_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(news_view(self.news)["status"], "Stale snapshot")
        self.assertEqual(configuration(self.env)["news"], "Not configured")
