from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from edgelab.contracts import fingerprint
from integrations.advanced_mcp.draft_service import DraftConflict, DraftService, DraftUnavailable, make_handler


TOKEN = "draft-fixture-token-A1b2c3D4e5F6g7H8"
PATH = "/v1/drafts"
DRAFT = {"template_id": "cointegration-fixture", "case": "shared-trend", "hypothesis": "The spread is stable."}
SOURCE = "integrations.advanced_mcp.draft_service.release_fingerprint"


class DraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.database = self.directory / "drafts.sqlite3"
        self.service = DraftService(self.database)

    def _request(self, draft=DRAFT):
        return {"request_id": str(uuid4()), "preview_sha256": self.service.preview(draft)["preview_sha256"],
                "draft": draft}

    def test_preview_normalizes_and_does_not_create_or_access_a_store(self):
        hypothesis = "  Cafe\u0301\t meets\n test.  "
        draft = {**DRAFT, "hypothesis": hypothesis}
        with patch("edgelab.stat_tools.synthetic", side_effect=AssertionError("No observation generation")):
            previews = [self.service.preview(draft), self.service.preview({**draft, "hypothesis": "Café meets test."})]
        self.assertEqual(previews[0], previews[1])
        payload = {key: previews[0][key] for key in ("schema_version", "draft", "binding")}
        self.assertEqual(previews[0]["preview_sha256"], fingerprint(payload))
        self.assertEqual(previews[0]["status"], "ready_for_review")
        self.assertEqual(previews[0]["scope"], "synthetic in-sample")
        self.assertEqual(len(previews[0]["method_gates"]), 3)
        self.assertFalse(self.database.exists())
        with self.assertRaises(ValueError):
            self.service.preview({**draft, "case": "holdout"})
        with self.assertRaises(ValueError):
            self.service.preview({**draft, "hypothesis": "x" * 1001})

    def test_exact_replay_restart_and_reconciliation_preserve_database_bytes(self):
        request = self._request()
        draft_id, digest = request["request_id"], request["preview_sha256"]
        with self.assertRaises(DraftUnavailable):
            self.service.receipt(request)
        self.assertFalse(self.database.exists())
        receipt = self.service.save(request)
        bytes_before = self.database.read_bytes()
        reloaded = DraftService(self.database)
        self.assertEqual(reloaded.save(request), receipt)
        self.assertEqual(reloaded.receipt(request), receipt)
        self.assertEqual(receipt["receipt_sha256"], fingerprint({k: v for k, v in receipt.items() if k != "receipt_sha256"}))
        self.assertEqual(reloaded.review(draft_id, digest)["status"], "ready_for_review")
        self.assertEqual(reloaded.review(draft_id, digest, handoff=True)["argv"],
                         ["python", "-B", "-m", "edgelab.capstone", "plan", "--case", "shared-trend"])
        changed = {**request, "draft": {**DRAFT, "hypothesis": "Different inputs."}}
        for method in (reloaded.save, reloaded.receipt):
            with self.assertRaises(DraftConflict):
                method(changed)
        with self.assertRaises(DraftUnavailable):
            reloaded.review(draft_id, "f" * 64)
        self.assertEqual(self.database.read_bytes(), bytes_before)

    def test_stale_preview_and_handoff_rejected_but_saved_receipt_remains_addressable(self):
        old_request = self._request()
        with patch(SOURCE, return_value="0" * 64):
            self.assertNotEqual(self.service.preview(DRAFT)["preview_sha256"], old_request["preview_sha256"])
            with self.assertRaises(DraftConflict):
                self.service.save(old_request)
            self.assertFalse(self.database.exists())
        saved = self.service.save(old_request)
        request_id, digest = old_request["request_id"], old_request["preview_sha256"]
        bytes_before = self.database.read_bytes()
        with patch(SOURCE, return_value="0" * 64):
            self.assertEqual(self.service.receipt(old_request), saved)
            self.assertEqual(self.service.save(old_request), saved)
            self.assertEqual(self.service.review(request_id, digest)["status"], "stale")
            with self.assertRaises(DraftConflict):
                self.service.review(request_id, digest, handoff=True)
            with self.assertRaises(DraftConflict):
                self.service.save({**old_request, "request_id": str(uuid4())})
        self.assertEqual(self.database.read_bytes(), bytes_before)

    def test_concurrent_identical_saves_create_one_receipt(self):
        request = self._request()
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(self.service.save, [request] * 20))
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM drafts").fetchone(), (1,))

    def test_foreign_sqlite_store_is_rejected_without_modification(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE edgelab_events (event_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO edgelab_events VALUES ('foreign')")
        original = self.database.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.save(self._request())
        self.assertEqual(self.database.read_bytes(), original)

    def test_tampered_receipt_never_returns_a_reconciliation_result(self):
        request = self._request()
        self.service.save(request)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE drafts SET receipt_json='{}'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.receipt(request)

    def test_loopback_routes_require_token_and_return_exact_saved_receipt(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, TOKEN)) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def request(method, path, payload=None, *, auth=True, headers=None):
                    fields = dict(headers or {})
                    if auth:
                        fields["Authorization"] = "Bearer " + TOKEN
                    if payload is not None:
                        fields["Content-Type"] = "application/json"
                        payload = json.dumps(payload).encode("utf-8")
                    with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as client:
                        client.request(method, path, body=payload, headers=fields)
                        response = client.getresponse()
                        return response.status, response.read()

                self.assertEqual(request("GET", "/healthz", auth=False)[0], 200)
                self.assertEqual(request("POST", f"{PATH}/preview", DRAFT, auth=False)[0], 401)
                self.assertEqual(request("POST", f"{PATH}/preview", DRAFT, headers={"Origin": "http://other.example"})[0], 403)
                self.assertEqual(request("POST", f"{PATH}/preview", {**DRAFT, "hypothesis": "X" * 9000})[0], 400)
                status, preview = request("POST", f"{PATH}/preview", DRAFT)
                self.assertEqual(status, 200)
                digest = json.loads(preview)["preview_sha256"]
                draft_id = str(uuid4())
                saved = {"request_id": draft_id, "preview_sha256": digest, "draft": DRAFT}
                self.assertEqual(request("POST", f"{PATH}/receipt", saved)[0], 404)
                self.assertFalse(self.database.exists())
                receipt = request("POST", PATH, saved)
                self.assertEqual(receipt[0], 200)
                self.assertEqual(request("POST", f"{PATH}/receipt", saved), receipt)
                self.assertEqual(request("POST", PATH, saved), receipt)
                self.assertEqual(request("GET", f"{PATH}/{draft_id}/readiness?preview_sha256={digest}")[0], 200)
                self.assertEqual(request("GET", f"{PATH}/{draft_id}/handoff?preview_sha256={'1' * 64}")[0], 404)
                self.assertEqual(request("PATCH", PATH, saved)[0], 405)
            finally:
                server.shutdown()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
