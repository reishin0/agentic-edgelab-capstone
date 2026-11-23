EdgeLab deployment review
=========================

The deployment order below starts at Checkpoint 5.1 and works backward.
It separates an implemented control from evidence that a real provider or
deployment is ready. The holdout and order execution are out of scope.

5.1: Collaboration and escalation
---------------------------------

LangGraph owns lifecycle and approval. Optional CrewAI tasks provide Planner
and Critic choices inside bounded planning; they have no tools, delegation,
code execution, or approval authority. The Reporter can select presentation
order from a minimal, verified summary. It cannot change the report or journal.
An advisory failure is visible, and a planning failure cannot approve a plan.

Start with a new local journal. Exercise a synthetic plan, reject a wrong
digest, execute only the exact displayed digest, and compare the canonical
report before and after adding presentation metadata. Do not use an active
journal for rollout: a source change invalidates older pending approvals.
The read-only escalation command emits a digest-bound local handoff for
unresolved, revise, or reconciliation-required runs. It grants no authority.
Multi-user identity, authenticated ticket delivery, and matched-budget
single-agent versus multi-agent evaluation remain separate acceptance items.

4.1: Planning search, LangChain and MCP
--------------------------------------

Bounded, outcome-blind beam search remains inside the graph. LangChain LCEL
composes method retrieval with conversion to structured evidence. The MCP
stdio process exposes public methods, an allowlist, and an optional trace for
one host-selected run. It cannot dispatch statistics or approve a run.

Deploy MCP as a host-owned subprocess, using the same isolated interpreter
and explicit journal/run pair for trace access. Verify that alternate run
IDs and extra parameters are rejected. Do not expose this interface on a
network until authenticated per-run authorization is independently qualified.
Native persistent graph checkpoints and broader planning branches remain
future work; the existing SQLite ledger remains the authority for restart.

3.1: Grounded retrieval
-----------------------

Use the bundled eight-note public corpus for the credential-free qualification.
For a separate Pinecone qualification, verify the configured index, namespace,
embedding model, vector dimension, and digest. Seed only a reviewed public
corpus with the exact digest, then test a missing, altered, and valid citation.
The provider adapter filters and reranks; it does not fall back silently.
Method notes are not market evidence and cannot authorize an experiment.
No live Pinecone seed or search is required by this rollout.

Dashboard research settings
---------------------------

Open **Settings** on the loopback dashboard. LM Studio is the default model
provider and bundled public notes are the default evidence source. EdgeLab
lists downloaded GGUF files and uses the loopback catalog to identify loaded
model IDs. Load the chosen model in LM Studio before selecting it in EdgeLab.
**Run without model advice** uses the deterministic Planner.

Use an ignored ``providers.env`` file in the checkout for optional provider
configuration. Supported keys include ``LM_STUDIO_MODELS_DIR`` for an
absolute local model folder override, ``LM_STUDIO_BASE_URL`` and
``LM_STUDIO_API_KEY`` for the loopback service, ``OPENROUTER_API_KEY`` for the
account catalog, and ``PINECONE_INDEX_HOST`` and ``PINECONE_API_KEY`` for the
public methods index. **Refresh models** queries OpenRouter only when it is
selected. **Free only** limits execution to eligible zero-priced routes. A
model test submits a fixed synthetic prompt. A successful test checks the
route and structured response once. It does not measure adviser quality.

**Test retrieval** shows exact required-note coverage and eligible citations.
The Pinecone diagnostic checks semantic ranking independently. Planning
requires exact reviewed ADF, KPSS, and Engle-Granger records from the pinned
namespace. Incomplete retrieval leaves the plan unresolved. Corpus seeding
remains an explicit digest-bound CLI command using the same settings file.

The Settings panel copies a CLI command for a new synthetic plan. The
dashboard reads saved journals. The CLI records the selected adviser and
retrieval source with the plan. Use the exact approval command to execute
statistics after reviewing the plan. The numerical Critic and controller
retain authority, and there is no automatic cloud fallback.

2.1: ReAct execution and durable evidence
-----------------------------------------

Keep the typed, deterministic tool loop and exact-digest approval. Verify
that an unresolved prerequisite stops dependent tools, interrupted work is
not silently replayed, and the journal can regenerate the report without
raw bars. Do not enable holdout execution or cloud-model failover. Johansen,
Gregory-Hansen, and one-use holdout accounting need separate specifications
and validation rather than widening the current tool allowlist.

1.1: Local model service
-----------------------

Qualify LM Studio last, with an EdgeLab-only loopback service, explicit role
model IDs and a separate settings file. Do not reuse a trading endpoint,
alter model residency or GPU settings, or enable cloud fallback. Confirm
structured choices, per-role timeouts, and failure handling on synthetic data
before using a frozen research sample. Offline tests use a model transport
stub; they do not qualify the live service or measure model quality.

Local commands
--------------

From the EdgeLab directory, use a fresh Python 3.12 virtual environment.
Install the research, MCP and CrewAI extras there and keep telemetry disabled
for the qualification process::

    python -m pip install ".[research,mcp,crew]"
    $env:CREWAI_DISABLE_TELEMETRY = "true"
    $env:OTEL_SDK_DISABLED = "true"
    $env:PYTHON_DOTENV_DISABLED = "1"
    $env:LANGSMITH_TRACING = "false"
    $env:LANGCHAIN_TRACING_V2 = "false"
    python -m unittest discover -s tests

Credential-free smoke test with a new journal::

    python -m edgelab.capstone --journal runtime/qualification.sqlite plan --case shared-trend
    python -m edgelab.capstone --journal runtime/qualification.sqlite execute --run RUN_ID --approve DIGEST
    python -m edgelab.capstone --journal runtime/qualification.sqlite report --run RUN_ID
    python -m edgelab.capstone --journal runtime/qualification.sqlite escalation --run RUN_ID
    python -m edgelab.mcp_service --journal runtime/qualification.sqlite --run-id RUN_ID

After independently qualifying the isolated local endpoint, enable advisory
roles explicitly. Replace RUN_ID and DIGEST with the outputs of that same
journal and settings with the EdgeLab-only configuration path::

    python -m edgelab.capstone --journal runtime/crew.sqlite --settings-file edgelab-local.env plan --case shared-trend --crewai --lm-studio --model-critic --allow-provider-io
    python -m edgelab.capstone --journal runtime/crew.sqlite execute --run RUN_ID --approve DIGEST
    python -m edgelab.capstone --journal runtime/crew.sqlite --settings-file edgelab-local.env report --run RUN_ID --crewai --allow-provider-io

Keep settings outside version control. Only use this profile with an isolated
operator-owned endpoint and journal. Roll back by using the prior interpreter
and source release; keep journals and provider configuration unchanged.

Design references: CMU Module 5 Quick Reference Guide, page 3 (roles and
graph coordination); Module 4 slides, page 65 (CrewAI, LangChain and MCP).
