# EdgeLab reports through Advanced MCP

This checkout includes a loopback adapter for comparing approved synthetic EdgeLab studies in an MCP client. The adapter verifies the saved ledger and returns the verdict, selected statistical diagnostics, Critic rules, and evidence hashes. Its profile exposes two read operations to a dedicated role.

## Prepare synthetic reports

Run from the EdgeLab checkout with its research environment active. Use a fresh local journal and repeat the planning, approval, and reporting steps for `shared-trend` and `spread-level-shift`.

```sh
python -m pip install -e '.[research]'
python -B -m edgelab.capstone --journal runtime/mcp-demo.sqlite plan --case shared-trend
python -B -m edgelab.capstone --journal runtime/mcp-demo.sqlite execute --run RUN_ID --approve PROTOCOL_SHA256
python -B -m edgelab.capstone --journal runtime/mcp-demo.sqlite report --run RUN_ID
```

Create a local JSON manifest with `schema_version` set to `1` and a `studies` object. Under each study name, put the `run_id` and `bundle.ledger_sha256` from its verified report. Check the plan’s saved case before assigning the label. This prototype verifies the run and ledger digests and relies on the manifest author for the label. Keep the journal and manifest in `runtime/`.

Start the adapter from the checkout after setting a dedicated `EDGELAB_READER_SERVICE_TOKEN` through private process configuration.

```sh
python -B -m integrations.advanced_mcp.report_service --journal runtime/mcp-demo.sqlite --manifest runtime/mcp-studies.json --token-env --port 8799
```

## Connect the gateway

The gateway runs in a separate Advanced MCP checkout with native OpenAPI connector support. Place [the sample profile](edgelab_research.yaml) and [API definition](edgelab.openapi.json) together in its configuration directory. Provide the same service token and a distinct `ADVANCED_MCP_EDGELAB_READER_TOKEN` in the gateway’s private environment, then start its classic profile.

```sh
python -B -m advanced_mcp.cli gateway --config config/edgelab_research.yaml
```

Connect your MCP client to `http://127.0.0.1:8798/mcp` using the reader token. Ask it to list the two reports and compare their Engle–Granger results, CUSUM p-values, and Critic evidence hashes. The `continue` or `stop` verdict applies to the saved synthetic run. The market holdout stays locked.

The source checkout also contains an experimental draft adapter and its isolated tests. The sample gateway profile covers report reads. The draft workflow still needs save receipt validation, rejection handling, and preview binding fixes before release as a gateway profile.
