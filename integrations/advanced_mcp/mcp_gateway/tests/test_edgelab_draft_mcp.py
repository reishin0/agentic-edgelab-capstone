"""Exercise authenticated draft recovery through the bundled loopback services."""

import asyncio
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import yaml

from mcp_gateway.code_mode import CodeModeError, node_executable


GATEWAY_ROOT = Path(__file__).resolve().parents[1]
EDGELAB_ROOT = GATEWAY_ROOT.parents[2]
PROFILE = GATEWAY_ROOT / "config/edgelab_drafts.yaml"
READER = "ADVANCED_MCP_EDGELAB_DRAFT_READER_TOKEN"
OPERATOR = "ADVANCED_MCP_EDGELAB_DRAFT_OPERATOR_TOKEN"
SERVICE = "EDGE_DRAFT_SERVICE_TOKEN"
DRAFT = {"template_id": "cointegration-fixture", "case": "shared-trend",
         "hypothesis": "The synthetic series share a common trend."}

# Inject response loss after one real backend commit. The production connector
# and adapter have no fault switch. Count dispatches so backend idempotency
# cannot hide a replay by the gateway.
FAULT_GATEWAY = """
from pathlib import Path
import sys
from mcp_gateway.openapi_connector import OpenAPIConnector

fault_request = sys.argv.pop(1)
save_calls = Path(sys.argv.pop(1))
request = OpenAPIConnector.request

async def lose_response(self, operation, *args, **kwargs):
    is_save = operation == 'call_tool' and args[0] == 'saveDraft'
    if is_save:
        request_id = args[1]['body']['request_id']
        with save_calls.open('a', encoding='ascii') as stream:
            stream.write(request_id + '\\n')
    reply = await request(self, operation, *args, **kwargs)
    if is_save and not reply.isError and request_id == fault_request:
        raise OSError('Synthetic response lost after a committed save')
    return reply

OpenAPIConnector.request = lose_response
from mcp_gateway.cli import main
main()
"""


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def free_ports():
    with socket.socket() as gateway, socket.socket() as adapter:
        gateway.bind(("127.0.0.1", 0))
        adapter.bind(("127.0.0.1", 0))
        return gateway.getsockname()[1], adapter.getsockname()[1]


@contextmanager
def running(command, folder, env, log_name):
    log = folder / log_name
    with log.open("wb") as output:
        child = subprocess.Popen(
            command, cwd=folder, env=env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            yield child, log
        finally:
            if child.poll() is None:
                if os.name == "nt":
                    # The Windows venv launcher can own a separate Python child.
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                   capture_output=True, timeout=10, check=False)
                else:
                    child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


def wait_for_health(http, url, service, *, path="/healthz", headers=None):
    child, log = service
    deadline = monotonic() + 15
    while monotonic() < deadline:
        if child.poll() is not None:
            pytest.fail(f"Loopback service exited: {log.read_text(encoding='utf-8', errors='replace')}")
        try:
            http.get(url + path, headers=headers, timeout=1).raise_for_status()
            return
        except (httpx.ConnectError, httpx.TimeoutException):
            sleep(0.1)
    pytest.fail(f"Loopback service did not start: {log.read_text(encoding='utf-8', errors='replace')}")


async def execute_code(url, token, code, **arguments):
    async with httpx.AsyncClient(headers=bearer(token), timeout=10, trust_env=False) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                return await client.call_tool("execute", {"code": code, **arguments})


def workflow_code(request):
    return "async () => { const draft = " + json.dumps(request) + "; " + (
        "const preview = await connectors.edgelab.previewDraft({body: draft.draft}); "
        "draft.preview_sha256 = preview.preview_sha256; "
        "return connectors.edgelab.saveDraft({body: draft}); }"
    )


def response_body(response):
    response.raise_for_status()
    return response.json()


def start_review(http, url, token, request, *, workflow_request_id=None):
    state = response_body(http.post(url + "/code/workflows", headers=bearer(token), json={
        "request_id": workflow_request_id or request["request_id"], "code": workflow_code(request),
    }))
    assert state["status"] == "awaiting_approval"
    preview, save = state["operations"]
    assert preview["status"] == "completed" and save["status"] == "pending"
    assert save["arguments"]["body"] == request
    return url + "/code/workflows/" + state["execution_id"], save


