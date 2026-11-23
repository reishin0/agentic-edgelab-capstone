"""Verify the bundled read-only research profile through real MCP clients."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import yaml

from test_edgelab_draft_mcp import bearer, free_ports, running, wait_for_health


GATEWAY_ROOT = Path(__file__).resolve().parents[1]
EDGELAB_ROOT = GATEWAY_ROOT.parents[1]
PROFILE = GATEWAY_ROOT / "config/edgelab_research.yaml"
STUDIES = {"shared-trend": "continue", "spread-level-shift": "stop"}
READER = "MCP_GATEWAY_EDGELAB_READER_TOKEN"
SERVICE = "EDGELAB_READER_SERVICE_TOKEN"
UNSCOPED = "MCP_GATEWAY_EDGELAB_QA_UNSCOPED_TOKEN"


def scientific_reports(python, journal, environment):
    settings = journal.with_name("offline-providers.env")
    settings.touch(exist_ok=False)

    def capstone(*arguments):
        completed = subprocess.run(
            [python, "-B", "-m", "edgelab.capstone", "--journal", str(journal),
             "--settings-file", str(settings), *arguments],
            cwd=EDGELAB_ROOT, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=40, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    reports = {}
    for study, verdict in STUDIES.items():
        plan = capstone("plan", "--case", study)
        assert plan["status"] == "awaiting_approval"
        run = plan["run_id"]
        capstone("execute", "--run", run, "--approve", plan["candidates"][0]["digest"])
        report = capstone("report", "--run", run)
        assert report["report"]["verdict"] == verdict
        reports[study] = report
    return reports


def assert_projection(study, projection, canonical):
    report, bundle = canonical["report"], canonical["bundle"]
    events = {event["key"]: event for event in bundle["events"]}
    review_rules = {
        key.removeprefix("review:"): event["value"]["rule_id"]
        for key, event in events.items() if key.startswith("review:")
    }
    eg = events["review:engle_granger"]["value"]["details"]
    stability = events["review:stability"]["value"]["details"]
    assert projection == {
        "schema_version": 1,
        "study_id": study,
        "run_id": canonical["run_id"],
        "verdict": report["verdict"],
        "reason": report["reason"],
        "ledger_sha256": bundle["ledger_sha256"],
        "scope": "synthetic in-sample",
        "holdout": "locked and untested",
        "window": report["window"],
        "diagnostics": {
            "engle_granger_a_on_b_p": eg["a_on_b"],
            "engle_granger_b_on_a_p": eg["b_on_a"],
            **{key: stability.get(key) for key in ("cusum_p", "ecm_loading", "ou_half_life_observations")},
        },
        "evidence": [
            {"event_key": key, "sha256": event["sha256"], "critic_rule": review_rules.get(key.partition(":")[2])}
            for key, event in events.items()
            if key.startswith(("result:", "review:")) or key == "verdict"
        ],
    }


async def assert_mcp_scope(url, token, reports=None):
    async with (
        asyncio.timeout(20),
        httpx.AsyncClient(headers=bearer(token), timeout=8, trust_env=False) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as client,
    ):
        initialization = await client.initialize()
        assert initialization.serverInfo.name == "mcp_gateway"
        names = {tool.name for tool in (await client.list_tools()).tools}
        if reports is None:
            assert names == {"gateway_search_tools"}
            assert (await client.call_tool("edgelab__listRuns", {})).isError
            assert (await client.call_tool("edgelab__getRunReport", {"study_id": "shared-trend"})).isError
            return

        assert names == {"gateway_search_tools", "edgelab__listRuns", "edgelab__getRunReport"}
        listing = await client.call_tool("edgelab__listRuns", {})
        assert not listing.isError and listing.structuredContent is not None
        assert listing.structuredContent == {
            "schema_version": 1,
            "runs": sorted([
                {"study_id": study, "run_id": report["run_id"], "verdict": STUDIES[study],
                 "ledger_sha256": report["bundle"]["ledger_sha256"]}
                for study, report in reports.items()
            ], key=lambda row: row["study_id"]),
        }
        for study, canonical in reports.items():
            result = await client.call_tool("edgelab__getRunReport", {"study_id": study})
            assert not result.isError and result.structuredContent is not None
            assert_projection(study, result.structuredContent, canonical)
        assert (await client.call_tool("edgelab__getRunReport", {"study_id": "unpublished-study"})).isError
        assert (await client.call_tool("edgelab__approve", {})).isError


def test_authenticated_research_profile_serves_canonical_reports_read_only(tmp_path):
    python = os.environ.get("EDGELAB_TEST_PYTHON") or sys.executable
    environment = {name: os.environ[name] for name in (
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME",
    ) if name in os.environ}
    environment.update(
        PYTHONPATH=os.pathsep.join(map(str, (GATEWAY_ROOT / "src", EDGELAB_ROOT))),
        PYTHON_DOTENV_DISABLED="1", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false",
        OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
    )
    journal = tmp_path / "scientific.sqlite3"
    reports = scientific_reports(python, journal, environment)
    before = sha256(journal.read_bytes()).digest()
    manifest = tmp_path / "studies.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "studies": {
            study: {"run_id": packet["run_id"], "ledger_sha256": packet["bundle"]["ledger_sha256"]}
            for study, packet in reports.items()
        },
    }), encoding="utf-8")

    gateway_port, adapter_port = free_ports()
    gateway, adapter = (f"http://127.0.0.1:{port}" for port in (gateway_port, adapter_port))
    profile = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    profile.update(port=gateway_port, allowed_origins=[gateway])
    backend, = profile["backends"]
    backend.update(url=adapter + "/v1", spec_file=str(PROFILE.parent / backend["spec_file"]))
    profile["auth"]["tokens"].append({"env": UNSCOPED, "subject": "test-unscoped", "roles": ["unscoped"]})
    config = tmp_path / "profile.yaml"
    config.write_text(yaml.safe_dump(profile), encoding="utf-8")

    tokens = {name: secrets.token_urlsafe(32) for name in (READER, SERVICE, UNSCOPED)}
    gateway_command = [sys.executable, "-B", "-m", "mcp_gateway.cli", "gateway", "--config", str(config)]
    adapter_command = [
        python, "-B", "-m", "integrations.mcp_gateway.report_service", "--journal", str(journal),
        "--manifest", str(manifest), "--token-env", "--port", str(adapter_port),
    ]
    with (
        running(adapter_command, tmp_path, {**environment, SERVICE: tokens[SERVICE]}, "report.log") as adapter_proc,
        running(gateway_command, tmp_path, {**environment, **tokens}, "gateway.log") as gateway_proc,
        httpx.Client(timeout=5, trust_env=False) as http,
    ):
        wait_for_health(http, adapter, adapter_proc, path="/v1/runs", headers=bearer(tokens[SERVICE]))
        wait_for_health(http, gateway, gateway_proc)
        assert http.post(gateway + "/mcp", json={}).status_code == 401
        assert http.get(adapter + "/v1/runs", headers=bearer(tokens[READER])).status_code == 401
        assert http.post(adapter + "/v1/runs", headers=bearer(tokens[SERVICE])).status_code == 405
        asyncio.run(assert_mcp_scope(gateway + "/mcp", tokens[READER], reports))
        asyncio.run(assert_mcp_scope(gateway + "/mcp", tokens[UNSCOPED]))

    assert sha256(journal.read_bytes()).digest() == before
