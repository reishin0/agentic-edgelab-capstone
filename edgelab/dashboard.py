"""Loopback, read-only view of EdgeLab journals and isolated news cache."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import logging
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .contracts import fingerprint
from .cross_assets import load_snapshot
from .history import FrozenHistory
from .journal import Journal, JournalConflict
from .leaderboard import load_leaderboard
from .news import LIMIT, MAX_BYTES, NEWS_CACHE, NEWS_PATH, ROOT, SYMBOL, article, cache, timestamp
from .providers import LMStudio, pinecone_url, read_env
from .research import validate_id
from .stability_snapshot import CACHE_FILE as STABILITY_CACHE_FILE, load_stability_snapshot
from .stat_tools import SYNTHETIC_SEED, Window, guarded_rows, sample_fingerprint, synthetic
from .waitlist import load_waitlist


JOURNAL_PATH = ROOT / "runtime" / "edgelab.sqlite"
LOG = logging.getLogger(__name__)
ASSETS = {"/": ("index.html", "text/html"), "/ui.css": ("ui.css", "text/css"),
          "/ui.js": ("ui.js", "text/javascript")}
ROUTES = ASSETS.keys() | {"/api/runs", "/api/run", "/api/news", "/api/waitlist",
                         "/api/cross-assets", "/api/leaderboard", "/api/synthetic-stability"}
_SYMBOL = r"(?:[A-Z]{3}/[A-Z]{3}|[A-Z0-9.-]+)"
_PAIR = re.compile(rf"; A=({_SYMBOL}), B=({_SYMBOL});")


def historical_symbols(history: dict) -> list[str] | None:
    match = _PAIR.search(history["source"])
    return list(match.groups()) if match else None


def recent_runs(journal: Journal) -> list[dict]:
    if not journal.path.is_file():
        return []
    with journal.connect() as db:
        rows = db.execute("""
            SELECT run_id, body, body_hash FROM edgelab_events
            WHERE event_key = 'plan' ORDER BY seq DESC LIMIT 30
        """).fetchall()
    runs = []
    for run_id, body, digest in rows:
        value = Journal._decode((body, digest))
        data = value["input"]
        history = data.get("history")
        label = data.get("case") or history["classification"] + " history"
        entry = {"id": validate_id(run_id), "label": label}
        if history and (symbols := historical_symbols(history)):
            entry["symbols"] = symbols
        runs.append(entry)
    return runs


def sample_chart(approval: dict, results: dict, history: FrozenHistory | None = None) -> dict | None:
    protocol = approval.get("protocol", {})
    if fingerprint(protocol) != approval.get("digest"):
        return None
    if "history" in protocol:
        if history is None or history.identity() != protocol["history"]:
            return None
        try:
            sample = history.sample()
        except (OSError, ValueError):
            return None
        source = history.source
    else:
        if not protocol.get("case") or protocol.get("seed") != SYNTHETIC_SEED:
            return None
        sample = guarded_rows(synthetic(protocol["case"]), Window(**protocol["window"]))
        source = "Synthetic in-sample observations"
    if sample_fingerprint(sample) != approval.get("sample_sha256"):
        return None
    series = {"days": [row.day for row in sample], "a": [row.a for row in sample], "b": [row.b for row in sample],
              "source": source}
    result = results.get("engle_granger", {})
    observation = result.get("observation", {})
    fit = observation.get("result", {}).get("a_on_b", {})
    if (result.get("approval") == approval["digest"]
            and observation.get("config", {}).get("sample_sha256") == approval["sample_sha256"]
            and isinstance(observation.get("result"), dict)
            and observation.get("result_sha256") == fingerprint(observation["result"])
            and all(type(fit.get(name)) in (float, int) for name in ("slope", "intercept"))):
        series["spread"] = [row.a - fit["intercept"] - fit["slope"] * row.b for row in sample]
    return series


def run_view(journal: Journal, run_id: str, history: FrozenHistory | None = None, *,
             history_by_hash: dict[str, FrozenHistory] | None = None) -> dict:
    events = journal.events(validate_id(run_id))
    by_key = {event["key"]: event["value"] for event in events}
    plan = by_key.get("plan")
    if not plan:
        raise KeyError("Run not found")
    approval, verdict = by_key.get("approval"), by_key.get("verdict")
    results = {key[7:]: value for key, value in by_key.items() if key.startswith("result:")}
    reviews = {key[7:]: value for key, value in by_key.items() if key.startswith("review:")}
    historical_input = plan["input"].get("history")
    if history_by_hash is not None and historical_input:
        history = history_by_hash.get(historical_input["manifest_sha256"])
    reserved = [key[9:] for key in by_key if key.startswith("reserved:")]
    status = ("finished" if verdict else "reconciliation required" if set(reserved) - results.keys()
              else "approved" if approval else plan["status"])
    return {"plan": plan, "approval": approval, "verdict": verdict, "status": status,
            "timeline": [{"key": event["key"], "sha256": event["sha256"]} for event in events],
            "results": results, "reviews": reviews,
            "symbols": historical_symbols(historical_input) if historical_input else None,
            "chart": sample_chart(approval, results, history) if approval and results else None}


def configuration(env: Path) -> dict:
    try:
        settings = read_env(env) if env.is_file() else {}
    except (ValueError, OSError):
        return dict.fromkeys(("lm_studio", "pinecone", "news"), "Configuration unreadable")
    lm, pine = "Not configured", "Not configured"
    try:
        if settings.get("LM_STUDIO_PLANNER_MODEL"):
            LMStudio.from_settings(settings)
            lm = "Configured"
    except ValueError:
        lm = "Invalid configuration"
    try:
        if settings.get("PINECONE_INDEX_HOST"):
            pinecone_url(settings["PINECONE_INDEX_HOST"])
            if settings.get("PINECONE_API_KEY"):
                pine = "Configured"
    except ValueError:
        pine = "Invalid configuration"
    news = "Configured" if all(settings.get(key) for key in ("EDGELAB_NEWS_KEY", "EDGELAB_NEWS_SECRET")) else "Not configured"
    return {"lm_studio": lm, "pinecone": pine, "news": news}


def news_view(path: Path, *, demo: bool = False) -> dict:
    if not path.is_file():
        return {"status": "No news ingested", "articles": []}
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        data = json.loads(raw)
        symbols, articles = data["symbols"], data["articles"]
        if (data.get("origin") != ("synthetic" if demo else "alpaca")
                or not isinstance(symbols, list) or not 1 <= len(symbols) <= 8
                or not all(isinstance(s, str) and SYMBOL.fullmatch(s) for s in symbols)
                or not isinstance(articles, list) or len(articles) > LIMIT):
            raise ValueError
        ingested = timestamp(data["ingested_at"])
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ingested)).total_seconds()
        status = "Synthetic fixture" if demo else "Stale snapshot" if age > 600 or age < 0 else "Latest snapshot"
        return {"status": status, "origin": data["origin"],
                "ingested_at": ingested, "symbols": symbols, "articles": [article(row) for row in articles]}
    except (OSError, AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return {"status": "News cache unreadable", "articles": []}


def handler(journal_paths: tuple[Path, ...], news_path: Path, env: Path, demo: bool,
            histories: tuple[FrozenHistory, ...] = (),
            waitlist: dict | None = None, cross_assets: dict | None = None,
            leaderboard: dict | None = None, *, live_leaderboard: Path | None = None,
            stability_path: Path | None = None):
    paths = tuple(path.resolve() for path in journal_paths)
    history_by_hash = {history.manifest_sha256: history for history in histories}
    if len(paths) != len(set(paths)) or len(histories) != len(history_by_hash):
        raise ValueError("Do not configure duplicate journals or history manifests")
    journals = tuple(Journal(path, read_only=True) for path in paths)

    class DashboardHandler(BaseHTTPRequestHandler):
        timeout = 3

        def log_message(self, format, *args):
            pass

        def log_request(self, code="-", size="-"):
            route = self.path.partition("?")[0]
            LOG.info("request route=%s status=%s", route if route in ROUTES else "<unknown>", code)

        def reply(self, status, media, content):
            self.send_response(status)
            self.send_header("Content-Type", media + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
                             "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            try:
                self.end_headers()
                self.wfile.write(content)
            except (ConnectionError, TimeoutError):
                # A browser can cancel its request while switching runs or closing.
                pass

        def json(self, status, data):
            self.reply(status, "application/json", json.dumps(data, allow_nan=False).encode("utf-8"))

        def do_GET(self):
            port = self.server.server_port
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                return self.json(403, {"error": "Loopback host required"})
            try:
                target = urlsplit(self.path)
                if not target.query and target.path in ASSETS:
                    name, media = ASSETS[target.path]
                    return self.reply(200, media, files("edgelab").joinpath("web", name).read_bytes())
                if target.path == "/api/runs":
                    config = (dict.fromkeys(("lm_studio", "pinecone", "news"), "Disabled in demo")
                              if demo else configuration(env))
                    runs = [run for journal in journals for run in recent_runs(journal)]
                    if len({run["id"] for run in runs}) != len(runs):
                        raise JournalConflict("Run ID occurs in multiple journals")
                    return self.json(200, {"runs": runs, "configuration": config, "demo": demo})
                if target.path == "/api/run":
                    try:
                        run_id, = parse_qs(target.query, max_num_fields=1).get("id", [])
                        validate_id(run_id)
                    except ValueError:
                        return self.json(400, {"error": "Invalid run ID"})
                    owners = [journal for journal in journals if journal.path.is_file()
                              and journal.get(run_id, "plan") is not None]
                    if not owners:
                        return self.json(404, {"error": "Run not found"})
                    if len(owners) != 1:
                        raise JournalConflict("Run ID occurs in multiple journals")
                    return self.json(200, run_view(owners[0], run_id, history_by_hash=history_by_hash))
                if target.path == "/api/news":
                    return self.json(200, news_view(news_path, demo=demo))
                if target.path == "/api/waitlist":
                    return self.json(200, waitlist if not demo else None)
                if target.path == "/api/cross-assets":
                    return self.json(200, cross_assets if not demo else None)
                if target.path == "/api/leaderboard":
                    if live_leaderboard is not None and not demo:
                        return self.json(200, load_leaderboard(live_leaderboard))
                    return self.json(200, leaderboard if not demo else None)
                if target.path == "/api/synthetic-stability" and not target.query:
                    snapshot = load_stability_snapshot(stability_path) if stability_path is not None and not demo else None
                    return self.json(200, snapshot)
                return self.json(404, {"error": "Not found"})
            except KeyError as error:
                if error.args == ("Run not found",):
                    return self.json(404, {"error": "Run not found"})
                return self.json(503, {"error": "Local evidence could not be read"})
            except (OSError, sqlite3.Error, JournalConflict, TypeError, ValueError) as error:
                LOG.warning("local evidence read failed: %s", type(error).__name__)
                return self.json(503, {"error": "Local evidence could not be read"})

    return DashboardHandler


def demo_runs(path: Path) -> None:
    from langsmith import tracing_context

    from .graph import build_graph
    from .methods import local_notes
    from .research import ResearchService

    graph = build_graph(ResearchService(Journal(path)), local_notes, use_model_critic=False)
    with tracing_context(enabled=False):
        for case in ("independent", "structural-break", "spread-level-shift", "shared-trend"):
            run = str(uuid4())
            plan = graph.invoke({"mode": "plan", "run_id": run, "case": case, "backend": "local"})["output"]
            graph.invoke({"mode": "execute", "run_id": run, "digest": plan["candidates"][0]["digest"]})


def seed_demo_news(path: Path) -> None:
    fixture = files("edgelab").joinpath("fixtures", "market-news.synthetic.json")
    cache(path, json.loads(fixture.read_text(encoding="utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8793)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--journal", type=Path, action="append",
                        help="Read this journal instead of the default; repeat to add journals")
    source.add_argument("--demo", action="store_true", help="Run four isolated CPU-only synthetic cases")
    parser.add_argument("--history-manifest", type=Path, action="append", default=[],
                        help="Opt in to plotting an exact approved sample; repeat to add samples")
    parser.add_argument("--waitlist", type=Path, help="Show a pinned local IWB waitlist JSON")
    parser.add_argument("--cross-assets", type=Path, help="Show pinned local FX and crypto candidates")
    parser.add_argument("--leaderboard", type=Path, help="Show a pinned local pair screening snapshot")
    parser.add_argument("--live-leaderboard", action="store_true", help="Reload the local snapshot on each request")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s EdgeLab %(message)s")
    if args.demo and (args.history_manifest or args.waitlist or args.cross_assets or args.leaderboard):
        parser.error("Local historical data, waitlists, and leaderboards are not allowed in synthetic demo mode")
    if args.live_leaderboard and not args.leaderboard:
        parser.error("--live-leaderboard requires --leaderboard")
    try:
        if args.journal and any(not path.is_file() for path in args.journal):
            raise ValueError("Each explicitly supplied journal must be an existing file")
        histories = tuple(FrozenHistory.open(path) for path in args.history_manifest)
        waitlist = load_waitlist(args.waitlist) if args.waitlist else None
        cross_assets = load_snapshot(args.cross_assets) if args.cross_assets else None
        leaderboard = load_leaderboard(args.leaderboard) if args.leaderboard else None
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, f"Dashboard: {error}\n")
    with TemporaryDirectory(prefix="edgelab-") as directory:
        paths = tuple(args.journal or (JOURNAL_PATH,))
        news_path = NEWS_PATH
        if args.demo:
            paths = (Path(directory) / "demo.sqlite",)
            news_path = Path(directory) / NEWS_CACHE
            demo_runs(paths[0])
            seed_demo_news(news_path)
        try:
            dashboard = handler(paths, news_path, ROOT / ".env", args.demo, histories, waitlist, cross_assets,
                                leaderboard, live_leaderboard=args.leaderboard if args.live_leaderboard else None,
                                stability_path=ROOT / STABILITY_CACHE_FILE)
        except ValueError as error:
            parser.exit(2, f"Dashboard: {error}\n")
        with ThreadingHTTPServer(("127.0.0.1", args.port), dashboard) as server:
            LOG.info("dashboard listening at http://127.0.0.1:%d/ mode=%s journals=%d histories=%d leaderboard=%s",
                     server.server_port, "synthetic" if args.demo else "read-only",
                     len(paths), len(histories), "loaded" if leaderboard is not None else "not configured")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                LOG.info("dashboard stopped by operator")


if __name__ == "__main__":
    main()