def test_authenticated_draft_restarts_and_reconciles_without_replay(tmp_path):
    from edgelab.journal import Journal
    from edgelab.methods import local_notes
    from edgelab.planning import search
    from edgelab.research import ResearchService

    try:
        node_executable()
    except CodeModeError:
        pytest.skip("Node.js 20+ is required for the live draft acceptance test")
    quickjs = GATEWAY_ROOT / "code_runtime/node_modules/quickjs-emscripten/package.json"
    if not quickjs.is_file():
        pytest.skip("Install the gateway's lockfile-pinned QuickJS runtime to run this test")

    journal_path = tmp_path / "scientific.sqlite3"
    journal = Journal(journal_path)
    run_id = str(uuid4())
    plan = ResearchService(journal).record_plan(run_id, DRAFT["case"], search(local_notes("cointegration")), "local")
    assert plan["status"] == "awaiting_approval"
    before = sha256(journal_path.read_bytes()).hexdigest()

    gateway_port, adapter_port = free_ports()
    gateway, adapter = (f"http://127.0.0.1:{port}" for port in (gateway_port, adapter_port))
    drafts = adapter + "/v1/drafts"
    profile = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    profile.update(port=gateway_port, allowed_origins=[gateway])
    profile["durable"]["database"] = str(tmp_path / "workflows.sqlite3")
    backend, = profile["backends"]
    backend.update(url=adapter + "/v1", spec_file=str(PROFILE.parent / backend["spec_file"]))
    config = tmp_path / "profile.yaml"
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")

    environment = {name: os.environ[name] for name in (
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME", "ADVANCED_MCP_NODE",
    ) if name in os.environ}
    environment["PYTHONPATH"] = os.pathsep.join(map(str, (GATEWAY_ROOT / "src", EDGELAB_ROOT)))
    tokens = {name: secrets.token_urlsafe(32) for name in (READER, OPERATOR, SERVICE)}
    fault = str(uuid4())
    save_calls = tmp_path / "save-calls.txt"
    gateway_args = ["gateway", "--config", str(config)]
    gateway_command = [sys.executable, "-B", "-m", "mcp_gateway.cli", *gateway_args]
    fault_command = [sys.executable, "-B", "-c", FAULT_GATEWAY, fault, str(save_calls), *gateway_args]
    gateway_env = {**environment, **tokens, "PYTHON_DOTENV_DISABLED": "1"}
    draft_db = tmp_path / "drafts.sqlite3"
    adapter_command = [sys.executable, "-B", "-m", "integrations.advanced_mcp.draft_service",
                       "--draft-db", str(draft_db), "--token-env", "--port", str(adapter_port)]
    adapter_env = {**environment, SERVICE: tokens[SERVICE]}

    with (running(adapter_command, tmp_path, adapter_env, "adapter.log") as backend_service,
          httpx.Client(timeout=15, trust_env=False) as http):
        wait_for_health(http, adapter, backend_service)

        def post(url, value, credential):
            return http.post(url, json=value, headers=bearer(tokens[credential]))

        assert http.post(drafts + "/preview", json=DRAFT).status_code == 401
        preview = response_body(post(drafts + "/preview", DRAFT, SERVICE))
        regular = {"request_id": str(uuid4()), "preview_sha256": preview["preview_sha256"], "draft": DRAFT}
        failure = {**regular, "request_id": fault}

        with running(gateway_command, tmp_path, gateway_env, "gateway.log") as first:
            wait_for_health(http, gateway, first)
            assert http.get(gateway + "/healthz").json()["service"] == "mcp_gateway"
            assert http.post(gateway + "/mcp", json={}).status_code == 401
            assert http.post(gateway + "/code/workflows", json={}).status_code == 401
            read_code = "async () => connectors.edgelab.previewDraft({body: " + json.dumps(DRAFT) + "})"
            result = asyncio.run(execute_code(gateway, tokens[READER], read_code))
            assert not result.isError and result.structuredContent["result"] == preview
            save_code = "async () => connectors.edgelab.saveDraft({body: " + json.dumps(regular) + "})"
            assert asyncio.run(execute_code(gateway, tokens[OPERATOR], save_code, allow_writes=True)).isError
            assert post(drafts + "/receipt", regular, SERVICE).status_code == 404
            assert not draft_db.exists()

            workflow, save = start_review(http, gateway, tokens[OPERATOR], regular)
            approval = {"approval_id": save["approval_id"]}
            assert post(workflow + "/approve", approval, READER).status_code == 400
            assert post(drafts + "/receipt", regular, SERVICE).status_code == 404
            assert response_body(post(workflow + "/approve", approval, OPERATOR))["status"] == "ready"
            assert not draft_db.exists()

        with running(fault_command, tmp_path, gateway_env, "gateway-restart.log") as second:
            wait_for_health(http, gateway, second)
            completed = response_body(post(workflow + "/resume", {}, OPERATOR))
            receipt = response_body(post(drafts + "/receipt", regular, SERVICE))
            assert completed["status"] == "completed" and completed["result"]["result"] == receipt
            assert response_body(post(workflow + "/resume", {}, OPERATOR))["result"] == completed["result"]

            args = {name: regular[name] for name in ("request_id", "preview_sha256")}
            review_code = ("async () => { const args = " + json.dumps(args) + "; return {"
                           "readiness: await connectors.edgelab.checkDraftReadiness(args), "
                           "handoff: await connectors.edgelab.getDraftHandoff(args)}; }")
            review = asyncio.run(execute_code(gateway, tokens[OPERATOR], review_code))
            assert not review.isError
            result = review.structuredContent["result"]
            assert result["readiness"]["status"] == "ready_for_review"
            assert result["handoff"]["argv"] == ["python", "-B", "-m", "edgelab.capstone", "plan", "--case", DRAFT["case"]]

            workflow, save = start_review(http, gateway, tokens[OPERATOR], failure)
            assert response_body(post(workflow + "/approve", {"approval_id": save["approval_id"]}, OPERATOR))["status"] == "ready"
            assert response_body(post(workflow + "/resume", {}, OPERATOR))["status"] == "needs_reconciliation"
            recovered = response_body(post(drafts + "/receipt", failure, SERVICE))
            resolution = {"sequence": save["sequence"], "result": recovered}
            assert post(workflow + "/resolve", resolution, READER).status_code == 400
            assert post(workflow + "/resolve", {**resolution, "result": {"fabricated": True}}, OPERATOR).status_code == 400
            assert response_body(post(workflow + "/resume", {}, OPERATOR))["status"] == "needs_reconciliation"
            assert response_body(post(workflow + "/resolve", resolution, OPERATOR))["status"] == "ready"
            completed = response_body(post(workflow + "/resume", {}, OPERATOR))
            assert completed["status"] == "completed" and completed["result"]["result"] == recovered

            changed_draft = {**DRAFT, "hypothesis": "A different draft reuses the saved request ID."}
            changed_preview = response_body(post(drafts + "/preview", changed_draft, SERVICE))
            conflict = {**regular, "preview_sha256": changed_preview["preview_sha256"], "draft": changed_draft}
            workflow, save = start_review(http, gateway, tokens[OPERATOR], conflict, workflow_request_id=str(uuid4()))
            assert response_body(post(workflow + "/approve", {"approval_id": save["approval_id"]}, OPERATOR))["status"] == "ready"
            refused = response_body(post(workflow + "/resume", {}, OPERATOR))
            assert refused["status"] == "failed" and refused["operations"][-1]["status"] == "not_saved"
            assert response_body(post(workflow + "/resume", {}, OPERATOR))["status"] == "failed"
            assert response_body(post(drafts + "/receipt", regular, SERVICE)) == receipt

    dispatched = save_calls.read_text(encoding="ascii").splitlines()
    assert dispatched == [regular["request_id"], failure["request_id"], conflict["request_id"]]
    assert sha256(journal_path.read_bytes()).hexdigest() == before
    assert [event["key"] for event in journal.events(run_id)] == ["plan"]
