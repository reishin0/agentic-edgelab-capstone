"""Prepare and store fixed-scope synthetic EdgeLab drafts over loopback HTTP."""

import argparse
from contextlib import closing
from dataclasses import asdict
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
from unicodedata import normalize
from uuid import UUID

from edgelab.contracts import fingerprint
from edgelab.messages import run_id, sha
from edgelab.methods import CORPUS_SHA256
from edgelab.providers import _parse_json
from edgelab.research import release_fingerprint
from edgelab.stat_tools import SYNTHETIC_SEED, window_for_demo

from .report_service import validate_token


VERSION = 1
PORT = 8801
TOKEN_ENV = "EDGE_DRAFT_SERVICE_TOKEN"
TEMPLATE = "cointegration-fixture"
CASES = ("shared-trend", "spread-level-shift")
MAX_BODY = 8192
MAX_HYPOTHESIS = 1000
APPLICATION_ID = 0x45444C44
BASE_ROUTE = "/v1/drafts"
READ_ROUTE = re.compile(r"/v1/drafts/([0-9a-f-]{36})/(readiness|handoff)\?preview_sha256=([0-9a-f]{64})")
METHOD_GATES = ("ADF/KPSS integration diagnostics", "Bidirectional Engle-Granger", "CUSUM stability")
MISSING_REQUIREMENTS = ("EdgeLab plan", "Exact run and protocol approval")
LEGACY_PREVIEW_KEYS = {"schema_version", "draft", "binding"}
PREVIEW_KEYS = LEGACY_PREVIEW_KEYS | {"status", "scope", "method_gates", "missing_requirements"}


class DraftConflict(ValueError):
    pass


class DraftUnavailable(ValueError):
    pass


def normalize_draft(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"template_id", "case", "hypothesis"}:
        raise ValueError("Expected a draft with template_id, case, and hypothesis")
    if value["template_id"] != TEMPLATE or value["case"] not in CASES:
        raise ValueError("Choose an eligible synthetic fixture")
    hypothesis = value["hypothesis"]
    if not isinstance(hypothesis, str) or not 1 <= len(hypothesis) <= MAX_HYPOTHESIS:
        raise ValueError("Hypothesis must be between 1 and 1000 characters")
    hypothesis = " ".join(normalize("NFC", hypothesis).split())
    if not 1 <= len(hypothesis) <= MAX_HYPOTHESIS or not hypothesis.isprintable():
        raise ValueError("Expected a nonempty printable hypothesis")
    return {"template_id": TEMPLATE, "case": value["case"], "hypothesis": hypothesis}


def _request_id(value: object) -> str:
    normalized = run_id(value)
    if UUID(normalized).version != 4:
        raise ValueError("Expected a canonical UUIDv4 request_id")
    return normalized


def _save_input(value: object) -> tuple[str, str, dict]:
    if not isinstance(value, dict) or set(value) != {"request_id", "preview_sha256", "draft"}:
        raise ValueError("Expected a request_id, preview_sha256, and draft")
    return _request_id(value["request_id"]), sha(value["preview_sha256"]), normalize_draft(value["draft"])


def _preview_payload(draft: dict, release_sha256: str) -> dict:
    fixture = {"case": draft["case"], "seed": SYNTHETIC_SEED, "window": asdict(window_for_demo())}
    return {"schema_version": VERSION, "draft": draft, "status": "ready_for_review",
            "scope": "synthetic in-sample", "method_gates": list(METHOD_GATES),
            "missing_requirements": list(MISSING_REQUIREMENTS),
            "binding": {"release_sha256": release_sha256, "corpus_sha256": CORPUS_SHA256,
                        "fixture_sha256": fingerprint(fixture)}}


