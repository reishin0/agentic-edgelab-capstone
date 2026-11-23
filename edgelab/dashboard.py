"""Loopback, read-only view of EdgeLab journals and isolated news cache."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import logging
from math import isfinite
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .contracts import fingerprint
from .cross_assets import load_snapshot
from .etf_entry_costs import load_etf_entry_costs
from .history import FrozenHistory
from .journal import Journal, JournalConflict
from .leaderboard import load_leaderboard
from .methods import BY_ID, CORPUS_SHA256, PineconeMethods, local_notes
from .model_settings import local_catalog, openrouter_catalog, test_model
from .news import LIMIT, MAX_BYTES, NEWS_CACHE, NEWS_PATH, ROOT, SYMBOL, article, cache, timestamp
from .planning import REQUIRED
from .providers import LMStudio, ProviderError, _parse_json, pinecone_url, read_env
from .research import validate_id
from .stability_snapshot import CACHE_FILE as STABILITY_CACHE_FILE, load_stability_snapshot
from .stat_tools import SYNTHETIC_SEED, Window, guarded_rows, sample_fingerprint, synthetic
from .waitlist import load_waitlist


JOURNAL_PATH = ROOT / "runtime" / "edgelab.sqlite"
LOG = logging.getLogger(__name__)
ASSETS = {"/": ("index.html", "text/html"), "/ui.css": ("ui.css", "text/css"),
          "/ui.js": ("ui.js", "text/javascript"), "/settings.js": ("settings.js", "text/javascript")}
SETTINGS_ROUTES = {"/api/settings/catalog", "/api/settings/test-model", "/api/settings/test-retrieval"}
ROUTES = ASSETS.keys() | {"/api/runs", "/api/run", "/api/news", "/api/waitlist",
                         "/api/cross-assets", "/api/leaderboard", "/api/synthetic-stability",
                         "/api/etf-entry-costs"} | SETTINGS_ROUTES
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
    result = results.get("engle_granger")
    if not isinstance(result, dict) or result.get("approval") != approval["digest"]:
        return series
    observation = result.get("observation")
    if not isinstance(observation, dict) or "error" in observation:
        return series
    config, payload = observation.get("config"), observation.get("result")
    if (not isinstance(config, dict) or config.get("sample_sha256") != approval["sample_sha256"]
            or not isinstance(payload, dict) or observation.get("result_sha256") != fingerprint(payload)):
        return series
    fit = payload.get("a_on_b")
    if not isinstance(fit, dict) or any(type(fit.get(name)) not in (float, int) for name in ("slope", "intercept")):
        return series
    try:
        spread = [row.a - fit["intercept"] - fit["slope"] * row.b for row in sample]
    except OverflowError:
        return series
    if all(map(isfinite, spread)):
        series["spread"] = spread
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


def configuration(env: Path, *, planner_env: Path | None = None) -> dict:
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
    status = {"lm_studio": lm, "pinecone": pine, "news": news}
    if planner_env is not None and planner_env.is_file():
        try:
            planner_settings = read_env(planner_env)
            if planner_settings.get("LM_STUDIO_AUTO_USE") == "1":
                model = LMStudio.from_settings(planner_settings).models["planner"]
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}", model):
                    status["default_planner"] = model
        except (OSError, ValueError):
            status["lm_studio"] = "Invalid configuration"
    return status


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


def provider_action(route: str, data: object, env: Path, demo: bool) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Expected a settings object")
    if route == "/api/settings/test-retrieval":
        backend = data.get("backend")
        if set(data) != {"backend"} or backend not in ("local", "pinecone"):
            raise ValueError("Choose an evidence source")
        if backend == "local":
            ids = {note.id for note in local_notes("cointegration")}
            missing = REQUIRED - ids
            result = {"corpus_version": "Bundled notes", "corpus_sha256": CORPUS_SHA256,
                      "required": {"status": "incomplete" if missing else "complete",
                                   "verified_ids": sorted(REQUIRED & ids), "missing_ids": sorted(missing)}}
        else:
            if demo:
                raise ProviderError("Cloud diagnostics are disabled in synthetic demo mode")
            settings = read_env(env) if env.is_file() else {}
            if not all(settings.get(key) for key in ("PINECONE_INDEX_HOST", "PINECONE_API_KEY")):
                raise ProviderError("Configure the Pinecone index host and key in providers.env")
            result = PineconeMethods(settings["PINECONE_INDEX_HOST"], settings["PINECONE_API_KEY"]).retrieval_diagnostic()
        result["citations"] = [{"id": name, "url": BY_ID[name].url, "locator": BY_ID[name].locator}
                               for name in result["required"]["verified_ids"]]
        return result

    catalog = route == "/api/settings/catalog"
    fields = {"provider", "free_only"} if catalog else {"provider", "free_only", "model_id"}
    provider, free_only = data.get("provider"), data.get("free_only")
    if (set(data) != fields or provider not in ("lm_studio", "openrouter") or type(free_only) is not bool
            or (not catalog and not isinstance(data["model_id"], str))):
        raise ValueError("Choose a provider and a model from its catalog")
    if demo and (provider != "lm_studio" or not catalog):
        raise ProviderError("Provider calls are disabled in synthetic demo mode")
    settings = read_env(env) if not demo and env.is_file() else {}
    if catalog and provider == "lm_studio":
        return local_catalog(settings, allow_provider_io=not demo)
    if catalog:
        return openrouter_catalog(settings, allow_provider_io=True, free_only=free_only)
    return test_model(settings, provider, data["model_id"], allow_provider_io=True, free_only=free_only)


def handler(journal_paths: tuple[Path, ...], news_path: Path, env: Path, demo: bool,
            histories: tuple[FrozenHistory, ...] = (),
            waitlist: dict | None = None, cross_assets: dict | None = None,
            leaderboard: dict | None = None, *, live_leaderboard: Path | None = None,
            stability_path: Path | None = None, etf_entry_costs_path: Path | None = None,
            provider_settings_path: Path | None = None):
    provider_settings_path = provider_settings_path or env.with_name("providers.env")
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

        def do_POST(self):
            if self.path not in SETTINGS_ROUTES:
                return self.json(501, {"error": "Only settings diagnostics accept POST"})
            port, host = self.server.server_port, self.headers.get("Host")
            if (host not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                    or self.headers.get("Origin") != f"http://{host}"):
                return self.json(403, {"error": "Open Settings from the local dashboard"})
            if self.headers.get_content_type() != "application/json":
                return self.json(415, {"error": "Expected a JSON settings request"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024:
                    return self.json(413, {"error": "Invalid settings request size"})
                data = _parse_json(self.rfile.read(size))
                return self.json(200, provider_action(self.path, data, provider_settings_path, demo))
            except ProviderError as error:
                return self.json(503, {"error": str(error)})
            except OSError:
                return self.json(503, {"error": "Local provider settings could not be read"})
            except (TypeError, ValueError, RecursionError):
                return self.json(400, {"error": "Invalid request or provider configuration"})

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
                              if demo else configuration(env, planner_env=provider_settings_path))
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
                if target.path == "/api/etf-entry-costs" and not target.query:
                    costs = load_etf_entry_costs(etf_entry_costs_path) if etf_entry_costs_path is not None and not demo else None
                    return self.json(200, costs)
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
    parser.add_argument("--etf-entry-costs", type=Path, help="Show the pinned November 23 ETF entry-cost result.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s EdgeLab %(message)s")
    if args.demo and (args.history_manifest or args.waitlist or args.cross_assets or args.leaderboard or args.etf_entry_costs):
        parser.error("Local historical data, waitlists, leaderboards, and ETF costs are not allowed in synthetic demo mode")
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
                                stability_path=ROOT / STABILITY_CACHE_FILE, etf_entry_costs_path=args.etf_entry_costs)
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
