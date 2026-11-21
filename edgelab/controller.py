"""In-memory transition guards for the first synthetic vertical slice."""

from dataclasses import asdict

from . import roles
from .contracts import (
    ActionRequest, Binding, Event, Finding, FixtureApproval, MethodNote,
    Observation, Signal, Status, Step, Verdict, fingerprint,
)
from .fixtures import Fixture, QUESTION


class RejectedTransition(ValueError):
    """A request failed validation before calling a tool or changing state."""


class ResearchRun:
    def __init__(self, run_id: str, fixture: Fixture, method: MethodNote | None) -> None:
        self._run_id = run_id
        self._fixture = fixture
        self._plan = roles.plan_protocol(QUESTION, method)
        self._ledger: list[Event] = []
        self._observations: list[Observation] = []
        self._findings: list[Finding] = []
        self._accepted_ids: set[str] = set()
        self._approval: FixtureApproval | None = None
        self._verdict: Verdict | None = None
        self._reason = ""
        self._recommendation: Verdict | None = None
        self._status = Status.AWAITING_APPROVAL if self._plan.protocol else Status.FINAL_REVIEW
        for branch in self._plan.branches:
            self._record("branch_accepted" if branch.accepted else "branch_rejected",
                         branch.branch_id, branch.reason)

    @property
    def status(self) -> Status:
        return self._status

    @property
    def version(self) -> int:
        return len(self._ledger)

    @property
    def ledger(self) -> tuple[Event, ...]:
        return tuple(self._ledger)

    def _record(self, kind: str, reference: str, detail: str) -> None:
        self._ledger.append(Event(self.version + 1, kind, reference, detail))

    def _require(self, status: Status) -> None:
        if self.status != status:
            raise RejectedTransition(f"Requires {status}; current status is {self.status}")

    def binding(self, message_id: str) -> Binding:
        if not self._plan.protocol:
            raise RejectedTransition("No eligible protocol to bind")
        return Binding(
            self._run_id, self._plan.protocol.digest, self._fixture.digest,
            self.version, message_id,
        )

    def _validate(self, binding: Binding) -> None:
        if not binding.message_id or binding.message_id in self._accepted_ids:
            raise RejectedTransition("Missing or replayed message ID")
        if binding != self.binding(binding.message_id):
            raise RejectedTransition("Request does not match the current run, version, protocol or fixture")

    def approve(self, approval: FixtureApproval) -> None:
        self._require(Status.AWAITING_APPROVAL)
        self._validate(approval.binding)
        self._approval = approval
        self._accepted_ids.add(approval.binding.message_id)
        self._record("fixture_approved", approval.binding.message_id, approval.binding.protocol_digest)
        self._status = Status.READY

    def _permitted_step(self) -> Step:
        self._require(Status.READY)
        if self._approval is None or self._plan.protocol is None:
            raise RejectedTransition("Execution requires a frozen and approved protocol")
        steps = self._plan.protocol.steps
        if len(self._observations) >= len(steps) or len(self._findings) != len(self._observations):
            raise RejectedTransition("No unreviewed or out-of-sequence action may proceed")
        return steps[len(self._observations)]

    def propose_next(self, message_id: str) -> ActionRequest:
        return roles.propose_action(self.binding(message_id), self._permitted_step())

    def execute(self, request: ActionRequest) -> Observation:
        permitted = self._permitted_step()
        self._validate(request.binding)
        if request.step != permitted:
            raise RejectedTransition(f"Only {permitted} is permitted at this stage")
        self._accepted_ids.add(request.binding.message_id)
        try:
            signal = self._fixture.observe(permitted)
        except (RuntimeError, ValueError):
            signal = Signal.ERROR
        if signal not in roles.ALLOWED_SIGNALS[permitted]:
            signal = Signal.ERROR
        observation = Observation(f"result-{len(self._observations) + 1}", permitted, signal)
        self._observations.append(observation)
        self._record("tool_observed", observation.result_id, f"{permitted}:{signal}")
        self._status = Status.REVIEW_REQUIRED
        return observation

    def review_milestone(self) -> Finding:
        self._require(Status.REVIEW_REQUIRED)
        observation = self._observations[-1]
        finding = roles.critique(observation)
        self._findings.append(finding)
        self._record("critic_reviewed", observation.result_id, finding.reason)
        # Only an observed prerequisite pass can unlock the dependent step.
        permitted = (
            observation.step == Step.DIAGNOSTICS
            and observation.signal == Signal.ASSUMPTIONS_MET
            and finding.result_id == observation.result_id
            and finding.verdict is None
        )
        self._status = Status.READY if permitted else Status.FINAL_REVIEW
        return finding

    def finalize(self) -> None:
        self._require(Status.FINAL_REVIEW)
        self._recommendation = self._findings[-1].verdict if self._findings else Verdict.UNRESOLVED
        self._record("critic_final_review", "verdict", str(self._recommendation or "no_recommendation"))

        # The controller applies the frozen rule, rather than adopting the recommendation blindly.
        signals = tuple(observation.signal for observation in self._observations)
        ruled = {
            (Signal.ASSUMPTIONS_MET, Signal.SUPPORT): Verdict.CONTINUE,
            (Signal.ASSUMPTIONS_MET, Signal.NO_SUPPORT): Verdict.STOP,
        }.get(signals, Verdict.UNRESOLVED)
        reviewed = tuple(finding.result_id for finding in self._findings) == tuple(
            observation.result_id for observation in self._observations
        )
        if not reviewed or ruled != self._recommendation:
            self._verdict = Verdict.UNRESOLVED
            self._reason = "Missing or inconsistent review; human resolution required."
        else:
            self._verdict = ruled
            self._reason = self._findings[-1].reason if self._findings else (
                "No eligible protocol because required method evidence is absent."
            )
        self._record("verdict_recorded", "verdict", self._verdict)
        self._status = Status.FINISHED

    def packet(self) -> dict:
        protocol = self._plan.protocol
        if self.status != Status.FINISHED:
            return {
                "run_id": self._run_id,
                "status": self.status,
                "state_version": self.version,
                "protocol_digest": protocol.digest if protocol else None,
                "fixture_digest": self._fixture.digest,
                "proposed_protocol": asdict(protocol) if protocol else None,
                "reviewed_branches": [asdict(branch) for branch in self._plan.branches],
                "next": "Explicit synthetic-fixture approval required." if self.status == Status.AWAITING_APPROVAL else None,
            }
        assert self._verdict is not None
        evidence = tuple(observation.result_id for observation in self._observations)
        entries = {
            "protocol": asdict(protocol) if protocol else None,
            "fixture_approval": asdict(self._approval) if self._approval else None,
            "fixture_provenance": self._fixture.provenance(),
            "branches": [asdict(branch) for branch in self._plan.branches],
            "observations": [asdict(observation) for observation in self._observations],
            "findings": [asdict(finding) for finding in self._findings],
            "ledger": [asdict(event) for event in self._ledger],
            "verdict": {"recommendation": self._recommendation, "recorded": self._verdict, "reason": self._reason},
        }
        manifest = {
            key: ({"status": "present", "sha256": fingerprint(value)} if value is not None
                  else {"status": "not_produced"})
            for key, value in entries.items()
        }
        return {
            "status": self.status,
            "report": roles.render_report(self._run_id, self._verdict, self._reason, tuple(self._findings), evidence),
            "bundle": {"manifest": manifest, "entries": entries},
        }
