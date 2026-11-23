Checkpoint 1.1-4.1 integration
==============================

This extends the offline demo in README.md. It is a local, synthetic research
implementation, not a trading system or a claim of statistical validation.

* **1.1 Local models:** Loopback-only LM Studio structured chat. One endpoint
  with model IDs per role. The model ranks eligible Planner choices; the
  deterministic Critic is the default. An advisory model Critic is opt-in.
* **2.1 Research loop:** LangGraph routes approval, tool action, milestone
  review, and verdict. Statistical tools cover ADF/KPSS, both Engle-Granger
  directions, OLS residuals, CUSUM, ECM, and AR(1)-implied OU half-life where
  applicable.
* **3.1 Grounding:** Eight reviewed public-source notes. Pinecone planning
  fetches the required ADF, KPSS, and Engle–Granger records by exact ID.
  Semantic ranking is a separate diagnostic.
* **4.1 ToT and MCP:** Four-depth, width-two beam search over allowlisted
  decisions with retained pruning reasons and advisory votes. MCP exposes
  public notes, the tool catalog, and an optional host-selected planning trace.

Run the integration
-------------------

The original fixture demo requires no extra packages. For the integration
path, install the optional dependencies in a new environment when it is safe
to do so. From this folder in PowerShell::

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[research,mcp]"
    .\.venv\Scripts\python.exe -B -m edgelab.capstone plan --no-model

This command uses bundled notes and disables model advice. Planning operates
on the protocol scope and method evidence before reading observation values.
Review the displayed candidates and use the selected run ID and protocol
digest in a separate command::

    .\.venv\Scripts\python.exe -B -m edgelab.capstone execute --run RUN_UUID --approve PROTOCOL_SHA256
    .\.venv\Scripts\python.exe -B -m edgelab.capstone show --run RUN_UUID

The local CLI operator supplies approval. That is not multi-user
authentication. The SQLite journal binds approval to the saved plan, source,
exact in-sample data fingerprint, and Python, statsmodels, NumPy and SciPy
versions. The planning model never receives sample values. Recorded
observations are reused on resume; a reserved call without a result stops for
reconciliation. The controller revalidates saved reviews against their bound results before
advancing. Numerical tools receive only approved in-sample rows; the
holdout cannot be opened. A continue verdict is in-sample only and does not
authorize the next stage. After a source change, make a fresh plan; old
approvals are not carried forward. Existing journals are retained.

Frozen historical data
----------------------

An alternative to the built-in fixtures is a local, pre-aligned daily CSV
containing exactly three columns: ``day,a,b``. Use only public, synthetic or
properly anonymized values, and keep exports outside the tracked project.
Record how the series were aligned and which session calendar was used.
EdgeLab checks the supplied calendar; it does not certify it against an
exchange. Do not fill closed days or missing sessions with synthetic prices.

Place a version-1 JSON manifest beside the CSV. It has these fields:

* ``schema_version``: integer 1.
* ``classification``: ``public``, ``anonymized``, or ``synthetic``.
* ``source`` and ``calendar``: short, non-sensitive provenance labels for
  the data and reviewed session calendar.
* ``file``: sibling CSV filename, using letters, digits, underscores or
  hyphens before ``.csv``. ``sha256``: lowercase SHA-256 of its exact bytes.
* ``sessions``: ordered array of 80 to 2,000 ISO dates for the approved
  in-sample sessions. It must match the CSV dates exactly, without duplicates.
* ``window``: object with ISO dates ``start``, ``end``, ``as_of``, and
  ``holdout_start``. The first two must match the first and last sessions.
  The last session cannot exceed as-of or reach the holdout.

Plan using the manifest, then supply the same manifest and the exact selected
digest to execute::

    .\.venv\Scripts\python.exe -B -m edgelab.capstone plan --history-manifest PATH_TO_MANIFEST.json --no-model
    .\.venv\Scripts\python.exe -B -m edgelab.capstone execute --run RUN_UUID --approve PROTOCOL_SHA256 --history-manifest PATH_TO_MANIFEST.json

