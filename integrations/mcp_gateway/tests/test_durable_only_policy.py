from copy import deepcopy

from mcp import types
import pytest
from pydantic import ValidationError

from mcp_gateway.auth import Principal, principal
from mcp_gateway.backend import BackendUnavailable
from mcp_gateway.code_mode import CodeModeError
from mcp_gateway.config import Backend, Settings
from mcp_gateway.gateway import Gateway


WRITE, RECEIPT = "saveDraft", "getDraftReceipt"
SAVE = f"research__{WRITE}"
DATA = {"body": {"request_id": "draft-1", "preview_sha256": "a" * 64,
                 "draft": {"fixture": "shared-trend", "hypothesis": "Series share a trend."}}}
SAVED = {"request_id": "draft-1", "preview_sha256": "a" * 64, "receipt_sha256": "b" * 64}
BACKEND = {"name": "research", "url": "http://127.0.0.1:9875/mcp", "roles": ["operator"],
           "allowed_tools": [WRITE, RECEIPT], "read_only_tools": [RECEIPT],
           "durable_only_tools": {WRITE: RECEIPT}}
INPUT = {"type": "object", "properties": {"body": {"type": "object"}}, "required": ["body"],
         "additionalProperties": False}
OUTPUT = {"type": "object", "properties": {key: {"type": "string"} for key in SAVED},
          "required": list(SAVED), "additionalProperties": False}


class SyntheticBackend:
    discovery_requires_credential = False

    def __init__(self):
        self.calls = []
        self.receipt = None
        self.lose_response = False

    async def list_tools(self, _ttl, **_):
        return [types.Tool(name=tool, inputSchema=INPUT, outputSchema=OUTPUT) for tool in (WRITE, RECEIPT)]

    async def request(self, operation, tool, arguments, **_):
        assert operation == "call_tool"
        self.calls.append((tool, deepcopy(arguments)))
        if tool == WRITE:
            self.receipt = SAVED.copy()
            if self.lose_response:
                raise BackendUnavailable("Synthetic response loss after the committed write")
        if self.receipt is None:
            return Gateway.error("Receipt does not exist")
        return types.CallToolResult(content=[], structuredContent=self.receipt)


class OneOperation:
    async def run(self, _mode, _code, _catalog, invoke, *, replay_context=None):
        arguments = deepcopy(DATA)
        capability = f"research.{WRITE}"
        if replay_context is None:
            return await invoke(capability, arguments)
        return await invoke(1, capability, arguments)


@pytest.fixture(autouse=True)
def operator():
    binding = principal.set(Principal("test-operator", frozenset({"operator"})))
    try:
        yield
    finally:
        principal.reset(binding)


def gateway_for(tmp_path, backend, *, surface="code", gated=True):
    config = BACKEND.copy()
    if not gated:
        del config["durable_only_tools"]
    settings = Settings.model_validate({
        "surface": surface,
        "auth": {"tokens": [{"env": "TEST_OPERATOR", "subject": "test-operator", "roles": ["operator"]}]},
        "backends": [config], "durable": {"database": str(tmp_path / "isolated.sqlite")},
    })
    gateway = Gateway(settings)
    backend.config = settings.backends[0]
    gateway.connectors["research"] = backend
    if gateway.workflows is not None:
        gateway.code_surface.executor = OneOperation()
        # The staged test uses neither an installed JS runtime nor its file pins.
        gateway.workflows.contract = lambda: "synthetic-test-contract"
        gateway.workflows.journal.open()
    return gateway


def test_gate_requires_an_allowed_write_and_a_declared_read_only_receipt():
    assert Backend.model_validate(BACKEND).durable_only_tools == {WRITE: RECEIPT}
    for changes in ({"durable_only_tools": [WRITE]}, {"allowed_tools": [RECEIPT]}, {"read_only_tools": []},
                    {"read_only_tools": [WRITE, RECEIPT]}, {"durable_only_tools": {WRITE: "unknown"}}):
        with pytest.raises(ValidationError):
            Backend.model_validate({**BACKEND, **changes})


