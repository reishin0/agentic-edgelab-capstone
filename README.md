# Agentic EdgeLab

Agentic EdgeLab is a local research assistant for quantitative analysts who need to test a relationship hypothesis and show how they reached a verdict. Its reference workflow tests whether two series share a stable long-run relationship. The researcher chooses the hypothesis, eligible observations and protocol. After exact approval, EdgeLab runs the approved steps and produces a verdict, evidence-linked report and reproducibility bundle.

## Architecture

1. LangChain retrieves reviewed method notes. A bounded Tree-of-Thought search ranks eligible protocol branches before statistical execution. Optional Planner advice receives a blinded task and allowed choices.
2. The **LangGraph planning command** saves the candidates and exits. A separate execution command checks the operator's exact run and protocol approval, records each milestone in SQLite and dispatches deterministic statistical tools.
3. The **Critic** reviews evidence and flags conflicts. Gate rules and the controller determine whether the workflow continues, stops, requests revision or remains unresolved.
4. The **Reporter** explains the recorded verdict from verified evidence. Optional CrewAI role tasks advise inside the workflow. The local MCP interface exposes public methods and the recorded planning trace.

The tools use statsmodels for ADF and KPSS, bidirectional Engle–Granger tests and stability diagnostics. The project includes local method notes, optional LM Studio and OpenRouter model advice, and optional Pinecone retrieval. See [architecture](ARCHITECTURE.rst), [message contracts](CONTRACTS.rst) and [dashboard settings](DEPLOYMENT.rst).

The bundled [`mcp_gateway` integration](integrations/mcp_gateway/README.md) serves approved synthetic reports and supports authenticated approval, receipt reconciliation, and resume for synthetic draft saves. Scientific execution requires its own exact run and protocol approval in the EdgeLab CLI.

## Run the synthetic example

From the repository root, create a clean environment with Python 3.12. Use its Python interpreter for the commands below.

```sh
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on macOS or Linux, or `.\.venv\Scripts\Activate.ps1` in PowerShell. The minimal fixture requires only the standard library.

```sh
python -B -m edgelab
python -B -m edgelab --approve-fixture --fixture support
```

For the integrated LangGraph and statistical tool demo, install the research dependencies. Use a new journal and copy the selected run ID and protocol SHA-256 from the planning output. The example uses generated synthetic data and needs zero provider credentials.

```sh
python -m pip install -e '.[research]'
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite plan --case shared-trend --no-model
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite execute --run RUN_ID --approve PROTOCOL_SHA256
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite report --run RUN_ID
```

The [synthetic example output](examples/synthetic-shared-trend.json) shows the milestone evidence and final verdict from this workflow.

Start the isolated dashboard with `python -B -m edgelab.dashboard --demo` and open the printed loopback URL.

Select a saved run to see each role's recorded decisions on **Overview** and
its approved A/B series and statistical results on **Evidence**. The optional
[Cointegration Test console](INTEGRATION.rst#cointegration-test-console)
prepares a selected pair in a dedicated journal and requires the exact
protocol approval before execution.

See [deployment](DEPLOYMENT.rst) for test dependencies and telemetry settings. Run the core, discovery, and workflow-comparison suites from the repository root.

```sh
python -B -m unittest discover -s tests
python -B -m unittest discover -s research/pair_discovery
python -B -m unittest discover -s research/workflow_comparison
```

## Use Ornith in LM Studio

Install the EdgeLab research extra, then copy [providers.env.example](providers.env.example)
to a new private `providers.env` file. Set `LM_STUDIO_BASE_URL` to the
loopback API address, including `/v1`. Load Ornith with the exact instance ID
`ornith-1.5-35b-a3b-q4-k-m`. Set `LM_STUDIO_PLANNER_MODEL` to that ID and
`LM_STUDIO_PLANNER_PROFILE=ornith`. Set `LM_STUDIO_AUTO_USE=1` to make it
the default for new plans. Set `LM_STUDIO_API_KEY` if the server requires it.

EdgeLab uses LM Studio's model chat template and asks for one allowed choice as
a JSON object. For `ornith-1.5-35b-a3b-q4-k-m`, the Planner profile sets
temperature 0.6, top-p 0.95, top-k 20, reasoning off, and a 128-token output
limit. These settings apply to EdgeLab's Planner requests.

```sh
python -B -m edgelab.capstone --settings-file providers.env --journal runtime/ornith-demo.sqlite plan --case shared-trend
```

Add `--no-model` to disable model advice. On the regular dashboard,
**Settings** offers diagnostics for LM Studio, the OpenRouter catalog and
Pinecone. Bundled notes are the default evidence source. Pinecone planning
requires verified ADF, KPSS, and Engle–Granger records by exact ID. The
dashboard reports semantic ranking separately. See [deployment](DEPLOYMENT.rst).

## Evaluation

| Study | Observed result |
| --- | --- |
| [Offline contracts](CONTRACTS.rst) | Tested authorization denials, blocking failures and evidence tampering against fixed regression criteria. |
| [Matched-budget workflow comparison](research/workflow_comparison/REPORT.md) | Over 24 blinded cases, deterministic 100%, single agent 28.13% and CrewAI roles 30.21%. The observed 2.08 percentage-point gain over one agent fell below the 10-point threshold. The deterministic baseline had direct access to the encoded rules. |
| [Independent stability validation](research/bootstrap_validation_20261114/REPORT.md) | 18,000 synthetic trials, each evaluated in three arms. The experimental bootstrap passed six stable-scenario gates and seven of twelve negative-scenario gates. EdgeLab retained CUSUM. [Aggregate results](research/bootstrap_validation_20261114/aggregate-results.json) |
| [Point-in-time discovery](research/pair_discovery/DISCOVERY_PROTOCOL_DRAFT.md) | A development coverage check retained all 4,784 frozen pairs as unresolved because formation histories were incomplete. The market holdout is locked and untested. [Coverage summary](examples/development-coverage-summary.json) |

Use the [workflow comparison protocol](research/workflow_comparison/PROTOCOL.rst) for the replayable stub demonstration and instructions for a separately configured local-model run. The [bootstrap protocol](research/bootstrap_validation_20261114/PROTOCOL.md) includes a self-check, the full synthetic study command and ledger verification.

## Repository map

- `edgelab/` contains the controller, role adapters, statistical tools, evidence contracts, journal, reporting, MCP and dashboard.
- `integrations/mcp_gateway/` contains the report and draft adapters and the bundled `mcp_gateway` project with its loopback profiles.
- `tests/` contains the core offline tests and synthetic fixtures.
- `research/` contains the workflow benchmark, independent bootstrap experiment and the development and holdout protocol code.

The published materials support research and reproducibility. Market datasets, local journals and credentials stay outside the repository. The controller gates all numerical execution to the approved in-sample protocol. The development and holdout research code has no order execution path.
