Workflow research-quality evaluation
====================================

The structured comparison establishes whether both model workflows can follow
the same fixed evidence and reporting rules within the measured budget. Its
deterministic reference implements those rules directly, so a perfect reference
score leaves zero improvement headroom. The qualification gate in this pilot
can expose regressions. Establishing added research value requires a task with
headroom for identifying issues beyond the scripted checklist.

Before running that study, have two quantitative reviewers independently
annotate unfamiliar synthetic and public development cases prepared by a third
reviewer. Include difficult cases such as asynchronous calendars, common
factor confounding, multiple-testing leakage, and changepoints. Adjudicate
disagreements before freezing the case set. Retain the development data
provenance, cases, accepted alternative conclusions, and scoring rubric under
content digests. Keep the market holdout outside the study.

Give all three arms the same evidence inputs, finite tool allowlist, and
deterministic calculations. For the agent arms use the same model, total
context and completion budget, and allotted calls. Use the existing scripted
diagnostics and report generator for the deterministic arm. Allow open-ended
structured issue and evidence annotations without giving agents the adjudicated
answers. Reviewers score shuffled, anonymized reports for warranted verdicts,
consequential flaw detection, claim-level citation accuracy, and reproducibility.
Specify the paired superiority threshold, inter-rater checks, case sample size,
and resource limits before collecting outputs. Report per-case outcomes and
which defects were uniquely found by each arm.
