"""Regression checks for the staged EdgeLab synthetic draft gateway profile."""

from pathlib import Path

import httpx
import jsonschema
import pytest

from mcp_gateway.auth import Principal, authorized
from mcp_gateway.backend import BackendUnavailable, OperationNotApplied
from mcp_gateway.config import load_config
from mcp_gateway.openapi_connector import OpenAPIConnector, compile_operations

PROFILE = Path(__file__).resolve().parents[1] / "config" / "edgelab_drafts.yaml"
REQUEST_ID = "12345678-1234-4abc-89ab-1234567890ab"
DRAFT = {
    "template_id": "cointegration-fixture",
    "case": "shared-trend",
    "hypothesis": "A fixed synthetic series may share a trend.",
}


def test_profile_sets_read_and_operator_scopes_and_requires_durable_save():
    config = load_config(PROFILE)
    backend, = config.backends
    operations = compile_operations(backend)
    assert (config.host, config.port, config.surface) == ("127.0.0.1", 8800, "code")
    assert Path(config.durable.database).name == "edgelab-drafts-workflows.sqlite3"
    assert backend.url == "http://127.0.0.1:8801/v1"
    assert backend.token_env == "EDGE_DRAFT_SERVICE_TOKEN"
    assert backend.durable_only_tools == {"saveDraft": "getDraftReceipt"}
    assert set(backend.read_only_tools) == set(operations) - {"saveDraft"}
    assert {name: (op.method, op.path) for name, op in operations.items()} == {
        "previewDraft": ("POST", "/drafts/preview"),
        "saveDraft": ("POST", "/drafts"),
        "getDraftReceipt": ("POST", "/drafts/receipt"),
        "checkDraftReadiness": ("GET", "/drafts/{request_id}/readiness"),
        "getDraftHandoff": ("GET", "/drafts/{request_id}/handoff"),
    }

    reader, operator = [
        Principal(token.subject, frozenset(token.roles))
        for token in config.auth.tokens
    ]
    assert reader.subject != operator.subject
    assert "operator" in operator.roles
    assert {name for name in operations if authorized(reader, backend, name)} == {"previewDraft"}
    assert all(authorized(operator, backend, name) for name in operations)


def test_preview_accepts_only_fixed_synthetic_drafts_and_recovery_matches_original_save():
    backend, = load_config(PROFILE).backends
    operations = compile_operations(backend)
    preview = operations["previewDraft"]
    jsonschema.validate({"body": DRAFT}, preview.input_schema)
    for draft in (
        {**DRAFT, "case": "market-holdout"},
        {**DRAFT, "hypothesis": ""},
        {**DRAFT, "hypothesis": "a" * 1001},
        {**DRAFT, "source_path": "unapproved-input.csv"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"body": draft}, preview.input_schema)

    save, receipt = operations["saveDraft"], operations["getDraftReceipt"]
    assert save.input_schema == receipt.input_schema
    assert save.output_schema == receipt.output_schema
    save_body = {"request_id": REQUEST_ID, "preview_sha256": "0" * 64, "draft": DRAFT}
    jsonschema.validate({"body": save_body}, save.input_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"body": {**save_body, "request_id": REQUEST_ID.replace("-4abc", "-5abc")}}, save.input_schema)


def test_handoff_schema_has_one_fixed_local_planning_command():
    backend, = load_config(PROFILE).backends
    handoff = compile_operations(backend)["getDraftHandoff"]
    binding = {"request_id": REQUEST_ID, "preview_sha256": "0" * 64}
    jsonschema.validate(binding, handoff.input_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"request_id": REQUEST_ID}, handoff.input_schema)

    response = {
        "schema_version": 1, "status": "ready_for_review", **binding,
        "argv": ["python", "-B", "-m", "edgelab.capstone", "plan", "--case", "shared-trend"],
    }
    jsonschema.validate(response, handoff.output_schema)
    for argv in (response["argv"] + ["--approve"], [*response["argv"][:4], "execute", *response["argv"][5:]]):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**response, "argv": argv}, handoff.output_schema)


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "status", "payload", "error"), [
    ("saveDraft", 200, {"status": "saved"}, BackendUnavailable),
    ("getDraftReceipt", 200, {"status": "saved"}, BackendUnavailable),
    ("saveDraft", 200, {"schema_version": 1, "status": "saved", "request_id": REQUEST_ID,
                        "preview_sha256": "0" * 64, "receipt_sha256": "b" * 64}, BackendUnavailable),
    ("saveDraft", 409, {"error": "Draft conflict"}, BackendUnavailable),
    ("saveDraft", 409, {"error": "Draft conflict", "status": "not_saved", "request_id": REQUEST_ID,
                        "preview_sha256": "a" * 64}, BackendUnavailable),
    ("saveDraft", 409, {"error": "Draft conflict", "status": "not_saved", "request_id": REQUEST_ID,
                        "preview_sha256": "0" * 64}, OperationNotApplied),
])
async def test_draft_connector_requires_bound_authenticated_response(monkeypatch, operation, status, payload, error):
    backend, = load_config(PROFILE).backends
    token = "fixture-credential-not-a-real-service-token"
    monkeypatch.setenv(backend.token_env, token)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    connector = OpenAPIConnector(backend, httpx.MockTransport(respond))
    connector.start()
    try:
        request = {"request_id": REQUEST_ID, "preview_sha256": "0" * 64, "draft": DRAFT}
        with pytest.raises(error):
            await connector.request("call_tool", operation, {"body": request})
    finally:
        await connector.close()
    call, = calls
    assert call.headers["Authorization"] == f"Bearer {token}"
