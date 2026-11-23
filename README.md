# Agentic EdgeLab

Agentic EdgeLab is a local research assistant for quantitative analysts who need to test a relationship hypothesis and show how they reached a verdict. Its reference workflow tests whether two series share a stable long-run relationship. The researcher chooses the hypothesis, eligible observations and protocol. After exact approval, EdgeLab runs the approved steps and produces a verdict, evidence-linked report and reproducibility bundle.

## Architecture

1. A **Planner** receives the blinded question and approved method context. LangChain retrieval returns checked method notes. A bounded Tree-of-Thought search ranks eligible protocol branches before the Planner sees sample values.
2. The **LangGraph controller** saves eligible protocol candidates and ends planning. A separate `execute` command verifies the run ID and exact protocol digest before dispatching approved statistical tools. SQLite records the plan, approval, reservations, results, reviews and verdict.
3. The **Critic** reviews evidence and flags conflicts. Gate rules and the controller determine whether the workflow continues, stops, requests revision or remains unresolved.
4. The **Reporter** explains the recorded verdict from verified evidence. Optional CrewAI role tasks advise inside the workflow. The local MCP service exposes method notes and a host-selected planning trace. The dashboard reads saved journals and offers opt-in provider diagnostics.

The tools use statsmodels for ADF and KPSS, bidirectional Engle–Granger tests and stability diagnostics. Planning uses bundled method notes by default, and LM Studio and OpenRouter advice are optional. Pinecone planning fetches the required records by exact ID and verifies them against the reviewed corpus. Missing or mismatched records leave the plan unresolved. The dashboard measures semantic ranking separately. See [architecture](ARCHITECTURE.rst), [message contracts](CONTRACTS.rst) and [dashboard settings](DEPLOYMENT.rst).

The bundled [authenticated MCP gateway](integrations/advanced_mcp/README.md) has separate profiles for read-only access to journal-verified synthetic reports and for operator-approved synthetic draft saves. After a lost save response, an operator can verify the original backend receipt and resume the draft workflow. Scientific execution requires its own exact run and protocol approval in the EdgeLab CLI.

## Run the synthetic example

The package requires Python 3.11 or later. From the repository root, create and activate a fresh virtual environment. Python 3.12 is suitable for the examples below.

```sh
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on macOS or Linux, or `.\.venv\Scripts\Activate.ps1` in PowerShell. The minimal fixture requires only the standard library.

```sh
python -B -m edgelab
python -B -m edgelab --approve-fixture --fixture support
```

For the journaled LangGraph and statistical demo, install the research dependencies and use a new journal. The plan uses bundled notes and `--no-model` to keep the example provider-free. Copy the run ID and the digest of the candidate whose `protocol.lag_policy` is `AIC` to compare your result with the published example.

```sh
python -m pip install -e '.[research]'
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite plan --case shared-trend --no-model
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite execute --run RUN_ID --approve PROTOCOL_SHA256
python -B -m edgelab.capstone --journal runtime/capstone-demo.sqlite report --run RUN_ID
```

The [published synthetic summary](examples/synthetic-shared-trend.json) shows the milestone evidence and final verdict for the AIC candidate. The CLI report also includes a reproducibility bundle.

For the offline suites, follow the dependency and telemetry setup in [deployment](DEPLOYMENT.rst). From the repository root, run `python -B -m unittest discover -s tests`, `python -B -m unittest discover -s research/pair_discovery`, and `python -B -m unittest discover -s research/workflow_comparison`. The signed holdout fixture tests also need `cryptography`.

## Use Ornith in LM Studio

Install the EdgeLab research extra and copy [providers.env.example](providers.env.example)
to a private `providers.env` file. Start the local LM Studio API server and load Ornith
as `ornith-1.5-35b-a3b-q4-k-m`. Set `LM_STUDIO_BASE_URL` to the server’s loopback
address including `/v1`, match `LM_STUDIO_PLANNER_MODEL` to the loaded instance ID,
and set `LM_STUDIO_PLANNER_PROFILE=ornith`.

EdgeLab uses LM Studio's model chat template and asks for one allowed choice as
a JSON object. For `ornith-1.5-35b-a3b-q4-k-m`, the Planner profile sets
temperature 0.6, top-p 0.95, top-k 20, reasoning off, and a 128-token output
limit. These settings apply to EdgeLab's Planner requests.

```sh
python -B -m edgelab.capstone --settings-file providers.env --journal runtime/ornith-demo.sqlite plan --case shared-trend --lm-studio --allow-provider-io
```

To opt into the loaded model for later plans, set `LM_STUDIO_AUTO_USE=1` in the
checkout’s `providers.env`. Use `plan --no-model` to bypass that setting. The
dashboard’s **Settings** offers explicit diagnostics for LM Studio, the
OpenRouter account catalog and Pinecone retrieval. See [deployment](DEPLOYMENT.rst)
for configuration and provider access.

## Evaluation

| Study | Observed result |
| --- | --- |
| [Offline contracts](CONTRACTS.rst) | Tested authorization denials, blocking failures and evidence tampering against fixed regression criteria. |
| [Matched-budget advisory comparison](research/workflow_comparison/REPORT.md) | On 24 blinded synthetic cases, mean rubric scores were 100% for the deterministic script, 28.13% for one agent and 30.21% for CrewAI roles. Both advisory arms used Qwen2.5 1.5B Instruct Q4_K_M with equal model-call budgets. The 2.08-percentage-point gain missed the 10-point threshold. The deterministic script directly encoded the scored rules. |
| [Independent stability validation](research/bootstrap_validation_20261114/REPORT.md) | 18,000 synthetic trials, each evaluated in three arms. The experimental bootstrap passed six stable-scenario gates and seven of twelve negative-scenario gates. EdgeLab retained CUSUM. [Aggregate results](research/bootstrap_validation_20261114/aggregate-results.json) |
| [Point-in-time discovery](research/pair_discovery/DISCOVERY_PROTOCOL_DRAFT.md) | A development coverage check left all 4,784 frozen pairs unresolved because formation histories were incomplete or invalid. The shortlist was empty and the market holdout remains locked. [Coverage summary](examples/development-coverage-summary.json) |

Use the [workflow comparison protocol](research/workflow_comparison/PROTOCOL.rst) for the replayable stub demonstration and instructions for a separately configured local-model run. The [bootstrap protocol](research/bootstrap_validation_20261114/PROTOCOL.md) includes a self-check, the full synthetic study command and ledger verification.

## Repository map

- `edgelab/` contains the controller, role adapters, statistical tools, evidence contracts, journal, reporting, MCP and dashboard.
- `integrations/advanced_mcp/` contains the bundled gateway, its loopback profiles, and the report and draft adapters.
- `tests/` contains the core offline tests and synthetic fixtures.
- `research/` contains the workflow benchmark, independent bootstrap experiment and the development and holdout protocol code.

Keep private market datasets, journals and credentials out of version control. The capstone controller gates statistical execution to the approved in-sample run and protocol. The development and holdout research code has no order execution path.
