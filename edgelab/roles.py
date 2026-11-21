"""Small deterministic role fixtures, not LLM or CrewAI agents."""

from dataclasses import asdict

from .contracts import (
    ActionRequest, Binding, CandidateReview, Finding, MethodNote, Observation,
    Plan, Protocol, Question, Signal, Step, Verdict,
)


RULE_VERSION = "demo-decision-rules-v1"
SEQUENCE = (Step.DIAGNOSTICS, Step.RELATIONSHIP)
ALLOWED_SIGNALS = {
    Step.DIAGNOSTICS: (Signal.ASSUMPTIONS_MET, Signal.CONFLICT, Signal.ERROR),
    Step.RELATIONSHIP: (Signal.SUPPORT, Signal.NO_SUPPORT, Signal.ERROR),
}


def review_candidate(
    branch_id: str, parent_id: str | None, steps: tuple[Step, ...], method: MethodNote | None
) -> CandidateReview:
    if method is None:
        return CandidateReview(branch_id, parent_id, steps, False, "Required method evidence is absent.", None)
    accepted = steps == SEQUENCE
    reason = "Diagnostics and review precede relationship analysis." if accepted else (
        "Add prerequisite diagnostics and review before the relationship step."
    )
    return CandidateReview(branch_id, parent_id, steps, accepted, reason, method.source_id)


def plan_protocol(question: Question, method: MethodNote | None) -> Plan:
    # This planning function never receives a run, tool, fixture, or observation.
    first = review_candidate("branch-1", None, (Step.RELATIONSHIP,), method)
    if method is None:
        return Plan(None, (first,))
    revised = review_candidate("branch-2", first.branch_id, SEQUENCE, method)
    return Plan(Protocol(question, method, revised.steps, RULE_VERSION), (first, revised))


def propose_action(binding: Binding, permitted_step: Step) -> ActionRequest:
    return ActionRequest(binding, permitted_step)


def critique(observation: Observation) -> Finding:
    reason, verdict = {
        Signal.ASSUMPTIONS_MET: ("Prerequisite review permits the next declared step.", None),
        Signal.CONFLICT: ("Conflicting prerequisites block dependent analysis.", Verdict.UNRESOLVED),
        Signal.ERROR: ("No valid tool result; do not treat missing evidence as zero.", Verdict.UNRESOLVED),
        Signal.SUPPORT: ("The fixture supports moving to the next declared stage only.", Verdict.CONTINUE),
        Signal.NO_SUPPORT: ("The fixture meets the predeclared stop condition.", Verdict.STOP),
    }[observation.signal]
    return Finding(observation.result_id, reason, verdict)


def render_report(
    run_id: str, verdict: Verdict, reason: str, findings: tuple[Finding, ...],
    evidence_refs: tuple[str, ...],
) -> dict:
    return {
        "run_id": run_id,
        "verdict": verdict,
        "reason": reason,
        "reviewed_findings": [asdict(finding) for finding in findings],
        "evidence_refs": evidence_refs,
        "limitations": "Scripted synthetic observations, not statistical results or action authority.",
    }
