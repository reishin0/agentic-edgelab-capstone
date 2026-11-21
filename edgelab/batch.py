"""Plan and execute a bounded batch of explicitly approved EdgeLab runs."""

import argparse
import json
from pathlib import Path
import sqlite3
from uuid import NAMESPACE_URL, UUID, uuid5

from .contracts import fingerprint
from .graph import build_graph
from .history import FrozenHistory
from .journal import Journal, JournalConflict
from .methods import local_notes
from .reporting import report_from_journal
from .research import ResearchService, release_fingerprint, validate_id
from .stat_tools import CASES, numerical_runtime


DEFAULT_JOURNAL = Path(__file__).resolve().parent.parent / "runtime" / "pair-batches.sqlite"
MAX_RUNS = 12


def _graph(journal, history):
    return build_graph(ResearchService(journal, history=history), local_notes, use_model_critic=False)


def _invoke(journal, history, **state):
    from langsmith import tracing_context

    with tracing_context(enabled=False):
        return _graph(journal, history).invoke(state)["output"]


def _input_map(histories):
    inputs = {history.manifest_sha256: history for history in histories}
    if len(inputs) != len(histories) or len({history.data_sha256 for history in histories}) != len(histories):
        raise ValueError("Duplicate frozen manifests or samples are not allowed in a batch")
    return inputs


def _load(journal, batch_id):
    plan = journal.get(validate_id(batch_id), "batch:plan")
    if not plan or plan.get("batch_id") != batch_id or plan.get("schema") != 1:
        raise JournalConflict("No matching EdgeLab batch plan")
    return plan


def _run_plan(journal, entry):
    plan = journal.get(entry["run_id"], "plan")
    if plan is None or fingerprint(plan) != entry["plan_sha256"]:
        raise JournalConflict("A batch member no longer matches its saved plan")
    return plan


def plan_batch(journal, *, histories=(), cases=(), lag_policy="BIC"):
    count = len(histories) + len(cases)
    if (not 1 <= count <= MAX_RUNS or (histories and cases) or lag_policy not in {"AIC", "BIC"}
            or len(set(cases)) != len(cases) or any(case not in CASES for case in cases)):
        raise ValueError(f"Select 1 to {MAX_RUNS} distinct frozen samples or synthetic fixtures and one lag policy")
    inputs = _input_map(histories)
    keys = sorted(inputs or cases)
    spec = {"schema": 1, "lag_policy": lag_policy, "runtime": numerical_runtime(),
            "source_sha256": release_fingerprint(), "inputs": keys, "mode": "history" if histories else "synthetic"}
    batch_id = str(uuid5(NAMESPACE_URL, "edgelab/batch/" + fingerprint(spec)))
    if journal.get(batch_id, "batch:plan") is not None:
        return show_batch(journal, batch_id)

    runs = []
    for key in keys:
        run_id = str(uuid5(UUID(batch_id), key))
        history = inputs.get(key)
        case = None if history else key
        plan = journal.get(run_id, "plan")
        if plan is None:
            plan = _invoke(journal, history, mode="plan", run_id=run_id, case=case, backend="local")
        candidates = [item for item in plan["candidates"] if item["protocol"]["lag_policy"] == lag_policy]
        if plan["status"] != "awaiting_approval" or len(candidates) != 1:
            raise JournalConflict("A batch member has no unique candidate for the requested lag policy")
        protocol = candidates[0]["protocol"]
        if (protocol["source_sha256"] != spec["source_sha256"]
                or protocol.get("history") != (history.identity() if history else None)
                or protocol.get("case") != case or fingerprint(protocol) != candidates[0]["digest"]):
            raise JournalConflict("A batch member does not match the frozen inputs or source")
        runs.append({"run_id": run_id, "input": key, "plan_sha256": fingerprint(plan),
                     "protocol_sha256": candidates[0]["digest"]})
    journal.append(batch_id, "batch:plan", {**spec, "batch_id": batch_id, "runs": runs})
    return show_batch(journal, batch_id)


