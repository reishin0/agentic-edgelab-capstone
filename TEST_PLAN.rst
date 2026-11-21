Agentic EdgeLab Test Plan
=========================

Purpose and release scope
-------------------------

Verify that EdgeLab evaluates a researcher-defined relationship without
looking at outcomes before approval, using observations outside the frozen
window, or reporting a conclusion the recorded evidence does not support.
This plan covers the offline capstone baseline, the local read-only
dashboard, and the optional MCP interface. It is not a trading, strategy
performance, or production-readiness certification.

Run ordinary acceptance tests with synthetic fixtures or mocked providers.
Use only explicitly approved, frozen public data for historical acceptance.
Do not start a live feed, load shared credentials, call an order endpoint,
download a holdout, or publish cached data or journal files.

Environment and entry criteria
------------------------------

* Start from a clean checkout in an isolated environment with Python 3.11+
  and the research and MCP extras. Record the Git SHA, OS, Python, and resolved
  dependency versions; compare exact numerical results only on matched
  runtimes. No project ``.env`` or provider key is required for acceptance.
* Use a temporary SQLite journal and the bundled synthetic fixtures by
  default. For a historical display check, use read-only copies or explicitly
  named read-only journals and their original, verified manifests.
* Before any new historical calculation, confirm the run UUID, exact
  protocol digest, source and sample fingerprints, numerical runtime,
  session calendar, and holdout boundary. Stop on drift or missing approval.

From the EdgeLab directory in PowerShell::

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[research,mcp]"
    $env:OPENBLAS_NUM_THREADS = '1'
    $env:OMP_NUM_THREADS = '1'
    $env:LANGSMITH_TRACING = 'false'
    $env:LANGCHAIN_TRACING_V2 = 'false'
    .\.venv\Scripts\python.exe -B -m unittest discover -s tests -v

Acceptance checks
-----------------

P0 checks protect evidence, approval, and the holdout. P1 checks protect
correctness and usability. Both are required for the offline baseline.

* **QA-01 | P0 | Approval and state.** Run
  ``test_workflow.py``, ``test_bugscan.py``, and ``test_reproducibility.py``.
  Reject missing, mismatched, stale and orphaned approvals; reject replay,
  out-of-order steps, unreviewed milestones and source/runtime drift. An
  interrupted reservation is reconciled or blocked, never silently replayed.
* **QA-02 | P0 | Data and holdout.** Run ``test_history.py`` and
  ``test_market_history.py``. Reject altered CSV bytes, duplicate or
  missing sessions, wrong columns, nonfinite prices and appended holdout
  rows. Confirm the chart and tool only use the approved in-sample dates.
* **QA-03 | P0 | Critic and reporting.** Run ``test_stat_critic.py``,
  ``test_workflow.py`` and the planted-shift dashboard regression.
  Conflicting root tests or Engle-Granger directions must not advance.
  A planted spread shift must pass the prerequisites, reach CUSUM and
  stop on rejection. The report, dashboard and journal must agree on the
  verdict and referenced evidence. CUSUM non-rejection is not proof of
  stability, and neither verdict is a trading signal.
* **QA-04 | P0 | Dashboard isolation.** Run ``test_dashboard.py``.
  Reject nonlocal hosts, writes, unlisted paths and duplicate run IDs across
  journals. Verify that demo mode neither reads private settings nor uses
  real cached headlines. Compare journal hashes before and after read-only
  browsing. A missing or mismatched manifest hides the chart, not the verdict.
* **QA-05 | P1 | Browser acceptance.** On desktop (1440 x 900) and mobile
  (390 x 844), switch between an equity and an FX run in one read-only
  dashboard. Check pair labels, dates, recorded p-values, verdict, spread
  chart and holdout state against each journal; neither run may show the
  other's data. Switch AIC/BIC: an unexecuted candidate must remain a preview,
  not change the verdict. Check waitlist filtering, stale-news labels, layout
  overflow and JavaScript errors. If no saved public runs are available,
  use test projections and record the result as fixture-only.
* **QA-06 | P1 | Retrieval, models and MCP.** Run ``test_integrations.py``
  and ``test_model_qualification.py`` with mocked or in-memory dependencies.
  Missing or inapplicable method evidence must block approval; untrusted
  retrieval metadata and invalid model output must not enter an approved
  plan. Verify that MCP cannot approve, dispatch statistics or expose an
  unbound run. These tests do not qualify a live service or a model.
* **QA-07 | P1 | Market context.** Run ``test_waitlist.py``,
  ``test_cross_assets.py``, ``test_options.py`` and the news-cache dashboard
  tests. Verify frozen universe identity, rejection of tampered rankings,
  stale/deduplicated/sanitized news, and partial option-chain labeling.
  None of these inputs may change a recorded statistical result.

Evidence, exit criteria and follow-up
------------------------------------

Record the tester, UTC timestamp, commit and dependency versions, check ID,
fixture or authorized run ID, expected and actual result, pass/fail/blocked
status, and artifact references. For historical checks record the manifest,
protocol and ledger digests, not raw prices or credentials. For failures
include the first reproducible step and relevant redacted output.

Accept the offline baseline only when the full suite passes without
unexplained skips, the browser matrix passes, no P0/P1 issue remains open
for the baseline, and the commit contains no credentials, market caches or
runtime state. Re-run the failing test and the full suite after a fix.
Track live provider checks, broader statistical simulation, point-in-time
universe screening, and actual holdout evaluation as separate work with
their own authorization and success criteria. No offline test pass counts
as evidence of profitability or readiness to trade.
