import json
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from edgelab.dashboard import handler


def scenario(n, cusum, revised, all_dates):
    return {"n": n, "arms": {arm: {"advanced": count} for arm, count in
                             zip(("cusum", "revised", "all_dates"), (cusum, revised, all_dates), strict=True)}}


class StabilitySnapshotTests(unittest.TestCase):
    def test_dashboard_projects_only_the_pinned_synthetic_snapshot(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        path = root / "results.json"
        raw = json.dumps({"holdout": "locked; not accessed", "runtime": {"internal": "not projected"},
                          "scenarios": {"stable_weak": scenario(240, 0, 180, 181),
                                        "weak_0.6_88": scenario(240, 0, 175, 177),
                                        "weak_2.4_88": scenario(240, 0, 160, 102)}}).encode()
        self.enterContext(patch("edgelab.stability_snapshot.SNAPSHOT_SHA256", sha256(raw).hexdigest()))
        route = "/api/synthetic-stability"
        request = self.server_request(root, path)

        self.assertEqual(request(route), (200, b"null"))
        path.write_bytes(raw)
        status, body = request(route)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["samples"], 720)
        self.assertEqual(data["holdout"], "locked; not accessed")
        self.assertEqual(data["scenarios"][1]["advanced"], {"cusum": 0, "revised": 175, "all_dates": 177})
        self.assertAlmostEqual(data["late_gain_pp"], 100 * 56 / 480)
        self.assertEqual(data["break_date_accuracy"], {"status": "missing"})
        self.assertNotIn("runtime", data)
        self.assertEqual(request(route + "?path=other.json")[0], 404)
        self.assertEqual(request("/.cache/bootstrap_all_dates_20261115/trials.csv")[0], 404)

        ledger = root / "trials.csv"
        rows = (b"scenario,stage,revised,all_dates,revised_date,all_date\n"
                b"weak_0.6_88,stability,break,break,465,486\n"
                b"weak_0.6_88,stability,break,break,464,485\n"
                b"weak_0.6_88,stability,clear,break,475,475\n"
                b"weak_0.6_88,stability,unresolved,clear,,475\n"
                b"weak_0.6_88,diagnostics,break,break,475,475\n"
                b"weak_2.4_88,stability,break,break,475,475\n"
                b"weak_2.4_88,stability,clear,break,475,476\n"
                b"weak_2.4_74,stability,break,break,475,475\n")
        self.enterContext(patch("edgelab.stability_snapshot.LEDGER_SHA256", sha256(rows).hexdigest()))
        ledger.write_bytes(rows)
        status, body = request(route)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["break_date_accuracy"], {
            "status": "available", "tolerance_observations": 10, "planted_split_percent": 88,
            "scenarios": [
                {"scenario": "weak_0.6_88", "eligible": 4, "within_tolerance": {"revised": 1, "all_dates": 2}},
                {"scenario": "weak_2.4_88", "eligible": 2, "within_tolerance": {"revised": 1, "all_dates": 2}},
            ],
        })
        ledger.write_bytes(rows + b"\n")
        status, body = request(route)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {**data, "break_date_accuracy": {"status": "unavailable"}})

        path.write_bytes(raw + b" ")
        self.assertEqual(request(route)[0], 503)
        self.assertEqual(self.server_request(root, path, demo=True)(route), (200, b"null"))
        self.assertFalse((root / "journal.sqlite").exists())

    def server_request(self, root, path, *, demo=False):
        server = self.enterContext(ThreadingHTTPServer(
            ("127.0.0.1", 0), handler((root / "journal.sqlite",), root / "news.json", root / ".env",
                                      demo, stability_path=path)))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        def request(route):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request("GET", route)
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()

        return request