def _receipt(request_id: str, preview_sha256: str) -> dict:
    receipt = {"schema_version": VERSION, "status": "saved", "request_id": request_id,
               "preview_sha256": preview_sha256}
    return {**receipt, "receipt_sha256": fingerprint(receipt)}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DraftService:
    def __init__(self, database: Path):
        self._path = database.resolve()
        if not self._path.parent.is_dir() or (self._path.exists() and not self._path.is_file()):
            raise ValueError("Choose a draft database in an existing local directory")
        self._release_sha256 = release_fingerprint()

    def _current_preview(self, draft: dict) -> dict:
        if release_fingerprint() != self._release_sha256:
            raise DraftConflict("EdgeLab source changed; restart the draft service and prepare a fresh preview")
        return _preview_payload(draft, self._release_sha256)

    def preview(self, value: object) -> dict:
        payload = self._current_preview(normalize_draft(value))
        return {**payload, "preview_sha256": fingerprint(payload)}

    @staticmethod
    def _schema(db: sqlite3.Connection, *, initialize: bool = False) -> None:
        app_id, = db.execute("PRAGMA application_id").fetchone()
        version, = db.execute("PRAGMA user_version").fetchone()
        objects = set(db.execute("SELECT type,name FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"))
        if (app_id, version, objects) == (APPLICATION_ID, VERSION, {("table", "drafts")}):
            return
        if not initialize or app_id or version or objects:
            raise sqlite3.DatabaseError("Unexpected draft database schema")
        db.execute("""CREATE TABLE drafts (
                       request_id TEXT PRIMARY KEY NOT NULL,
                       preview_sha256 TEXT NOT NULL,
                       preview_json TEXT NOT NULL,
                       receipt_json TEXT NOT NULL
                   ) WITHOUT ROWID""")
        db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        db.execute(f"PRAGMA user_version = {VERSION}")

    @staticmethod
    def _stored(db: sqlite3.Connection, request_id: str) -> tuple[str, dict, dict] | None:
        row = db.execute("SELECT preview_sha256,preview_json,receipt_json FROM drafts WHERE request_id=?",
                         (request_id,)).fetchone()
        if row is None:
            return None
        preview_sha256, preview_raw, receipt_raw = row
        try:
            preview = _parse_json(preview_raw)
            receipt = _parse_json(receipt_raw)
            # Legacy drafts remain readable for exact receipt reconciliation.
            if (set(preview) not in (LEGACY_PREVIEW_KEYS, PREVIEW_KEYS)
                    or type(preview["schema_version"]) is not int or preview["schema_version"] != VERSION
                    or preview["draft"] != normalize_draft(preview["draft"])
                    or set(preview["binding"]) != {"release_sha256", "corpus_sha256", "fixture_sha256"}
                    or any(sha(digest) != digest for digest in preview["binding"].values())
                    or fingerprint(preview) != preview_sha256 or _json(preview) != preview_raw
                    or receipt != _receipt(request_id, preview_sha256) or _json(receipt) != receipt_raw):
                raise ValueError
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise sqlite3.DatabaseError("Draft receipt integrity check failed") from None
        return preview_sha256, preview, receipt

    def _read(self, request_id: str) -> tuple[str, dict, dict]:
        if not self._path.is_file():
            raise DraftUnavailable("Draft unavailable")
        uri = self._path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
            self._schema(db)
            stored = self._stored(db, request_id)
        if stored is None:
            raise DraftUnavailable("Draft unavailable")
        return stored

    @staticmethod
    def _same_request(stored: tuple[str, dict, dict], preview_sha256: str, draft: dict) -> dict:
        stored_digest, preview, receipt = stored
        if stored_digest != preview_sha256 or preview["draft"] != draft:
            raise DraftConflict("Request ID already refers to different draft inputs")
        return receipt

    def save(self, value: object) -> dict:
        request_id, preview_sha256, draft = _save_input(value)
        if not self._path.exists() and fingerprint(self._current_preview(draft)) != preview_sha256:
            raise DraftConflict("Draft preview is stale or does not match its inputs")
        with closing(sqlite3.connect(self._path, timeout=3)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db, initialize=True)
            stored = self._stored(db, request_id)
            if stored is not None:
                return self._same_request(stored, preview_sha256, draft)
            preview = self._current_preview(draft)
            if fingerprint(preview) != preview_sha256:
                raise DraftConflict("Draft preview is stale or does not match its inputs")
            receipt = _receipt(request_id, preview_sha256)
            db.execute("INSERT INTO drafts VALUES (?,?,?,?)", (request_id, preview_sha256, _json(preview), _json(receipt)))
            return receipt

    def receipt(self, value: object) -> dict:
        request_id, preview_sha256, draft = _save_input(value)
        return self._same_request(self._read(request_id), preview_sha256, draft)

    def review(self, request_id: str, preview_sha256: str, *, handoff: bool = False) -> dict:
        request_id, preview_sha256 = _request_id(request_id), sha(preview_sha256)
        stored_digest, preview, _ = self._read(request_id)
        if stored_digest != preview_sha256:
            raise DraftUnavailable("Draft unavailable")
        case = preview["draft"]["case"]
        ready = (release_fingerprint() == self._release_sha256
                 and fingerprint(_preview_payload(preview["draft"], self._release_sha256)) == stored_digest)
        response = {"schema_version": VERSION, "request_id": request_id, "preview_sha256": preview_sha256,
                    "status": "ready_for_review" if ready else "stale"}
        if handoff:
            if not ready:
                raise DraftConflict("A fresh draft preview is required before handoff")
            response["argv"] = ["python", "-B", "-m", "edgelab.capstone", "plan", "--case", case]
        return response