@pytest.mark.asyncio
async def test_classic_and_generic_code_cannot_call_durable_only_write(tmp_path):
    backend = SyntheticBackend()
    classic = gateway_for(tmp_path, backend, surface="classic")
    for args in (DATA, {**DATA, "durable_approved": True}):
        response = await classic.call_tool(SAVE, args)
        assert response.isError and "durable workflow approval" in response.content[0].text

    code = gateway_for(tmp_path, backend)
    try:
        with pytest.raises(CodeModeError, match="durable workflow approval"):
            await code.code_surface.run("execute", {"code": "async () => {}", "allow_writes": True})
        assert backend.calls == []
    finally:
        code.workflows.journal.close()


@pytest.mark.asyncio
async def test_only_live_bound_approval_dispatches_save_and_repeat_reuses_result(tmp_path):
    backend = SyntheticBackend()
    workflows = gateway_for(tmp_path, backend).workflows
    try:
        request = {"request_id": "workflow-1", "code": "async () => {}"}
        awaiting = await workflows.start(request)
        identifier = awaiting["execution_id"]
        first = awaiting["operations"][0]["approval_id"]
        assert awaiting["status"] == "awaiting_approval" and backend.calls == []
        assert (await workflows.start(request))["execution_id"] == identifier
        with workflows.journal.connect() as db:
            db.execute("UPDATE operations SET expires=0 WHERE workflow_id=?", (identifier,))
        with pytest.raises(CodeModeError, match="expired"):
            await workflows.decide(identifier, "approve", {"approval_id": first})
        refreshed = await workflows.resume(identifier)
        second = refreshed["operations"][0]["approval_id"]
        assert first != second and backend.calls == []
        with pytest.raises(CodeModeError, match="recorded operation"):
            await workflows.decide(identifier, "approve", {"approval_id": first})
        await workflows.decide(identifier, "approve", {"approval_id": second})
        assert backend.calls == []
        completed = await workflows.resume(identifier)
        assert completed["status"] == "completed" and completed["result"] == SAVED
        assert await workflows.resume(identifier) == completed
        assert backend.calls == [(WRITE, DATA)]
    finally:
        workflows.journal.close()


@pytest.mark.asyncio
async def test_uncertain_save_requires_matching_backend_receipt_after_restart(tmp_path):
    backend = SyntheticBackend()
    backend.lose_response = True
    first = gateway_for(tmp_path, backend).workflows
    try:
        state = await first.start({"request_id": "uncertain", "code": "async () => {}"})
        identifier = state["execution_id"]
        await first.decide(identifier, "approve", {"approval_id": state["operations"][0]["approval_id"]})
        state = await first.resume(identifier)
        assert state["status"] == "needs_reconciliation"
        assert backend.calls == [(WRITE, DATA)]
    finally:
        first.journal.close()

    second = gateway_for(tmp_path, backend).workflows
    try:
        assert (await second.resume(identifier))["status"] == "needs_reconciliation"
        assert backend.calls == [(WRITE, DATA)]
        for bad in (None, {"error": "bogus receipt"}):
            backend.receipt = bad
            with pytest.raises(CodeModeError, match="Matching backend receipt unavailable"):
                await second.decide(identifier, "resolve", {"sequence": 1, "result": bad})
            assert second.snapshot(identifier)["operations"][0]["status"] == "uncertain"
        backend.receipt = SAVED.copy()
        with pytest.raises(CodeModeError, match="Supplied result differs"):
            await second.decide(identifier, "resolve", {"sequence": 1, "result": {**SAVED, "receipt_sha256": "c" * 64}})
        assert second.snapshot(identifier)["operations"][0]["status"] == "uncertain"
        resolved = await second.decide(identifier, "resolve", {"sequence": 1, "result": dict(reversed(SAVED.items()))})
        assert resolved["status"] == "ready"
        completed = await second.resume(identifier)
        assert completed["status"] == "completed" and completed["result"] == SAVED
        assert backend.calls == [(WRITE, DATA), *[(RECEIPT, DATA)] * 4]
    finally:
        second.journal.close()


@pytest.mark.asyncio
async def test_empty_gate_preserves_ordinary_code_mode_and_legacy_reconciliation(tmp_path):
    backend = SyntheticBackend()
    gateway = gateway_for(tmp_path, backend, gated=False)
    try:
        result = await gateway.code_surface.run("execute", {"code": "async () => {}", "allow_writes": True})
        assert result == SAVED and backend.calls == [(WRITE, DATA)]
        assert await gateway.workflows.reconciled_result(SAVE, DATA, {"legacy": True}) == '{"legacy":true}'
        assert backend.calls == [(WRITE, DATA)]
    finally:
        gateway.workflows.journal.close()
