"""Serve explicitly pinned synthetic EdgeLab evidence over loopback HTTP."""

import argparse
from dataclasses import asdict, dataclass
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import isfinite
import os
from pathlib import Path
import re
import sqlite3

from edgelab.journal import Journal
from edgelab.messages import check_version, run_id, sha
from edgelab.providers import _parse_json
from edgelab.reporting import report_from_journal
from edgelab.stat_tools import CASES, SYNTHETIC_SEED, TOOL_SEQUENCE, window_for_demo


VERSION = 1
PORT = 8799
TOKEN_ENV = "EDGELAB_READER_SERVICE_TOKEN"
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,31}")
RULE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
VERDICTS = {"continue", "revise", "stop", "unresolved"}
SUMMARY_FIELDS = ("study_id", "run_id", "verdict", "ledger_sha256")
RUNS_ROUTE = "/v1/runs"


class StudyUnavailable(ValueError):
    """Use the same public denial for unknown and inaccessible studies."""


@dataclass(frozen=True, slots=True)
class StudyPin:
    run_id: str
    ledger_sha256: str


def _bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Adapter input exceeds its size limit")
    return raw


def load_manifest(path: Path) -> dict[str, StudyPin]:
    manifest = _parse_json(_bounded_read(path, 4096))
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "studies"}:
        raise ValueError("Invalid study manifest")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != VERSION:
        raise ValueError("Unsupported study manifest version")
    entries = manifest["studies"]
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 2:
        raise ValueError("The pilot must pin one or two synthetic studies")
    pins = {}
    for alias, entry in entries.items():
        if not ALIAS.fullmatch(alias) or not isinstance(entry, dict) or set(entry) != {"run_id", "ledger_sha256"}:
            raise ValueError("Invalid study pin")
        pins[alias] = StudyPin(run_id(entry["run_id"]), sha(entry["ledger_sha256"]))
    if len({pin.run_id for pin in pins.values()}) != len(pins):
        raise ValueError("Study aliases must refer to distinct runs")
    return pins


def validate_token(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) or len(set(token)) < 8:
        raise ValueError("Invalid dedicated adapter service token")
    return token


def _stat(details: dict, key: str, *, probability: bool = False) -> float | None:
    value = details.get(key)
    if value is None:
        return None
    if type(value) not in (int, float) or not isfinite(value) or (probability and not 0 <= value <= 1):
        raise ValueError("Invalid projected statistic")
    return float(value)


