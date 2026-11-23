# Agentic EdgeLab

Agentic EdgeLab is a local research assistant for quantitative analysts who need to test a relationship hypothesis and show how they reached a verdict. Its reference workflow tests whether two series share a stable long-run relationship. The researcher chooses the hypothesis, eligible observations and protocol. After exact approval, EdgeLab runs the approved steps and produces a verdict, evidence-linked report and reproducibility bundle.

## Architecture

1. A **Planner** receives the blinded question and approved method context. LangChain retrieval returns checked method notes. A bounded Tree-of-Thought search ranks eligible protocol branches before the Planner sees sample values.
2. The **LangGraph controller** pauses for the local operator to approve the exact protocol digest. It checks scope, journals every transition in SQLite and dispatches deterministic statistical tools.
3. The **Critic** reviews evidence and flags conflicts. Gate rules and the controller determine whether the workflow continues, stops, requests revision or remains unresolved.
4. The **Reporter** explains the recorded verdict from verified evidence. Optional CrewAI role tasks advise inside the workflow. The local MCP interface and dashboard provide read-only inspection by default. The optional [Waitlist pair console](INTEGRATION.rst#waitlist-pair-console) runs a selected pair after exact local approval.

The tools use statsmodels for ADF and KPSS, bidirectional Engle–Granger tests and stability diagnostics. The project includes local method notes, optional LM Studio and OpenRouter model advice, and optional Pinecone retrieval. See [architecture](ARCHITECTURE.rst), [message contracts](CONTRACTS.rst) and [dashboard settings](DEPLOYMENT.rst).

The bundled [Advanced MCP integration](integrations/advanced_mcp/README.md) serves approved synthetic reports and supports authenticated approval, receipt reconciliation, and resume for synthetic draft saves. Scientific execution requires its own exact run and protocol approval in the EdgeLab CLI.

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
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite plan --case shared-trend
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite execute --run RUN_ID --approve PROTOCOL_SHA256
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite report --run RUN_ID
```

The [synthetic example output](examples/synthetic-shared-trend.json) shows the milestone evidence and final verdict from this workflow.

To exercise the offline suite with the optional components installed, see the dependency and telemetry setup in [deployment](DEPLOYMENT.rst) and run `python -B -m unittest discover -s tests`. For the discovery tests, run `python -B -m unittest discover -s research/pair_discovery` from the repository root. The repository includes the evaluation methods, synthetic inputs in the test harnesses and result summaries.

## Use Ornith in LM Studio

Install the EdgeLab research extra, then copy [providers.env.example](providers.env.example)
to a new private `providers.env` file. Set `LM_STUDIO_BASE_URL` to your
local LM Studio API address, including `/v1`. Match `LM_STUDIO_PLANNER_MODEL`
to the exact loaded instance ID and set `LM_STUDIO_PLANNER_PROFILE=ornith`.
Set `LM_STUDIO_AUTO_USE=1` to use that model by default for new CLI plans.

EdgeLab uses LM Studio's model chat template and asks for one allowed choice as
a JSON object. For `ornith-1.5-35b-a3b-q4-k-m`, the Planner profile sets
temperature 0.6, top-p 0.95, top-k 20, reasoning off, and a 128-token output
limit. These settings apply to EdgeLab's Planner requests.

```sh
python -B -m edgelab.capstone --journal runtime/ornith-demo.sqlite plan --case shared-trend
```

Add `--no-model` to disable model advice. The dashboard's **Settings**
offers explicit diagnostics for LM Studio, the OpenRouter account catalog,
and Pinecone retrieval. See [deployment](DEPLOYMENT.rst) for configuration
and provider access.

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
- `integrations/advanced_mcp/` contains the bundled gateway, its loopback profiles, and the report and draft adapters.
- `tests/` contains the core offline tests and synthetic fixtures.
- `research/` contains the workflow benchmark, independent bootstrap experiment and the development and holdout protocol code.

The published materials support research and reproducibility. Market datasets, local journals and credentials stay outside the repository. The controller gates all numerical execution to the approved in-sample protocol. The development and holdout research code has no order execution path.
