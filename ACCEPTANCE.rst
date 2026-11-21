EdgeLab Offline Acceptance
==========================

**Result: PASS** for baseline ``7cfcb9b8d74f589ad6ebe1789fbf20e463cd21bd``
on 2026-11-11 at 03:09 UTC. The test scope and exit criteria are defined in
`TEST_PLAN.rst <TEST_PLAN.rst>`_.

The 49 committed project files were tested from a clean source archive on
Windows 11 with Python 3.13.15. The automated suite ran 74 tests with zero
failures, errors, or skips.

* **QA-01:** Approval, replay, and source/runtime drift safeguards passed.
* **QA-02:** Frozen sample validation and holdout protection passed.
* **QA-03:** The planted spread shift reached CUSUM and correctly stopped
  the synthetic run; recorded verdicts agreed with their journal evidence.
* **QA-04:** The dashboard rejected disallowed requests and ambiguous run
  IDs; missing or mismatched manifests did not produce charts.
* **QA-05:** Chromium checks passed at 1440 x 900 and 390 x 844. Switching
  between the saved equity and FX runs preserved their respective charts,
  dates, p-values, and verdicts. The AIC preview did not alter either BIC
  result. No JavaScript errors or outbound requests were observed.
* **QA-06:** Mocked provider and in-memory MCP checks passed.
* **QA-07:** Waitlist, news-cache, and options fixture checks passed.

The saved journals, reports, and frozen inputs had matching hashes before
and after verification. No historical statistics were recalculated, no
holdout data was accessed, and the existing dashboard was not restarted.
Live provider validation, out-of-sample performance, and trading readiness
were outside the scope of this acceptance run. Raw test logs, run artifacts,
market data, and credentials are not included in the repository.