Planning reads manifest metadata only, not CSV observations. Numerical
execution binds the exact manifest and approved sample, and rejects changed
bytes, missing sessions and holdout rows. The report and journal omit local
file paths. Durations expressed in observations are in supplied session units
for historical data. The holdout stays locked and must not be included in the
CSV. The example uses the supplied files and bundled methods.

Paired history via Alpaca
~~~~~~~~~~~~~~~~~~~~~~~~~

For the reviewed 2025 NYSE Arca session calendar, the optional adapter
can fetch and freeze a paired daily history with one data-only call::

    python -B -m edgelab.market_history SPY QQQ --start 2025-01-02 --end 2025-10-31 --feed iex --allow-provider-io

Use the emitted manifest path in the plan and execute commands above.
Windows needs IANA time-zone data (included by the research environment's
numerical dependencies; install tzdata separately when necessary).
The adapter requests raw closes on one named feed, checks both series
against the reviewed session calendar, and refuses gaps, duplicates or
pagination. It caches only the in-sample CSV and manifest in ignored,
content-addressed storage. It never requests the holdout, fills missing
sessions, or changes feeds automatically. Raw closes are not total
returns; IEX is not the consolidated tape. A frozen download is not proof
of point-in-time vendor availability or absence of later corrections.

For a read-only report and evidence bundle use::

    python -B -m edgelab.capstone report --run RUN_UUID

For explicitly local chart access, use the same frozen manifest::

    python -B -m edgelab.dashboard --history-manifest PATH_TO_MANIFEST.json

The dashboard plots historical samples only after approved observations
exist and the manifest and sample digests match. Do not share that
loopback view, cache, or vendor-derived artifacts without review.
See ARCHITECTURE.rst for the checkpoint cross-check.

Stock candidate waitlist
~~~~~~~~~~~~~~~~~~~~~~~~

Download the official IWB holdings CSV from iShares. Build and pin the
local snapshot with::

    python -B -m edgelab.waitlist PATH_TO_IWB_HOLDINGS.csv
    python -B -m edgelab.dashboard --waitlist PATH_TO_WAITLIST.json

The builder ranks eligible US-listed USD equities by positive fund
holding market value and saves exactly 1,000 distinct tickers in ignored
storage. The JSON records the holdings date, source digest and exclusions.
The **Cointegration Test** tab lists the saved candidate assets. The optional pair
console can test one selected pair at a time.
Fund holdings are a research universe proxy, not an official index
constituent file or evidence of cointegration. Do not use a later holdings
snapshot to define an earlier historical test universe. The synthetic
grading demo does not read this local snapshot.

FX and crypto candidate snapshots
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The local cross-asset builder takes the final BIS FX annex workbook and
the public Kraken AssetPairs and Ticker JSON responses. It ranks 10
named USD spot FX quotes from BIS Table 3 and 10 online Kraken USD
spot markets by trailing 24-hour volume times VWAP from a reviewed
unpegged cryptocurrency list. The two rankings use different turnover
periods and cannot be compared as a common leaderboard. Margin flags
describe the public market, not actual account or region eligibility.

Build and open a pinned snapshot with::

    python -B -m edgelab.cross_assets BIS.xlsx PAIRS.json TICKER.json --observed-at UTC_TIMESTAMP
    python -B -m edgelab.dashboard --cross-assets PATH_TO_SNAPSHOT.json

No account or order APIs are used. The dashboard shows all of these
instruments as not tested. See STABILITY_PLAN.rst for the proposed
separate evaluation gates. The synthetic demo does not load the snapshot.

Cointegration Test console
~~~~~~~~~~~~~~~~~~~~~~~~~~

Enable the console with ``--pair-console-journal runtime/waitlist-pair-tests.sqlite``
and a configured stock or cross-asset waitlist. Use a separate journal file.
Select two instruments and choose a matching
frozen manifest supplied at launch or previously captured by the console.
A stock pair can also request a bounded
Alpaca IEX capture with explicit consent and the reviewed sessions through
October 31, 2025.

