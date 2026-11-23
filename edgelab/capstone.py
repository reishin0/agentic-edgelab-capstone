"""Opt-in Checkpoints 1.1-4.1 integration entry point."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .crew_roles import CrewRoles, advisory_report
from .escalation import escalation_for
from .graph import build_graph
from .history import FrozenHistory
from .journal import Journal
from .methods import CORPUS, CORPUS_SHA256, NAMESPACE, PineconeMethods, local_notes
from .model_settings import local_catalog, openrouter_adviser
from .providers import LMStudio, ProviderError, read_env
from .reporting import report_from_journal
from .research import ResearchService, validate_id
from .stat_tools import CASES


ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = ROOT / "runtime" / "edgelab.sqlite"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=JOURNAL_PATH,
                        help="Use a separate EdgeLab journal for an isolated deployment")
    parser.add_argument("--settings-file", type=Path, default=ROOT / ".env",
                        help="Provider configuration; only opened by explicit provider commands")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("corpus", help="Show the eight public notes and the exact review digest")
    seed = commands.add_parser("seed-pinecone", help="Upsert the reviewed public corpus into an existing index")
    seed.add_argument("--approve-corpus", required=True)
    seed.add_argument("--allow-provider-io", action="store_true")
    plan = commands.add_parser("plan", help="Plan without accessing series values")
    data = plan.add_mutually_exclusive_group()
    data.add_argument("--case", choices=CASES)
    data.add_argument("--history-manifest", type=Path, help="JSON manifest for a frozen in-sample CSV")
    plan.add_argument("--retrieval", choices=("local", "pinecone"), default="local")
    advisers = plan.add_mutually_exclusive_group()
    advisers.add_argument("--lm-studio", action="store_true")
    advisers.add_argument("--openrouter", action="store_true", help="Opt into OpenRouter Planner advice")
    plan.add_argument("--model-id", help="Exact loaded LM Studio or OpenRouter model ID")
    plan.add_argument("--allow-paid-model", action="store_true", help="Opt into paid OpenRouter routes")
    plan.add_argument("--model-critic", action="store_true",
                      help="Opt into an additional advisory model Critic; requires a separately qualified model")
    plan.add_argument("--allow-provider-io", action="store_true")
    plan.add_argument("--crewai", action="store_true", help="Run model planning roles as CrewAI tasks")
    for action in ("execute", "show", "report", "escalation"):
        entry = commands.add_parser(action)
        entry.add_argument("--run", required=True, type=validate_id)
        if action == "execute":
            entry.add_argument("--approve", required=True, help="Exact displayed protocol digest")
            entry.add_argument("--history-manifest", type=Path, help="The same manifest used to plan a historical run")
        elif action == "report":
            entry.add_argument("--crewai", action="store_true", help="Add advisory report presentation metadata")
            entry.add_argument("--allow-provider-io", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "corpus":
            result = {"namespace": NAMESPACE, "sha256": CORPUS_SHA256, "notes": [asdict(note) for note in CORPUS]}
        elif args.command == "seed-pinecone":
            if not args.allow_provider_io:
                parser.error("Pinecone access requires --allow-provider-io")
            settings = read_env(args.settings_file)
            pinecone = PineconeMethods(settings.get("PINECONE_INDEX_HOST", ""), settings.get("PINECONE_API_KEY", ""))
            result = {"namespace": NAMESPACE, "upserted": pinecone.seed(args.approve_corpus)}
        else:
            if args.command != "plan" and not args.journal.is_file():
                parser.error("No EdgeLab run journal exists")
            if args.command in ("show", "report", "escalation"):
                journal = Journal(args.journal, read_only=True)
                result = (report_from_journal(journal, args.run) if args.command != "show" else
                          {"run_id": args.run, "events": journal.events(args.run)})
                if args.command == "escalation":
                    result = {"run_id": args.run, "escalation": escalation_for(result)}
                if args.command == "report" and args.crewai:
                    if not args.allow_provider_io:
                        parser.error("CrewAI reporting requires --allow-provider-io")
                    model = LMStudio.from_settings(read_env(args.settings_file))
                    result = advisory_report(result, CrewRoles(model))
            else:
                retrieve, model = local_notes, None
                use_model_critic, adviser_provider = False, None
                if args.command == "plan":
                    if args.crewai and not args.lm_studio:
                        parser.error("--crewai planning requires --lm-studio")
                    if args.model_critic and not args.lm_studio:
                        parser.error("--model-critic requires --lm-studio")
                    if args.model_id is not None and not (args.lm_studio or args.openrouter):
                        parser.error("--model-id requires --lm-studio or --openrouter")
                    if args.openrouter and not args.model_id:
                        parser.error("--openrouter requires an exact --model-id")
                    if args.allow_paid_model and not args.openrouter:
                        parser.error("--allow-paid-model requires --openrouter")
                    use_model_critic = args.model_critic
                    if args.lm_studio or args.openrouter or args.retrieval == "pinecone":
                        if not args.allow_provider_io:
                            parser.error("Provider access requires --allow-provider-io")
                        settings = read_env(args.settings_file) if args.settings_file.is_file() else {}
                        if args.retrieval == "pinecone":
                            retrieve = PineconeMethods(
                                settings.get("PINECONE_INDEX_HOST", ""), settings.get("PINECONE_API_KEY", ""),
                            ).retrieve
                        if args.lm_studio:
                            if args.model_id is not None:
                                catalog = local_catalog(settings, allow_provider_io=True)
                                if args.model_id not in catalog["loaded_ids"]:
                                    parser.error("Load the selected LM Studio model before planning")
                                settings["LM_STUDIO_PLANNER_MODEL"] = args.model_id
                            model = LMStudio.from_settings(settings)
                            adviser_provider = "lm_studio"
                            if args.crewai:
                                model = CrewRoles(model)
                        if args.openrouter:
                            model = openrouter_adviser(settings, args.model_id, allow_provider_io=True,
                                                       free_only=not args.allow_paid_model)
                            adviser_provider = "openrouter"
                    case = None if args.history_manifest else args.case or CASES[0]
                    state = {"mode": "plan", "run_id": str(uuid4()), "case": case, "backend": args.retrieval}
                else:
                    state = {"mode": "execute", "run_id": args.run, "digest": args.approve}
                history = FrozenHistory.open(args.history_manifest) if args.history_manifest else None
                service = ResearchService(Journal(args.journal), history=history)
                graph = build_graph(service, retrieve, model, use_model_critic=use_model_critic,
                                    adviser_provider=adviser_provider)
                from langsmith import tracing_context

                with tracing_context(enabled=False):
                    result = graph.invoke(state)["output"]
    except ModuleNotFoundError as error:
        if error.name == "langgraph":
            parser.exit(2, "LangGraph is not installed; install the research extra in an isolated environment.\n")
        raise
    except (OSError, ProviderError, RuntimeError, ValueError, sqlite3.Error) as error:
        parser.exit(2, f"EdgeLab: {error}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
