"""The single LangGraph controller for the integration path."""

from collections.abc import Callable
from typing import TypedDict

from . import planning
from .crew_roles import CrewRoles
from .messages import ExecuteRequest, parse_run
from .methods import Note
from .providers import ChoiceAdviser, ProviderError
from .research import ResearchService


class PlanState(TypedDict, total=False):
    notes: list[dict]
    search_result: dict
    retrieval_error: bool


class RunState(TypedDict, total=False):
    mode: str
    run_id: str
    case: str | None
    digest: str
    backend: str
    current_step: str
    search_result: dict
    output: dict


def build_graph(service: ResearchService, retrieve: Callable[[str], tuple[Note, ...]],
                model: ChoiceAdviser | None = None, *, use_model_critic: bool = True,
                adviser_provider: str | None = None):
    from dataclasses import asdict
    from langchain_core.runnables import RunnableLambda
    from langgraph.graph import END, START, StateGraph

    retrieval = RunnableLambda(retrieve, name="public_method_retrieval") | RunnableLambda(
        lambda notes: [asdict(note) for note in notes], name="method_evidence_records")

    def retrieve_notes(_state: PlanState) -> dict:
        try:
            return {"notes": retrieval.invoke("cointegration")}
        except ProviderError:
            return {"notes": [], "retrieval_error": True}

    def beam(state: PlanState) -> dict:
        result = planning.search(tuple(Note(**note) for note in state["notes"]), model,
                                 use_model_critic=use_model_critic)
        if state.get("retrieval_error"):
            result["reason"] = "Configured retrieval failed; no ungrounded fallback was used."
        result["retrieval_observation"] = ("provider_error" if state.get("retrieval_error") else "completed")
        if adviser_provider is not None:
            result["adviser_provider"] = adviser_provider
        profile = getattr(model, "planner_profile", "")
        if isinstance(profile, str) and profile:
            result["planner_profile"] = profile
        if isinstance(model, CrewRoles):
            result["role_backend"] = "crewai"
        return {"search_result": result}

    plan = StateGraph(PlanState)
    plan.add_node("retrieve_methods", retrieve_notes)
    plan.add_node("bounded_beam_search", beam)
    plan.add_edge(START, "retrieve_methods")
    plan.add_edge("retrieve_methods", "bounded_beam_search")
    plan.add_edge("bounded_beam_search", END)

    def save_plan(state: RunState) -> dict:
        return {"output": service.record_plan(state["run_id"], state["case"], state["search_result"], state["backend"])}

    def approve(state: RunState) -> dict:
        service.approve(state["run_id"], state["digest"])
        return {}

    def act(state: RunState) -> dict:
        tool = service.next_step(state["run_id"])
        service.action(state["run_id"], tool)
        return {"current_step": tool}

    def critique(state: RunState) -> dict:
        service.critique(state["run_id"], state["current_step"])
        return {}

    def final(state: RunState) -> dict:
        return {"output": service.finalize(state["run_id"])}

    def next_node(state: RunState) -> str:
        return "action" if service.next_step(state["run_id"]) else "final"

    graph = StateGraph(RunState)
    graph.add_node("planning", plan.compile())
    graph.add_node("save_plan", save_plan)
    graph.add_node("approve", approve)
    graph.add_node("action", act)
    graph.add_node("critic", critique)
    graph.add_node("final", final)
    graph.add_conditional_edges(START, lambda state: state["mode"], {"plan": "planning", "execute": "approve"})
    graph.add_edge("planning", "save_plan")
    graph.add_edge("save_plan", END)
    graph.add_conditional_edges("approve", next_node, {"action": "action", "final": "final"})
    graph.add_edge("action", "critic")
    graph.add_conditional_edges("critic", next_node, {"action": "action", "final": "final"})
    graph.add_edge("final", END)
    def validate(message):
        request = parse_run(message)
        if isinstance(request, ExecuteRequest):
            return {"mode": "execute", "run_id": request.run_id, "digest": request.digest}
        service.check_plan_scope(request.case, request.backend)
        return {"mode": "plan", "run_id": request.run_id, "case": request.case, "backend": request.backend}

    return RunnableLambda(validate, name="validate_run_request") | graph.compile()