def show_batch(journal, batch_id):
    plan = _load(journal, batch_id)
    digest = fingerprint(plan)
    claim = journal.get(batch_id, "batch:approval")
    if claim is not None and claim != {"batch_sha256": digest}:
        raise JournalConflict("Stored batch approval does not match the saved plan")
    blocked = journal.get(batch_id, "batch:blocked")
    results = []
    for entry in plan["runs"]:
        run_id = entry["run_id"]
        member = _run_plan(journal, entry)
        saved = report_from_journal(journal, run_id)
        report = saved["report"]
        approval = journal.get(run_id, "approval")
        if approval is not None and (claim is None or approval["digest"] != entry["protocol_sha256"]):
            raise JournalConflict("Batch member approval does not match the authorized protocol")
        results.append({**entry, "label": member["input"].get("case") or member["input"]["history"]["source"],
                        **{key: report[key] for key in ("status", "verdict", "reason", "holdout", "window")},
                        "ledger_sha256": saved["bundle"]["ledger_sha256"]})
    if blocked:
        status = "blocked"
    elif claim is None:
        status = "awaiting_approval"
    else:
        status = "finished" if all(run["status"] == "finished" for run in results) else "incomplete"
    return {"batch_id": batch_id, "batch_sha256": digest, "status": status, "lag_policy": plan["lag_policy"],
            "runtime": plan["runtime"], "source_sha256": plan["source_sha256"], "blocked": blocked, "runs": results}


def execute_batch(journal, batch_id, digest, *, histories=()):
    plan = _load(journal, batch_id)
    if digest != fingerprint(plan):
        raise JournalConflict("Execution requires the exact displayed batch SHA-256")
    if journal.get(batch_id, "batch:approval") is not None:
        return show_batch(journal, batch_id)
    if plan["source_sha256"] != release_fingerprint() or plan["runtime"] != numerical_runtime():
        raise JournalConflict("Batch source or numerical runtime changed; prepare a fresh plan")
    inputs = _input_map(histories)
    expected = set(plan["inputs"]) if plan["mode"] == "history" else set()
    if set(inputs) != expected:
        raise JournalConflict("Supply exactly the frozen manifests used to plan this batch")

    # Verify all sample files before claiming the batch or calling any statistic.
    for entry in plan["runs"]:
        member = _run_plan(journal, entry)
        history = inputs.get(entry["input"])
        if history:
            if history.identity() != member["input"]["history"]:
                raise JournalConflict("A historical input changed after planning")
            history.sample()
        if [event["key"] for event in journal.events(entry["run_id"])] != ["plan"]:
            if journal.get(batch_id, "batch:approval") is not None:
                return show_batch(journal, batch_id)
            raise JournalConflict("A batch member has already been used outside this batch")
    if not journal.append(batch_id, "batch:approval", {"batch_sha256": digest}):
        return show_batch(journal, batch_id)
    for entry in plan["runs"]:
        run_id = entry["run_id"]
        try:
            _invoke(journal, inputs.get(entry["input"]), mode="execute", run_id=run_id,
                    digest=entry["protocol_sha256"])
            if report_from_journal(journal, run_id)["report"]["status"] != "finished":
                raise JournalConflict("Batch member did not finish")
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            journal.append(batch_id, "batch:blocked", {"run_id": run_id, "error": type(error).__name__})
            break
    return show_batch(journal, batch_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    selection = planning.add_mutually_exclusive_group(required=True)
    selection.add_argument("--history-manifest", action="append", type=Path)
    selection.add_argument("--case", action="append", choices=CASES)
    planning.add_argument("--lag-policy", choices=("AIC", "BIC"), default="BIC")
    execution = commands.add_parser("execute")
    execution.add_argument("--batch", required=True, type=validate_id)
    execution.add_argument("--approve", required=True)
    execution.add_argument("--history-manifest", action="append", type=Path)
    report = commands.add_parser("report")
    report.add_argument("--batch", required=True, type=validate_id)
    report.add_argument("--run", type=validate_id, help="Export the audited report for one batch member")
    args = parser.parse_args()
    try:
        if args.command != "plan" and not args.journal.is_file():
            raise ValueError("Batch journal does not exist")
        paths = getattr(args, "history_manifest", None) or []
        if len(paths) > MAX_RUNS:
            raise ValueError(f"At most {MAX_RUNS} manifests may be supplied")
        histories = tuple(map(FrozenHistory.open, paths))
        journal = Journal(args.journal, read_only=args.command == "report")
        if args.command == "plan":
            result = plan_batch(journal, histories=histories, cases=tuple(args.case or ()), lag_policy=args.lag_policy)
        elif args.command == "execute":
            result = execute_batch(journal, args.batch, args.approve, histories=histories)
        else:
            result = show_batch(journal, args.batch)
            if args.run:
                if not any(run["run_id"] == args.run for run in result["runs"]):
                    raise ValueError("Run is not a member of the named batch")
                result = report_from_journal(journal, args.run)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, ImportError) as error:
        parser.exit(2, f"EdgeLab batch: {error}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    if args.command == "execute" and result["status"] != "finished":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