def make_handler(service: DraftService, token: str):
    expected = f"Bearer {validate_token(token)}"

    class DraftHandler(BaseHTTPRequestHandler):
        timeout = 3

        def log_message(self, format, *args):
            pass

        def _reply(self, status: int, value: dict):
            content = _json(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            if status == 405:
                self.send_header("Allow", "GET, POST")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(content)
                except (ConnectionError, TimeoutError):
                    pass

        def send_error(self, code, message=None, explain=None):
            self._reply(405 if code == 501 else code, {"error": "Invalid request"})

        def _dispatch(self, target: str):
            port = self.server.server_port
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            if (len(hosts) != 1 or hosts[0] not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                    or origins not in ([], [f"http://{hosts[0]}"])):
                return self._reply(403, {"error": "Loopback request required"})
            if self.command == "GET" and target == "/healthz":
                return self._reply(200, {"status": "alive", "service": "edgelab-drafts"})
            authorization = self.headers.get_all("Authorization", [])
            if (len(authorization) != 1 or not authorization[0].isascii()
                    or not compare_digest(authorization[0], expected)):
                return self._reply(401, {"error": "Service authentication required"})
            try:
                if self.command == "GET":
                    if self.headers.get_all("Content-Length", []) not in ([], ["0"]) or "Transfer-Encoding" in self.headers:
                        raise ValueError("GET cannot include a request body")
                    match = READ_ROUTE.fullmatch(target)
                    if match is not None:
                        request_id, action, digest = match.groups()
                        return self._reply(200, service.review(request_id, digest, handoff=action == "handoff"))
                else:
                    actions = {BASE_ROUTE: service.save, f"{BASE_ROUTE}/preview": service.preview,
                               f"{BASE_ROUTE}/receipt": service.receipt}
                    if target in actions:
                        body = self._body()
                        return self._reply(200, actions[target](body))
            except DraftConflict as error:
                rejection = {"error": str(error)}
                if self.command == "POST" and target == BASE_ROUTE:
                    request_id, preview_sha256, _ = _save_input(body)
                    rejection.update(status="not_saved", request_id=request_id, preview_sha256=preview_sha256)
                return self._reply(409, rejection)
            except DraftUnavailable:
                pass
            except (OSError, sqlite3.Error):
                return self._reply(503, {"error": "Draft service unavailable"})
            except (RecursionError, TypeError, ValueError):
                return self._reply(400, {"error": "Invalid draft request"})
            return self._reply(404, {"error": "Draft unavailable"})

        def _body(self) -> object:
            headers = self.headers.get_all("Content-Length", [])
            content_type = self.headers.get_all("Content-Type", [])
            if (len(headers) != 1 or not re.fullmatch(r"[0-9]{1,4}", headers[0])
                    or not 1 <= int(headers[0]) <= MAX_BODY or "Transfer-Encoding" in self.headers
                    or len(content_type) != 1 or self.headers.get_content_type() != "application/json"):
                raise ValueError("Invalid JSON request headers")
            size = int(headers[0])
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("Incomplete JSON body")
            return _parse_json(body.decode("utf-8"))

        def do_GET(self):
            self._dispatch(self.requestline.split()[1])

        def do_POST(self):
            self._dispatch(self.requestline.split()[1])

    return DraftHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft-db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--token-env", action="store_true", required=True, help=f"Use the explicit {TOKEN_ENV} process value")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Expected a valid loopback port")
    try:
        handler = make_handler(DraftService(args.draft_db), os.environ.get(TOKEN_ENV, ""))
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError):
        parser.exit(2, "Could not start the loopback EdgeLab draft service with the supplied inputs.\n")


if __name__ == "__main__":
    main()
