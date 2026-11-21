"""Controller-owned research service and durable, guarded statistical dispatch."""

from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from .contracts import fingerprint
from .history import FrozenHistory
from .journal import Journal, JournalConflict
from .messages import VERSION, ToolBinding, check_version, run_id as validate_id, sha
from .methods import BY_ID, CORPUS_SHA256
from .planning import BEAM, LEVELS, REQUIRED, score_decisions
from .stat_tools import (
    ALPHA, CASES, SYNTHETIC_SEED, TOOL_SEQUENCE, Point, Window, compute, guarded_rows, numerical_runtime,
    assess, check_window, sample_fingerprint, synthetic, window_for_demo,
)


def release_fingerprint() -> str:
    sources = sorted(Path(__file__).parent.glob("*.py"))
    return fingerprint({source.name: sha256(source.read_bytes()).hexdigest() for source in sources})


class ResearchService:
    def __init__(self, journal: Journal, *, numerical_tool: Callable = compute,
                 runtime_identity: Callable[[], dict[str, str]] = numerical_runtime,
                 history: FrozenHistory | None = None) -> None:
        self.journal = journal
        self._numerical_tool = numerical_tool
        self._runtime_identity = runtime_identity
        self._history = history
        self._release = release_fingerprint()

    def check_plan_scope(self, case: str | None, backend: str) -> None:
        if (backend not in {"local", "pinecone"} or (self._history is None and case not in CASES)
                or (self._history is not None and case is not None)):
            raise ValueError("Select one valid synthetic or historical input and a retrieval backend")

    def record_plan(self, run_id: str, case: str | None, plan: dict, backend: str) -> dict:
        validate_id(run_id)
        self.check_plan_scope(case, backend)
        data = (
            {"history": self._history.identity(), "window": asdict(self._history.window)}
            if self._history else
            {"case": case, "seed": SYNTHETIC_SEED, "window": asdict(window_for_demo())}
        )
        proposals = plan["candidates"]
        if (len(proposals) > BEAM
                or plan["status"] != ("awaiting_approval" if proposals else "unresolved")):
            raise JournalConflict("Plan status or candidate count is inconsistent")
        candidates, seen = [], set()
        evidence = plan["method_evidence"]
        ids = {note["id"] for note in evidence if note["id"] in BY_ID and asdict(BY_ID[note["id"]]) == note}
        if len(ids) != len(evidence):
            raise JournalConflict("Method evidence is altered, unknown, or duplicated")
        retained = {branch["id"]: branch for branch in plan["branches"] if branch["retained"] is True}
        for candidate in proposals:
            decisions = tuple(candidate["decisions"])
            score = score_decisions(decisions)
            if (len(decisions) != len(LEVELS) or score is None or score < 7
                    or candidate["score"] != score or not REQUIRED <= ids):
                raise JournalConflict("Candidate failed controller method or scope gates")
            leaf_id = "/".join(decisions)
            if leaf_id in seen or candidate["id"] != leaf_id:
                raise JournalConflict("Candidate identity is invalid or duplicated")
            seen.add(leaf_id)
            parent = "root"
            for depth in range(1, len(decisions) + 1):
                prefix = decisions[:depth]
                branch_id = "/".join(prefix)
                branch = retained.get(branch_id)
                if (branch is None or branch["parent_id"] != parent or branch["depth"] != depth
                        or tuple(branch["decisions"]) != prefix or branch["score"] != score_decisions(prefix)):
                    raise JournalConflict("Candidate is not backed by a retained planning path")
                parent = branch_id
            contract = {
                "version": VERSION, "run_id": run_id, **data,
                "steps": list(TOOL_SEQUENCE), "lag_policy": {"eg_aic": "AIC", "eg_bic": "BIC"}[decisions[1]],
                "decisions": decisions, "alpha": ALPHA, "retrieval": backend,
                "evidence_ids": sorted(ids),
                "corpus_sha256": CORPUS_SHA256, "source_sha256": self._release,
            }
            candidates.append({"digest": fingerprint(contract), "protocol": contract})
        record = {**plan, "run_id": run_id, "candidates": candidates, "retrieval": backend, "input": data}
        self.journal.append(run_id, "plan", record)
        if not candidates:
            verdict = {"verdict": "unresolved", "reason": plan["reason"], "evidence_refs": []}
            self.journal.append(run_id, "verdict", verdict)
            return {**record, "report": verdict, "bundle": {
                "events": self.journal.events(run_id), "missing": ["approved protocol", "observations", "holdout evaluation"],
            }}
        return record

    def _planned_candidate(self, run_id: str, digest: str) -> tuple[dict, str]:
        try:
            sha(digest)
        except ValueError as error:
            raise JournalConflict("Approval must match an exact displayed protocol digest") from error
        plan = self.journal.get(run_id, "plan")
        if plan is None or plan.get("run_id") != run_id or plan["status"] != "awaiting_approval":
            raise JournalConflict("No approvable plan for this run")
        entry = next((item for item in plan["candidates"] if item["digest"] == digest), None)
        if entry is None or entry["protocol"]["run_id"] != run_id or fingerprint(entry["protocol"]) != digest:
            raise JournalConflict("Approval must match an exact displayed protocol digest")
        if entry["protocol"]["source_sha256"] != self._release:
            raise JournalConflict("Source changed after planning; a fresh plan is required")
        check_version(entry["protocol"].get("version"))
        check_window(Window(**entry["protocol"]["window"]))
        current_history = self._history.identity() if self._history else None
        if entry["protocol"].get("history") != current_history:
            raise JournalConflict("Supply the same frozen historical manifest used to plan this run")
        return entry, fingerprint(plan)

    def _sample(self, protocol: dict) -> tuple[Point, ...]:
        window = Window(**protocol["window"])
        if self._history is not None:
            return self._history.sample()
        return guarded_rows(synthetic(protocol["case"]), window)

    def approve(self, run_id: str, digest: str) -> dict:
        validate_id(run_id)
        entry, plan_hash = self._planned_candidate(run_id, digest)
        existing = self.journal.get(run_id, "approval")
        if existing is not None:
            if existing["digest"] != digest:
                raise JournalConflict("This run is already approved for a different candidate")
            return self._approval(run_id)
        runtime = self._runtime_identity()
        protocol = entry["protocol"]
        sample = self._sample(protocol)
        approval = {**entry, "plan_sha256": plan_hash, "runtime": runtime,
                    "sample_sha256": sample_fingerprint(sample), "sample_n": len(sample),
                    "authority": "explicit local CLI operator"}
        self.journal.append(run_id, "approval", approval)
        return approval

    def _approval(self, run_id: str) -> dict:
        validate_id(run_id)
        approval = self.journal.get(run_id, "approval")
        if approval is None or approval["protocol"]["run_id"] != run_id:
            raise JournalConflict("A run-bound human approval is required")
        entry, plan_hash = self._planned_candidate(run_id, approval["digest"])
        if (entry["protocol"] != approval["protocol"] or approval.get("plan_sha256") != plan_hash
                or approval.get("runtime") != self._runtime_identity()):
            raise JournalConflict("Approved plan or numerical runtime has changed")
        return approval

    @staticmethod
    def _binding(approval: dict, tool: str) -> dict:
        protocol = approval["protocol"]
        return asdict(ToolBinding(protocol["run_id"], approval["digest"], tool,
                                  Window(**protocol["window"]), approval["sample_sha256"], protocol["version"]))

    def _result(self, run_id: str, tool: str, binding: dict) -> dict | None:
        key = f"result:{tool}"
        result = self.journal.get(run_id, key)
        if result is not None and (
            result.get("id") != key or any(result.get(field) != value for field, value in binding.items())
            or self.journal.get(run_id, f"reserved:{tool}") != binding
        ):
            raise JournalConflict("Result does not match the reserved, approved operation")
        return result

    def _finding(self, run_id: str, tool: str, approval: dict) -> dict:
        result = self._result(run_id, tool, self._binding(approval, tool))
        if result is None:
            raise JournalConflict("No bound result to review")
        return self.review_result(tool, approval, result)

    @staticmethod
    def review_result(tool: str, approval: dict, result: dict) -> dict:
        observation = result.get("observation")
        result_hash = fingerprint(result)
        if isinstance(observation, dict) and "error" not in observation:
            protocol = approval["protocol"]
            config = {"tool": tool, "lag_policy": protocol["lag_policy"], "alpha": protocol["alpha"],
                      "window": protocol["window"], "n": approval["sample_n"], **approval["runtime"],
                      "sample_sha256": approval["sample_sha256"]}
            if (observation.get("config") != config or observation.get("config_sha256") != fingerprint(config)
                    or not isinstance(observation.get("result"), dict)
                    or observation.get("result_sha256") != fingerprint(observation["result"])):
                finding = assess(tool, None)
                finding.update(rule_id="EVIDENCE_BINDING_FAILED",
                               reason="The numerical payload does not match the approved configuration or its result digest.")
            else:
                finding = assess(tool, observation)
        else:
            finding = assess(tool, observation)
        return {"result_id": result["id"], "evidence_sha256": result_hash, **finding}

    def _progress(self, run_id: str, approval: dict) -> tuple[str | None, list[dict]]:
        findings = []
        for tool in TOOL_SEQUENCE:
            finding = self.journal.get(run_id, f"review:{tool}")
            if finding is None:
                return tool, findings
            # A stored review is a record of the decision, not authority to bypass it.
            if finding != self._finding(run_id, tool, approval):
                raise JournalConflict("Review conflicts with the approved evidence and rules")
            findings.append(finding)
            if finding["decision"] != "proceed":
                return None, findings
        return None, findings

    def next_step(self, run_id: str) -> str | None:
        return self._progress(run_id, self._approval(run_id))[0]

    def action(self, run_id: str, tool: str) -> dict:
        if type(tool) is not str or tool not in TOOL_SEQUENCE:
            raise JournalConflict("Tool is outside the in-sample allowlist")
        approval = self._approval(run_id)
        if tool not in TOOL_SEQUENCE or tool != self._progress(run_id, approval)[0]:
            raise JournalConflict("Tool is out of sequence, not reviewed, or not authorized")
        key = f"result:{tool}"
        binding = self._binding(approval, tool)
        recorded = self._result(run_id, tool, binding)
        if recorded is not None:
            return recorded
        protocol = approval["protocol"]
        window = Window(**protocol["window"])
        sample = self._sample(protocol)
        if sample_fingerprint(sample) != approval["sample_sha256"]:
            raise JournalConflict("Numerical input changed after approval")
        if not self.journal.append(run_id, f"reserved:{tool}", binding):
            # A process may have crashed after invoking the tool. Never replay that reservation.
            recorded = self._result(run_id, tool, binding)
            if recorded is not None:
                return recorded
            raise JournalConflict("Reserved operation has no result; reconciliation is required")
        try:
            options = {"sessions": self._history.sessions} if self._history else {}
            observation = self._numerical_tool(tool, sample, window, protocol["lag_policy"], **options)
        except (ArithmeticError, ImportError, RuntimeError, ValueError):
            observation = {"error": "statistical_tool_failed"}
        try:
            if not isinstance(observation, dict):
                raise ValueError("Expected an observation object")
            fingerprint(observation)
        except (RecursionError, TypeError, ValueError):
            observation = {"error": "invalid_tool_observation"}
        record = {"id": key, **binding, "observation": observation}
        self.journal.append(run_id, key, record)
        return record

    def critique(self, run_id: str, tool: str) -> dict:
        approval = self._approval(run_id)
        if tool not in TOOL_SEQUENCE or tool != self._progress(run_id, approval)[0]:
            raise JournalConflict("Critic must review the current milestone")
        finding = self._finding(run_id, tool, approval)
        self.journal.append(run_id, f"review:{tool}", finding)
        return finding

    def finalize(self, run_id: str) -> dict:
        step, findings = self._progress(run_id, self._approval(run_id))
        if step is not None:
            raise JournalConflict("A milestone review is still required")
        if not findings or findings[-1]["decision"] == "proceed":
            raise JournalConflict("No terminal decision is supported")
        last = findings[-1]
        verdict = {"verdict": last["decision"], "reason": last["reason"],
                   "evidence_refs": [finding["result_id"] for finding in findings]}
        self.journal.append(run_id, "verdict", verdict)
        scope = (f"{self._history.classification}, in-sample historical research only"
                 if self._history else "synthetic, in-sample research only")
        return {
            "run_id": run_id, "status": "finished",
            "report": {**verdict, "milestone_reviews": findings, "scope": scope,
                       "holdout": "locked and untested"},
            "bundle": {"events": self.journal.events(run_id), "missing": ["holdout evaluation"]},
        }