The console displays the protocol bound to the run and digest before offering
approval. Once approved, it prints execution and the verified report. A
ready report prompts you to refresh the dashboard. The reminder clears once
the refreshed run list includes that result. Saved plans and reports remain
available in the console after a service restart. A prior interrupted
execution or unresolved reservation blocks new jobs in that journal. The
console stores new plans and results in its own journal and opens the other
research journals read-only. Approval is local operator approval.

Batch pair execution
--------------------

Plan up to 12 explicit frozen manifests (or distinct synthetic fixtures)
without reading observation values. The same input set, source, runtime,
and lag policy resolve to the same batch and run IDs in one journal. Review
the displayed pairs, windows, lag policy, and batch SHA-256 before approving.
Run from the EdgeLab directory with the research environment activated::

    python -B -m edgelab.batch plan --history-manifest A.json --history-manifest B.json
    python -B -m edgelab.batch execute --batch BATCH_UUID --approve BATCH_SHA256 --history-manifest A.json --history-manifest B.json
    python -B -m edgelab.batch report --batch BATCH_UUID
    python -B -m edgelab.batch report --batch BATCH_UUID --run RUN_UUID

The default journal is ``runtime/pair-batches.sqlite``. To use another
journal, supply ``--journal PATH`` before the subcommand. Each run's approval,
tool results, Critic reviews, verdict, and reproducibility ledger are stored
in SQLite. The report commands regenerate JSON from that evidence without
rerunning tests or requiring the input files. Pass the batch journal to
the dashboard with ``--journal`` and the matching manifests with
``--history-manifest`` to see the corresponding historical charts.

The batch preflights every frozen input, then runs the approved candidates
serially. A statistical ``stop`` or ``unresolved`` verdict is a result, not a
batch failure. An infrastructure error stops the batch. Completed runs are
retained and unstarted members are visible. A claimed batch is never
automatically retried; a repeat command returns its persisted status.
This does not collect data, schedule jobs, unlock holdouts, or establish
significance across a screened universe. Use separately predeclared
multiple-testing controls for universe research.

Optional providers
------------------

Fill in the project-local .env, which Git ignores. The root coursework .env
is not loaded. LM_STUDIO_BASE_URL defaults to http://127.0.0.1:1234/v1; model
IDs must be set explicitly. Use ``--lm-studio --allow-provider-io`` for
model-assisted ranking with the deterministic Critic. Use ``--model-critic``
only with a separately qualified advisory Critic model; it can escalate but
cannot waive any gate. The reporter setting is reserved; reporting stays
deterministic. Chat requests can consume GPU resources. Do not use this mode
during live trading or until you have authorized provider access.

For Pinecone, use an existing dedicated 1024-dimensional dense cosine vector
index and set PINECONE_INDEX_HOST to its data-plane host, not the console URL.
The corpus command lists the exact text and digest for review. Only fixed
public topics and the included public paraphrases are sent to Pinecone.
Review, then explicitly upsert and plan::

    .\.venv\Scripts\python.exe -B -m edgelab.capstone corpus
    .\.venv\Scripts\python.exe -B -m edgelab.capstone seed-pinecone --approve-corpus CORPUS_SHA256 --allow-provider-io
    .\.venv\Scripts\python.exe -B -m edgelab.capstone plan --retrieval pinecone --lm-studio --allow-provider-io

The seed command writes to edgelab-public-methods-v1 and does not create,
delete, or reconfigure an index. The two provider flags are independent.
The 0.30 vector and 0.50 rerank floors are provisional routing rules; missing
applicable evidence causes abstention. Source quote checking is not automated:
the notes are paraphrases and the connector verifies them against the
versioned corpus.

MCP gateway and stdio interface
-------------------------------

EdgeLab's stdio service uses allowlisted discovery and dispatch. To start
the optional interface::

    .\.venv\Scripts\python.exe -B -m edgelab.mcp_service

The host may bind --run-id RUN_UUID to expose that run's planning trace.
The interface cannot approve or execute an experiment.

The `bundled mcp_gateway integration <integrations/mcp_gateway/README.md>`_
runs as a separate authenticated loopback process. Its report profile reads
pinned synthetic studies. Its draft profile binds operator approval to the
exact save operation and reconciles uncertain saves against backend receipts
before resuming. The draft and workflow stores are separate from EdgeLab's
scientific journal, and the scientific handoff uses the normal CLI approval
path.

