"""Optional CrewAI role tasks; never a controller, tool runner, or evidence store."""

from dataclasses import dataclass
from importlib.metadata import version
import json
import os
from typing import Protocol

from .providers import ProviderError


class DecisionModel(Protocol):
    def choose(self, role: str, prompt: str, options: tuple[str, ...], timeout: float) -> str: ...


ROLE_TASKS = {
    "planner": ("Planner", "Rank eligible blinded protocol options using declared prerequisites."),
    "critic": ("Critic", "Challenge the plan against reviewed methods and request escalation if needed."),
    "reporter": ("Reporter", "Select the presentation order of an already verified research report."),
}
REPORT_ORDERS = ("verdict_first", "evidence_first")


@dataclass(frozen=True, slots=True)
class CrewRoles:
    model: DecisionModel

    @property
    def models(self):
        return getattr(self.model, "models", {})

    def choose(self, role: str, prompt: str, options: tuple[str, ...], timeout: float) -> str:
        if role not in ROLE_TASKS or not options or len(options) > 8 or len(set(options)) != len(options):
            raise ValueError("Invalid CrewAI role or choices")
        if os.getenv("CREWAI_DISABLE_TELEMETRY", "").lower() not in {"true", "1", "yes", "on"}:
            raise ProviderError("Start the isolated CrewAI process with CREWAI_DISABLE_TELEMETRY=true")
        if os.getenv("PYTHON_DOTENV_DISABLED") != "1":
            raise ProviderError("Start CrewAI with PYTHON_DOTENV_DISABLED=1; use an explicit settings file")
        try:
            from crewai import Agent, BaseLLM, Crew, Process, Task
        except ImportError:
            raise ProviderError("CrewAI is unavailable; install the crew extra in an isolated environment") from None

        name, goal = ROLE_TASKS[role]
        model = self.model
        used = False
        answer = None

        class ChoiceLLM(BaseLLM):
            def call(self, messages, tools=None, callbacks=None, available_functions=None, **kwargs):
                nonlocal used, answer
                if used or tools or available_functions:
                    raise ProviderError("CrewAI role exceeded its one-call, no-tools contract")
                used = True
                # Forward the controller's bounded context, not framework-generated tool instructions.
                answer = model.choose(role, f"{goal}\n{prompt}", options, timeout)
                if answer not in options:
                    raise ProviderError("CrewAI model returned a non-allowlisted choice")
                return "Final Answer: " + answer

        try:
            agent = Agent(role=name, goal=goal, backstory="EdgeLab advisory role. The controller has final authority.",
                          llm=ChoiceLLM(model="edgelab-bounded-choice", temperature=0),
                          tools=[], allow_delegation=False, allow_code_execution=False,
                          max_iter=1, max_retry_limit=0, verbose=False)
            task = Task(description=goal, expected_output="Exactly one controller-allowlisted label.",
                        agent=agent, tools=[])
            outcome = Crew(agents=[agent], tasks=[task], process=Process.sequential,
                           memory=False, share_crew=False, tracing=False, verbose=False).kickoff()
            raw = outcome.raw.strip()
        except Exception as error:
            raise ProviderError(f"CrewAI {role} task failed ({type(error).__name__})") from None
        if not used or raw != answer or raw not in options:
            raise ProviderError("CrewAI output did not match the validated choice")
        return raw


def advisory_report(verified: dict, roles: CrewRoles) -> dict:
    """Add presentation metadata without editing the canonical report or ledger."""
    report = verified["report"]
    if report["status"] != "finished":
        raise ValueError("CrewAI reporting requires a finished, verified run")
    context = {key: report[key] for key in ("status", "verdict", "scope", "holdout")}
    context["review_rules"] = [row["rule_id"] for row in report["milestone_reviews"] if "rule_id" in row]
    try:
        order = roles.choose("reporter", json.dumps(context), REPORT_ORDERS, 15)
    except ProviderError:
        return {**verified, "presentation": {"backend": "crewai", "status": "unavailable",
                                             "order": "verdict_first"}}
    return {**verified, "presentation": {"backend": "crewai", "version": version("crewai"),
                                         "status": "completed", "order": order}}
