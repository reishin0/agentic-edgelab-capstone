EdgeLab synthetic error study
=============================

Frozen before running the simulation. This is separate from the five
repeated workflow demonstrations and the deterministic acceptance suite.
It uses only the 180 synthetic formation observations in each trial.

* Use 1,000 independently seeded samples per scenario, with disjoint seeds
  100000 through 105999, allocated in the scenario order in the runner.
* Scenarios: independent random walks (null); stable shared trend with
  strong or weak spread reversion (positive controls); mid-sample hedge
  slope reversal or intercept shift (unstable); independent persistent
  AR(1) levels with phi=0.98 (outside the I(1) target).
* Fix BIC, alpha=0.05 and all DGP parameters in the runner. Run the existing
  deterministic diagnostics, bidirectional Engle-Granger and CUSUM pipeline.
  Do not tune thresholds or switch lag policy based on these outcomes.
* "Continue" is a positive screen, not a validated trade. Report positive
  rates and 95% Wilson intervals per scenario. Treat non-continue as a
  missed detection for the two stable scenarios; report false advancement
  separately for the null, unstable and outside-scope scenarios. Retain
  stop, revise, unresolved, and invalid-tool counts.
* Separately compare the workflow route with a frozen rules-only reference
  using identical numerical observations. Agreement is a wiring check,
  not independent statistical corroboration.
* Acceptance suite gates: zero successful unauthorized operations; all
  tested fidelity and safe-stop cases must pass; at least 95% agreement
  with the frozen reference. No statistical power or false-positive target
  was specified for the simulation; estimates must not be turned into a
  pass/fail gate after inspecting them.

No provider, market data, journal, or holdout access. This tests one
formation sample per trial, not the family-wise performance of the bulk
equity screen, execution economics, or LLM quality.