Source API TODOs
----------------

These are required for a provider-backed or MySQL-sourced run, not for the
offline synthetic or frozen-export path.

* **LM Studio:** Qualify the loaded model's structured chat response before
  enabling it for the Planner and Critic. A planning qualification stopped
  at the first Planner request: finish reason was length, with no final JSON.
  The local comparison produced 18 branches and two
  candidates awaiting approval. Reasoning and token settings were unchanged.
  Qwen2.5 1.5B Instruct (Q4_K_M) is qualified for eligible Planner ranking
  on the tested isolated CPU worker, not for Critic judgment. In a
  contrast test, the model falsely accepted four flawed protocols. The
  deterministic Critic rejected all four, and two fresh Planner-only runs
  each retained 18 branch records and returned two candidates for human
  approval with one model call. Keep advisory model Critic qualification
  open. Do not point EdgeLab at a shared trading worker by default.
* **Pinecone:** Configure the dedicated index host and key, review and seed the
  public-method corpus, then verify embedding, vector query and reranking with
  the required method notes. Local notes were used in the diagnostic run.
* **MySQL history:** Establish a dedicated read-only account limited to
  approved history views and agree on the date, symbol, price adjustment and
  session-calendar contract. Implement and qualify a bounded export into the
  existing frozen manifest and CSV format. Do not load trading configuration
  or send raw series to the planning model. MySQL is not connected yet.

Still open
----------

Optional CrewAI role tasks and LCEL method retrieval are covered by
DEPLOYMENT.rst. Broader LangChain tool adapters, multi-user identity, native graph
checkpoints, holdout authorization and evaluation, broader statistical methods,
and end-to-end matched-budget comparisons remain future work. A separate
`matched-budget advisory comparison <research/workflow_comparison/REPORT.md>`_
completed 24 paired synthetic cases. Cloud provider use is opt-in. The
controller runs statistical tools in-process. MCP exposes a descriptive tool
catalog for inspection.

Research dashboard and Critic
-----------------------------

From this folder, with the research extra installed, run an isolated
four-case demonstration::

    $env:OPENBLAS_NUM_THREADS = '1'
    $env:OMP_NUM_THREADS = '1'
    .\.venv\Scripts\python.exe -B -m edgelab.dashboard --demo

Open http://127.0.0.1:8793/. Use a different port if needed. Omit
``--demo`` to see the project journal, or repeat ``--journal`` to read
explicit journals together. When supplying journals, include the project
journal explicitly to also see its runs. Repeat ``--history-manifest`` to
opt in to plotting each corresponding approved sample. If the matching
manifest is absent, the verdict remains visible but the chart is withheld.
Duplicate run IDs across journals are rejected as ambiguous.
The server binds only to loopback. The optional waitlist pair console uses
explicit local approval, and a stock capture requires a separate opt-in.
The demo uses a temporary journal and never touches an existing run.
The optional ETF entry-cost panel accepts a pinned aggregate result through
``--etf-entry-costs PATH_TO_RESULT.json`` in read-only mode. The operator
supplies the result separately. The dashboard verifies the result hash and
displays aggregate modeled buy costs with a newcomer Tip. Synthetic demo mode
omits the panel.
For assessment from a clean clone, install the research extra with
``python -m pip install -e ".[research]"``, then run
``python -B -m edgelab.dashboard --demo`` from this folder. Demo mode does not
read ``.env`` or the live cache. It seeds a separate temporary
``.cache/market-news.json`` from the bundled synthetic fixture; those headlines
are labeled and are not presented as provider data.

The ``spread-level-shift`` fixture adds a 1.2-unit intercept shift midway
through the approved synthetic formation window. It is a positive control
for the stability check: both series pass the unit-root prerequisite, both
Engle-Granger directions reject, then CUSUM rejects and the controller stops.
The original ``structural-break`` slope reversal remains a separate case; an
earlier inconclusive test can stop that run before stability is checked.

