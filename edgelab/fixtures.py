"""Synthetic inputs only. Signals are scripted, not computed statistics."""

from dataclasses import dataclass

from .contracts import MethodNote, Question, Signal, Step, fingerprint


QUESTION = Question(
    "Do two synthetic series support a stable long-run relationship?",
    ("series-A", "series-B"),
)

METHOD = MethodNote(
    "synthetic-method:diagnostics-first",
    "1",
    "fixture note 1",
    "Review the relationship test prerequisites first. Conflicting diagnostics "
    "leave the question unresolved; they do not authorize the relationship test.",
)

TOOL_VERSION = "scripted-signals-v1"
SCENARIOS = ("conflict", "support", "no-support", "tool-error")


@dataclass(frozen=True, slots=True)
class Fixture:
    name: str

    def __post_init__(self) -> None:
        if self.name not in SCENARIOS:
            raise ValueError("Unknown synthetic fixture")

    @property
    def digest(self) -> str:
        return fingerprint(self.provenance())

    def provenance(self) -> dict[str, str]:
        return {"origin": "bundled synthetic fixture", "fixture": self.name, "tool": TOOL_VERSION}

    def observe(self, step: Step) -> Signal:
        if step == Step.DIAGNOSTICS:
            if self.name == "tool-error":
                raise RuntimeError("Synthetic diagnostics tool failed")
            return Signal.CONFLICT if self.name == "conflict" else Signal.ASSUMPTIONS_MET
        if step == Step.RELATIONSHIP and self.name in ("support", "no-support"):
            return Signal.SUPPORT if self.name == "support" else Signal.NO_SUPPORT
        raise ValueError("Fixture has no observation for that step")
