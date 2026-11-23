"""Outcome-blind bounded beam search over allowlisted protocol decisions."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
from time import monotonic

from .methods import BY_ID, REQUIRED_METHOD_IDS, Note
from .providers import ChoiceAdviser, ProviderError


LEVELS = (
    ("conditional_i1", "stationary_levels", "correlation_only"),
    ("eg_aic", "eg_bic", "unrestricted_lags"),
    ("cusum_ecm_ou", "cusum_only", "skip_stability"),
    ("chronological_locked", "reuse_holdout", "in_sample_as_holdout"),
)
SCORES = {"conditional_i1": 8, "eg_aic": 8, "eg_bic": 8, "cusum_ecm_ou": 9, "chronological_locked": 10}
DECISION_TEXT = {
    "conditional_i1": "Run ADF and KPSS first. Allow Engle-Granger only if both series are I(1).",
    "stationary_levels": "Assume stationary levels without the required ADF and KPSS prerequisite.",
    "correlation_only": "Use correlation as evidence of cointegration; skip ADF and KPSS.",
    "eg_aic": "Fix AIC lag selection before testing; run Engle-Granger in both directions.",
    "eg_bic": "Fix BIC lag selection before testing; run Engle-Granger in both directions.",
    "unrestricted_lags": "Change the lag rule after inspecting the results.",
    "cusum_ecm_ou": "After the relationship check, require CUSUM, ECM, and OU/half-life diagnostics.",
    "cusum_only": "Run CUSUM but omit the planned ECM and OU/half-life diagnostics.",
    "skip_stability": "Omit stability diagnostics.",
    "chronological_locked": "Use the approved in-sample window; keep the later holdout locked and untested.",
    "reuse_holdout": "Reuse holdout observations to select the protocol.",
    "in_sample_as_holdout": "Treat the same observations as in-sample and as holdout.",
}
REQUIRED = set(REQUIRED_METHOD_IDS)
BEAM = 2
THOUGHT_LIMIT = 21
MODEL_LIMIT = 16
CRITIC_LABELS = ("accept", "escalate")


def score_decisions(decisions: tuple[str, ...]) -> float | None:
    if not 1 <= len(decisions) <= len(LEVELS) or any(
        type(decision) is not str or decision not in allowed or decision not in SCORES
        for decision, allowed in zip(decisions, LEVELS)
    ):
        return None
    return sum(SCORES[decision] for decision in decisions) / len(decisions)


@dataclass(frozen=True, slots=True)
class Thought:
    id: str
    parent_id: str | None
    decisions: tuple[str, ...]
    score: float | None
    reason: str


def critic_prompt(plans: tuple[tuple[str, ...], ...], notes: tuple[Note, ...]) -> str:
    evidence = {note.id: note.text for note in notes if note.id in REQUIRED and BY_ID.get(note.id) == note}
    return json.dumps({
        "task": "Review protocol design before analysis. No data or results should be available yet.",
        "checklist": [
            "Method evidence includes ADF, KPSS, and Engle-Granger. ADF and KPSS precede testing; require both series to be I(1).",
            "Fix AIC or BIC in advance and require Engle-Granger in both directions.",
            "Require CUSUM, ECM, and OU diagnostics after the relationship check.",
            "Use only the in-sample window. The later holdout stays locked.",
        ],
        "plans": [[DECISION_TEXT[label] for label in decisions] for decisions in plans],
        "method_evidence": evidence,
        "decision_rule": {
            "accept": "All plans meet all four checks. Accept only means present for human approval, never execute.",
            "escalate": "At least one plan fails a check, for example skipping the unit-root tests or reusing holdout data.",
        },
    })


def protocol_issues(plans: tuple[tuple[str, ...], ...], notes: tuple[Note, ...]) -> tuple[str, ...]:
    evidence = {note.id for note in notes if BY_ID.get(note.id) == note}
    issues = []
    if not REQUIRED <= evidence:
        issues.append("missing_method_evidence")
    if not 1 <= len(plans) <= BEAM:
        issues.append("invalid_candidate_count")
    for index, decisions in enumerate(plans, 1):
        if len(decisions) != 4:
            issues.append(f"candidate_{index}:incomplete")
            continue
        assumptions, method, stability, validation = decisions
        for valid, check in (
            (assumptions == "conditional_i1", "unit_root_prerequisite"),
            (method in ("eg_aic", "eg_bic"), "fixed_bidirectional_method"),
            (stability == "cusum_ecm_ou", "stability_diagnostics"),
            (validation == "chronological_locked", "locked_holdout"),
        ):
            if not valid:
                issues.append(f"candidate_{index}:{check}")
    return tuple(issues)


def search(notes: tuple[Note, ...], model: ChoiceAdviser | None = None, *, use_model_critic: bool = True) -> dict:
    evidence = {note.id for note in notes if BY_ID.get(note.id) == note}
    trace: list[dict] = []
    attempts = 0
    decisions_log: list[dict[str, str]] = []
    cutoff = monotonic() + 225
    preferred = None
    review = {"mode": "model_advisory" if model is not None and use_model_critic else "deterministic",
              "outcome": "not_run", "issues": []}

    def ask(role: str, stage: str, prompt: str, labels: tuple[str, ...]) -> str:
        nonlocal attempts
        remaining = cutoff - monotonic()
        if model is None or remaining <= 0 or attempts >= MODEL_LIMIT:
            raise ProviderError("Planning time or model budget exhausted")
        attempts += 1
        choice = model.choose(role, prompt, labels, min(remaining, 15))
        if monotonic() >= cutoff:
            raise ProviderError("Model response exceeded the planning deadline")
        if choice not in labels:
            raise ProviderError("Model response is outside the eligible choices")
        decisions_log.append({"role": role, "stage": stage, "choice": choice})
        return choice

    def result(candidates=(), reason="") -> dict:
        models = getattr(model, "models", None)
        return {"status": "awaiting_approval" if candidates else "unresolved",
                "candidates": list(candidates), "branches": trace, "reason": reason,
                "method_evidence": [asdict(BY_ID[key]) for key in sorted(evidence)],
                "thoughts": len(trace), "model_attempts": attempts, "model_preference": preferred,
                "model_decisions": decisions_log, "protocol_review": review,
                "role_models": {role: models.get(role)
                                for role in (("planner", "critic") if use_model_critic else ("planner",))}
                if isinstance(models, Mapping) else {}}

    if not REQUIRED <= evidence:
        review.update(outcome="blocked", issues=["missing_method_evidence"])
        return result(reason="Missing applicable ADF, KPSS or Engle-Granger method evidence.")

    def record_layer(layer: list[Thought], depth: int, kept: list[Thought], prune_reason: str) -> None:
        for node in layer:
            retained = node in kept
            trace.append({**asdict(node), "depth": depth, "retained": retained,
                          "reason": node.reason if retained or node.score is None else prune_reason})

    frontier = [Thought("root", None, (), None, "")]
    layer: list[Thought] = []
    depth = 0
    try:
        for depth, options in enumerate(LEVELS, 1):
            for parent in frontier:
                ordered = options
                eligible = tuple(option for option in options if score_decisions((*parent.decisions, option)) is not None)
                if model is not None and len(eligible) > 1:
                    lead = ask("planner", f"rank_depth_{depth}", json.dumps({
                        "task": "Rank one valid option first for a blinded long-run relationship test.",
                        "rule": "Either fixed lag rule is valid. No data-driven choice is allowed at planning time.",
                        "prerequisites": [DECISION_TEXT[label] for label in parent.decisions],
                        "options": {label: DECISION_TEXT[label] for label in eligible},
                    }), eligible)
                    ordered = (lead, *(option for option in options if option != lead))
                for option in ordered:
                    if len(trace) + len(layer) >= THOUGHT_LIMIT or monotonic() >= cutoff:
                        record_layer(layer, depth, [], "planning budget exhausted")
                        return result(reason="Planning budget ended before complete validation.")
                    decisions = (*parent.decisions, option)
                    score = score_decisions(decisions)
                    reason = "passes available prerequisites" if score is not None else (
                        "violates approved method, tool, stability or protected-holdout boundary"
                    )
                    layer.append(Thought("/".join(decisions), parent.id, decisions, score, reason))
            ranked = sorted((node for node in layer if node.score is not None), key=lambda node: -node.score)
            frontier = [node for node in ranked if node.score >= 5 and node.score >= ranked[0].score - 3][:BEAM]
            record_layer(layer, depth, frontier, "score or beam pruning")
            layer = []
            if not frontier:
                return result(reason="No protocol survived the hard gates and beam.")
        passing = [node for node in frontier if node.score >= 7]
        if not passing:
            return result(reason="No complete protocol scored at least 7/10.")
        protocols = tuple(node.decisions for node in passing)
        issues = protocol_issues(protocols, notes)
        if issues:
            review.update(outcome="blocked", issues=list(issues))
            return result(reason="Deterministic Critic rejected a protocol.")
        if model is not None and use_model_critic:
            critique = ask("critic", "protocol_review",
                           critic_prompt(protocols, notes), CRITIC_LABELS)
            if critique == "escalate":
                review.update(outcome="escalated")
                return result(reason="Advisory Critic requested human review.")
            if len(passing) > 1 and passing[0].score - passing[1].score <= 0.5:
                labels = tuple(node.id for node in passing)
                descriptions = {node.id: DECISION_TEXT[node.decisions[1]] for node in passing}
                votes = [ask("critic", "tie_vote", "Advisory tie vote only: " + json.dumps({
                    "rule": "Choose a protocol only if it has fewer tests or stronger diagnostics. "
                            "Choose no_preference if only the fixed lag rule differs.",
                    "options": {label: descriptions[label] for label in order},
                }), (*order, "no_preference")) for order in (labels, labels[::-1])]
                preferred = votes[0] if votes[0] == votes[1] and votes[0] in labels else None
        review.update(outcome="passed")
        return result((asdict(node) for node in passing), "The researcher approves one frozen candidate.")
    except ProviderError:
        record_layer(layer, depth, [], "model failure or planning budget exhausted")
        return result(reason="A required model decision failed or exhausted its budget; no protocol was approved.")