The dashboard distinguishes protocol branches from executed tests. Its
ReAct-style view shows proposals, approval, tool reservations, observations,
Critic decisions and the controller's verdict. It does not expose model
chain-of-thought. Charts show fingerprint-matched approved synthetic
series and their recorded in-sample fitted spread. A local historical run
requires explicit manifest selection to plot its approved series. LM Studio
and Pinecone tiles distinguish configuration from selected-run observations.
Neither is a live health check.

Critic policy ``stat-critic-v1`` checks each result against the approved
window, sample, lag choice and numerical runtime. It recomputes I(0)/I(1)
from level and first-difference ADF/KPSS p-values. Disagreement, inconsistent
labels, invalid evidence, or an evidence binding mismatch cannot advance the
workflow. Bidirectional Engle-Granger disagreement remains unresolved.
CUSUM rejection stops; non-rejection means no break was detected by that
test. ECM and AR(1)-implied OU are descriptive, and do not establish stability
or mean reversion. Findings preserve a rule ID and the bound result digest.
A new source version requires a fresh plan; old journals are not rewritten.
Run the fixed-seed diagnostic screen with
``python -B -m tests.critic_stress``. It compares 20 samples per synthetic
scenario and reports dispositions and terminal rules. It is not a
calibration, a live-data comparison or a power study.

Pair leaderboard
----------------

Start the dashboard with ``--leaderboard PATH_TO_SNAPSHOT.json`` to display a
local screening snapshot alongside the selected journal runs. Keep each
statistical family separate: the view accepts stock/stock and FX/crypto, not
FX/FX. Each group identifies its sample and shows price coverage, tests
actually run, data blocks, and comparisons not screened. The snapshot
must be generated from reviewed evidence, not inferred from a watchlist.

Measured pairs are ordered within their family by ascending Holm-adjusted
p-value, then by the larger of the two directional Engle-Granger p-values.
Rows without both directional results are unranked. A rank describes the
historical test, not a buy/sell recommendation. The dashboard does not
rank trading opportunities without separate validation of spread stability,
out-of-sample after-cost performance, and both legs' execution feasibility.

Optional Alpaca market data
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use Alpaca market-data credentials in the project-local ``.env`` (see
``config.env.example``). Prefer a dedicated pair; never copy a full trading
environment into the coursework repo. Avoid repeated polling with a shared
credential. To fetch a bounded news and options snapshot, use::

    .\.venv\Scripts\python.exe -B -m edgelab.news SPY QQQ --allow-provider-io
    .\.venv\Scripts\python.exe -B -m edgelab.options SPY QQQ --allow-provider-io

The options command caches one page per underlying (up to 20 contracts
each) in ``.cache/market-options.json``, including quote and trade times,
bid/ask, last, implied volatility and Greeks where returned. It records
the requested expiry range and whether more contracts were available.
The default indicative feed can be delayed and its quotes are modified;
the snapshot is not a full chain or a live trading input.

The fetcher makes one bounded REST request per poll and stores the latest
successful snapshot in the Git-ignored ``.cache/market-news.json``. The live
cache does not contain API keys. Do not commit the cache or ``.env``, or
publish provider data without checking redistribution rights. Commit the
bundled synthetic fixture for credential-free assessment instead. The dashboard refreshes
that local cache, displays fetch and publication timestamps, and marks a
snapshot stale after 10 minutes. Real-time availability depends on feed
entitlement. No polling starts with the dashboard, no full articles are
stored, and neither headlines nor the cache affect research decisions.
See the `Alpaca news endpoint <https://docs.alpaca.markets/us/v1.4.2/reference/news-3>`_.

API references: `LM Studio structured output <https://lmstudio.ai/docs/developer/openai-compat/structured-output>`_,
`Pinecone embedding <https://docs.pinecone.io/reference/api/2025-10/inference/generate-embeddings>`_,
`Pinecone reranking <https://docs.pinecone.io/reference/api/2025-10/inference/rerank>`_,
`LangGraph Graph API <https://docs.langchain.com/oss/python/langgraph/graph-api>`_,
`MCP Python SDK v1 <https://py.sdk.modelcontextprotocol.io/v1/>`_,
and `statsmodels cointegration <https://www.statsmodels.org/stable/generated/statsmodels.tsa.stattools.coint.html>`_.
