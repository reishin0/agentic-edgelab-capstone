"""Immutable records exchanged by the synthetic workflow."""

from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
import json


def fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


class Step(StrEnum):
    DIAGNOSTICS = "diagnostics"
    RELATIONSHIP = "relationship"


class Status(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    REVIEW_REQUIRED = "review_required"
    FINAL_REVIEW = "final_review"
    FINISHED = "finished"


class Verdict(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"
    UNRESOLVED = "unresolved"


class Signal(StrEnum):
    ASSUMPTIONS_MET = "assumptions_met"
    CONFLICT = "conflicting_diagnostics"
    SUPPORT = "supports_next_stage"
    NO_SUPPORT = "does_not_support_next_stage"
    ERROR = "tool_error"


@dataclass(frozen=True, slots=True)
class Question:
    hypothesis: str
    series_labels: tuple[str, str]


@dataclass(frozen=True, slots=True)
class MethodNote:
    source_id: str
    version: str
    locator: str
    requirement: str


@dataclass(frozen=True, slots=True)
class Protocol:
    question: Question
    method: MethodNote
    steps: tuple[Step, ...]
    decision_rule: str

    @property
    def digest(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True, slots=True)
class CandidateReview:
    branch_id: str
    parent_id: str | None
    steps: tuple[Step, ...]
    accepted: bool
    reason: str
    source_id: str | None


@dataclass(frozen=True, slots=True)
class Plan:
    protocol: Protocol | None
    branches: tuple[CandidateReview, ...]


@dataclass(frozen=True, slots=True)
class Binding:
    run_id: str
    protocol_digest: str
    fixture_digest: str
    expected_version: int
    message_id: str


@dataclass(frozen=True, slots=True)
class FixtureApproval:
    binding: Binding


@dataclass(frozen=True, slots=True)
class ActionRequest:
    binding: Binding
    step: Step


@dataclass(frozen=True, slots=True)
class Observation:
    result_id: str
    step: Step
    signal: Signal


@dataclass(frozen=True, slots=True)
class Finding:
    result_id: str
    reason: str
    verdict: Verdict | None


@dataclass(frozen=True, slots=True)
class Event:
    version: int
    kind: str
    reference: str
    detail: str