class ReportService:
    def __init__(self, journal_path: Path, manifest_path: Path):
        path = journal_path.resolve(strict=True)
        if not path.is_file():
            raise ValueError("Expected an existing synthetic journal")
        self._journal = Journal(path, read_only=True)
        self._pins = load_manifest(manifest_path)

    def report(self, study_id: str) -> dict:
        try:
            pin = self._pins[study_id]
            verified = report_from_journal(self._journal, pin.run_id)
            report, bundle = verified["report"], verified["bundle"]
            if (verified["run_id"] != pin.run_id or bundle["ledger_sha256"] != pin.ledger_sha256
                    or report["status"] != "finished" or report["verdict"] not in VERDICTS
                    or bundle["missing"] != ["holdout evaluation"]):
                raise ValueError("Study evidence or scope differs from its pin")
            return self._project(study_id, pin, report, bundle["events"])
        except (AttributeError, KeyError, OSError, OverflowError, RecursionError, sqlite3.Error, TypeError, ValueError):
            raise StudyUnavailable("Study unavailable") from None

    def list_runs(self) -> dict:
        runs = []
        for study_id in sorted(self._pins):
            try:
                report = self.report(study_id)
            except StudyUnavailable:
                continue
            runs.append({field: report[field] for field in SUMMARY_FIELDS})
        return {"schema_version": VERSION, "runs": runs}

    @staticmethod
    def _project(study_id, pin, report, events):
        by_key = {event["key"]: event for event in events}
        data = by_key["plan"]["value"]["input"]
        protocol = by_key["approval"]["value"]["protocol"]
        fixture_window = asdict(window_for_demo())
        case = data.get("case")
        check_version(protocol.get("version"))
        if (case not in CASES or data != {"case": case, "seed": SYNTHETIC_SEED, "window": fixture_window}
                or "history" in protocol or any(protocol.get(key) != data[key] for key in data)
                or report["window"] != fixture_window or report["scope"] != "in-sample research only"
                or report["holdout"] != "locked and untested"):
            raise ValueError("Only the approved fixed-seed synthetic fixtures are readable")
        reason = report["reason"]
        if not isinstance(reason, str) or not 1 <= len(reason) <= 320 or not reason.isprintable():
            raise ValueError("Invalid verdict rationale")

        details, evidence = {}, []
        for result_key, finding in zip(report["evidence_refs"], report["milestone_reviews"], strict=True):
            tool = result_key.removeprefix("result:")
            rule = finding["rule_id"]
            if tool not in TOOL_SEQUENCE or result_key != f"result:{tool}" or not RULE.fullmatch(rule):
                raise ValueError("Invalid reviewed evidence reference")
            details[tool] = finding["details"]
            for event_key in (result_key, f"review:{tool}"):
                evidence.append({"event_key": event_key, "sha256": sha(by_key[event_key]["sha256"]),
                                 "critic_rule": rule})
        evidence.append({"event_key": "verdict", "sha256": sha(by_key["verdict"]["sha256"]),
                         "critic_rule": None})
        eg, stability = details.get("engle_granger", {}), details.get("stability", {})
        return {"schema_version": VERSION, "study_id": study_id, "run_id": pin.run_id,
                "scope": "synthetic in-sample", "holdout": report["holdout"], "verdict": report["verdict"],
                "reason": reason, "window": fixture_window, "ledger_sha256": pin.ledger_sha256,
                "diagnostics": {"engle_granger_a_on_b_p": _stat(eg, "a_on_b", probability=True),
                                "engle_granger_b_on_a_p": _stat(eg, "b_on_a", probability=True),
                                "cusum_p": _stat(stability, "cusum_p", probability=True),
                                "ecm_loading": _stat(stability, "ecm_loading"),
                                "ou_half_life_observations": _stat(stability, "ou_half_life_observations")},
                "evidence": evidence}


def make_handler(service: ReportService, token: str):
    expected_authorization = f"Bearer {validate_token(token)}"

    class ReportHandler(BaseHTTPRequestHandler):
        timeout = 3

        def log_message(self, format, *args):
            pass

        def _reply(self, status, data):
            content = json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            if status == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(content)
                except (ConnectionError, TimeoutError):
                    pass

        def send_error(self, code, message=None, explain=None):
            self._reply(405 if code == 501 else code, {"error": "Invalid request"})

        def do_GET(self):
            port = self.server.server_port
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            if (len(hosts) != 1 or hosts[0] not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                    or origins not in ([], [f"http://{hosts[0]}"])):
                return self._reply(403, {"error": "Loopback request required"})
            authorization = self.headers.get_all("Authorization", [])
            if (len(authorization) != 1 or not authorization[0].isascii()
                    or not compare_digest(authorization[0], expected_authorization)):
                return self._reply(401, {"error": "Service authentication required"})
            target = self.requestline.split()[1]
            if self.headers.get_all("Content-Length", []) not in ([], ["0"]) or "Transfer-Encoding" in self.headers:
                return self._reply(400, {"error": "GET requests cannot include a body"})
            if target == RUNS_ROUTE:
                return self._reply(200, service.list_runs())
            alias = target.removeprefix(f"{RUNS_ROUTE}/")
            if alias != target and ALIAS.fullmatch(alias):
                try:
                    return self._reply(200, service.report(alias))
                except StudyUnavailable:
                    pass
            return self._reply(404, {"error": "Study unavailable"})

    return ReportHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--port", type=int, default=PORT)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--token-file", type=Path)
    auth.add_argument("--token-env", action="store_true", help=f"Use the explicit {TOKEN_ENV} process value")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Expected a valid loopback port")
    try:
        token = (os.environ.get(TOKEN_ENV, "") if args.token_env
                 else _bounded_read(args.token_file, 130).decode("ascii").rstrip("\r\n"))
        handler = make_handler(ReportService(args.journal, args.manifest), token)
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, RecursionError, UnicodeError, ValueError):
        parser.exit(2, "Could not start the loopback EdgeLab report service with the supplied inputs.\n")


if __name__ == "__main__":
    main()
