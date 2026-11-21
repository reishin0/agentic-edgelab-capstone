"""Version 1 runtime-validated messages for the integrated EdgeLab workflow."""

from dataclasses import dataclass
import re
from uuid import UUID

from .stat_tools import CASES, TOOL_SEQUENCE, Window, check_window
from .methods import QUERY


VERSION = 1


def fields(value, expected):
    if type(value) is not dict or value.keys() != set(expected):
        raise ValueError("Message fields do not match the contract")


def check_version(value):
    if type(value) is not int or value != VERSION:
        raise ValueError("Unsupported message version")


def run_id(value):
    if type(value) is not str or len(value) != 36 or str(UUID(value)) != value:
        raise ValueError("Expected a canonical run UUID")
    return value


def sha(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a canonical SHA-256 digest")
    return value


def tool_name(value):
    if type(value) is not str or value not in TOOL_SEQUENCE:
        raise ValueError("Tool is outside the in-sample allowlist")
    return value


@dataclass(frozen=True, slots=True)
class PlanRequest:
    run_id: str
    case: str | None
    backend: str


@dataclass(frozen=True, slots=True)
class ExecuteRequest:
    run_id: str
    digest: str


def parse_run(message):
    # Missing version is accepted only for the existing in-process graph callers.
    if type(message) is not dict:
        raise ValueError("Expected a run request object")
    payload = dict(message)
    check_version(payload.pop("version", VERSION))
    mode = payload.get("mode")
    if mode == "plan":
        fields(payload, {"mode", "run_id", "case", "backend"})
        case, backend = payload["case"], payload["backend"]
        if (case is not None and (type(case) is not str or case not in CASES)) or backend not in ("local", "pinecone"):
            raise ValueError("Unknown research case or retrieval backend")
        return PlanRequest(run_id(payload["run_id"]), case, backend)
    if mode == "execute":
        fields(payload, {"mode", "run_id", "digest"})
        return ExecuteRequest(run_id(payload["run_id"]), sha(payload["digest"]))
    raise ValueError("Unknown workflow mode")


@dataclass(frozen=True, slots=True)
class ToolBinding:
    run_id: str
    approval: str
    tool: str
    window: Window
    sample_sha256: str
    version: int = VERSION

    def __post_init__(self):
        check_version(self.version)
        run_id(self.run_id)
        sha(self.approval)
        tool_name(self.tool)
        check_window(self.window)
        sha(self.sample_sha256)


@dataclass(frozen=True, slots=True)
class MCPRequest:
    name: str
    topic: str | None


def parse_mcp(name, args):
    if name not in ("method_notes_search", "branch_records", "statistical_tool_catalog"):
        raise ValueError("Tool unavailable")
    fields(args, {"version", "topic"} if name == "method_notes_search" else {"version"})
    check_version(args["version"])
    topic = args.get("topic")
    if name == "method_notes_search" and (type(topic) is not str or topic not in QUERY):
        raise ValueError("Topic is outside the public-method allowlist")
    return MCPRequest(name, topic)
