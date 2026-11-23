# MCP gateway in EdgeLab

EdgeLab includes the [`mcp_gateway` package](SOURCE.md) and two loopback profiles. The report profile serves verified synthetic research evidence. The draft profile implements authenticated operator approval, receipt reconciliation, and resume for synthetic draft saves. The gateway workflow database, draft database, and EdgeLab scientific journal each have their own purpose and file.

## Install from this checkout

From the EdgeLab repository root in PowerShell, activate the EdgeLab Python environment. The `mcp-gateway` distribution installs the `mcp_gateway` Python module and the `mcp-gateway` command in a separate virtual environment using the bundled dependency lock. The draft profile also requires Node.js 20 or newer and pnpm. Set `MCP_GATEWAY_NODE` if the gateway needs a specific Node.js executable.

```powershell
python -m pip install -e '.[research,mcp]'
$edgeGateway = 'integrations/mcp_gateway'
uv sync --project $edgeGateway --locked --extra test
pnpm --dir "$edgeGateway/code_runtime" install --frozen-lockfile
```

The commands below assume you work from the EdgeLab repository root. Supply the named credentials privately as distinct, randomly generated bearer tokens. Use the same service token in the EdgeLab adapter and its corresponding gateway. The MCP client uses only a role token. Reuse existing token values under the variable names shown below.

## Run the draft profile

In the EdgeLab environment, set `EDGE_DRAFT_SERVICE_TOKEN` and run the draft adapter.

```powershell
New-Item -ItemType Directory -Force 'runtime/mcp_gateway' | Out-Null
python -B -m integrations.mcp_gateway.draft_service `
  --draft-db runtime/mcp_gateway/drafts.sqlite3 --token-env --port 8801
```

In a second terminal, set the same `EDGE_DRAFT_SERVICE_TOKEN` and two distinct client values, `MCP_GATEWAY_EDGELAB_DRAFT_READER_TOKEN` and `MCP_GATEWAY_EDGELAB_DRAFT_OPERATOR_TOKEN`. Start the bundled profile.

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
& ./integrations/mcp_gateway/.venv/Scripts/python.exe -B -m mcp_gateway.cli gateway `
  --config integrations/mcp_gateway/config/edgelab_drafts.yaml
```

Connect an MCP client to `http://127.0.0.1:8800/mcp` using Streamable HTTP. The reader can prepare a preview for `shared-trend` or `spread-level-shift`. An operator uses the workflow HTTP routes at the same address with the operator token. Ordinary MCP calls can read previews, receipts, readiness, and handoffs as their role permits. A draft save requires the durable workflow and exact operator approval.

Use this Code Mode program for the `code` field when creating a workflow. Replace `DRAFT_UUID` with a new UUIDv4. The workflow's outer `request_id` identifies the execution.

```javascript
async () => {
  const preview = await connectors.edgelab.previewDraft({body: {
    template_id: 'cointegration-fixture',
    case: 'shared-trend',
    hypothesis: 'The synthetic series share a common trend.'
  }});
  return connectors.edgelab.saveDraft({body: {
    request_id: 'DRAFT_UUID',
    preview_sha256: preview.preview_sha256,
    draft: preview.draft
  }});
}
```

| Step | Operator action |
| --- | --- |
| Prepare and inspect | `POST /code/workflows` with a unique `request_id` and Code Mode `code` that previews and saves the draft. Inspect `GET /code/workflows/{execution_id}` for the pending operation, arguments, binding, and `approval_id`. |
| Approve and resume | `POST /code/workflows/{execution_id}/approve` with `{"approval_id":"..."}`, followed by `POST /code/workflows/{execution_id}/resume` with `{}`. |
| Reconcile an uncertain save | Retrieve the exact backend receipt for the recorded `saveDraft` arguments using `getDraftReceipt`. `POST /code/workflows/{execution_id}/resolve` with `{"sequence":2,"result":{...}}` using the recorded operation sequence and exact receipt. A successful resolution returns `ready`. Call `/resume` to reuse the receipt. |
| Hand off for research | Use `getDraftHandoff` for the saved draft. Run its fixed synthetic `capstone plan --case ...` command in the EdgeLab research environment with a separate `--journal` path before `plan`. Review the resulting candidates and use the CLI's exact run and protocol approval to execute. |

A matching receipt can recover a committed save whose response was lost. If the receipt is unavailable, the workflow stays in reconciliation. For a verified rejection before a write, prepare a fresh preview and workflow. The workflow journal defaults to `runtime/edgelab-drafts-workflows.sqlite3` relative to this guide. Keep it and the draft database private.

Gateway workflow bindings include the source, dependency locks, and configuration paths. Use the original verified gateway checkout and configuration at their original paths to resume or reconcile a workflow created before the rename.

## Run the report profile

Create a fresh synthetic journal. For example, run the following commands for `shared-trend`, substituting the displayed run ID and protocol digest in the last two commands. Repeat for `spread-level-shift` if you want both reports.

```powershell
python -B -m edgelab.capstone --journal runtime/mcp_gateway/synthetic.sqlite plan --case shared-trend
python -B -m edgelab.capstone --journal runtime/mcp_gateway/synthetic.sqlite execute --run RUN_ID --approve PROTOCOL_SHA256
python -B -m edgelab.capstone --journal runtime/mcp_gateway/synthetic.sqlite report --run RUN_ID
```

Prepare a local manifest with `schema_version` 1 and a `studies` object. Each entry maps a study ID to the report's `run_id` and `bundle.ledger_sha256` (stored as `ledger_sha256` in the manifest). Use the actual fixture case as the study ID for `shared-trend` and `spread-level-shift`.

Set `EDGELAB_READER_SERVICE_TOKEN` in the EdgeLab environment and start the report adapter with the chosen synthetic journal and manifest.

```powershell
python -B -m integrations.mcp_gateway.report_service `
  --journal runtime/mcp_gateway/synthetic.sqlite `
  --manifest runtime/mcp_gateway/studies.json --token-env --port 8799
```

In a separate terminal, set the same `EDGELAB_READER_SERVICE_TOKEN` and a distinct `MCP_GATEWAY_EDGELAB_READER_TOKEN`. Start the report gateway.

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
& ./integrations/mcp_gateway/.venv/Scripts/python.exe -B -m mcp_gateway.cli gateway `
  --config integrations/mcp_gateway/config/edgelab_research.yaml
```

Connect the MCP client to `http://127.0.0.1:8798/mcp` with the reader token. Use `edgelab__listRuns` and `edgelab__getRunReport` to compare the recorded verdicts, Engle–Granger and CUSUM p-values, Critic rules, and evidence hashes. The results are in-sample synthetic findings.

## Verify locally

The tests generate their own temporary tokens and stores, launch both loopback profiles, and use real MCP clients. The report check builds fresh fixtures through the EdgeLab CLI. The draft check simulates a lost response after a successful save and verifies exact receipt recovery without replaying the write.

```powershell
$env:EDGELAB_TEST_PYTHON = (Get-Command python).Source
& ./integrations/mcp_gateway/.venv/Scripts/python.exe -B -m pytest `
  integrations/mcp_gateway/tests -q
```
