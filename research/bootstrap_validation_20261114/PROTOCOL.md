# Independent synthetic validation of EdgeLab's stability bootstrap

Frozen before examining any confirmation trial outputs. The study uses the
unchanged conditional AR(1) wild bootstrap from the November 14 experiment.
It asks whether the 540-observation bootstrap pipeline has enough power to
advance stable spreads while controlling the rate of advancement when the
relationship is ineligible or unstable. A pass supports further research.
Market holdout access, trading simulation, and production promotion are
outside this protocol.

## Arms and seeds

- Compare 180-observation CUSUM, 540-observation CUSUM, and 540-observation
  bootstrap. Run the existing BIC ADF/KPSS and bidirectional Engle–Granger
  gates at 5% before the respective stability check. A failed gate or tool
  error counts as no advancement. Run the bootstrap arm even if the
  paired 540-observation CUSUM computation errors after the upstream gates.
- Reuse the same sampled shocks across all arms. Original generators keep
  the break at the same fraction in each observation window to reproduce
  the prior design. New cases use the first 180 observations of the same
  540-observation path. A break occurring after observation 180 is invisible
  to the 180-observation arm. That arm's result is descriptive in those
  cases. The equal-length comparison isolates the stability-method change.
- Run exactly 1,000 new seeds per scenario in the order below. Scenario
  index `i` (zero based) uses the half-open integer range
  `[510000 + 1000*i, 511000 + 1000*i)`. Bootstrap draws use the reference
  implementation's separate random stream, 199 draws, and strict rejection
  at p < 0.05. The original scan grid spans 20%–80% every five observations
  plus the upper endpoint. Explicit out-of-grid cases test this limitation.
- The eight original generators and module hashes are those pinned by the
  November 14 study. For new generators the shared trend has innovation SD
  1, the spread has innovation SD 0.6, and its baseline coefficient on the
  trend is 1.3. New independent Gaussian shocks and unit-variance Student-t
  shocks use NumPy PCG64. The original generators retain their RNGs.

| Scenario in seed order | Specification | Gate |
| --- | --- | --- |
| `independent_walks` | Separate Gaussian random walks | Ceiling |
| `stable_strong` | AR(1), phi = 0.40 | Preserve |
| `stable_weak` | AR(1), phi = 0.92 | Gain |
| `slope_reversal` | Slope changes from 1.3 to -1.2 halfway | Ceiling |
| `intercept_mid` | Intercept increases by 1.2 halfway | Ceiling |
| `independent_persistent_ar` | Separate stationary AR(1) levels, phi = 0.98 | Ceiling |
| `intercept_off_grid` | Intercept increases by 1.2 at 42% | Ceiling |
| `stable_weak_arma` | ARMA(1,1), phi = 0.92 and MA = 0.5 | Gain |
| `stable_student_t` | AR(1), phi = 0.92 with Student-t(3) spread innovations | Gain |
| `stable_volatility_regimes` | AR(1), phi = 0.92 with innovation SD multiplying successively by 0.5, 1.5, 0.5 in equal thirds | Gain |
| `stable_near_unit` | AR(1), phi = 0.96 | Gain |
| `correlated_walks` | Two walks with innovation correlation 0.80 and no stable spread | Ceiling |
| `intercept_small_early` | Intercept increases by 0.6 at 22%, spread phi = 0.40 | Ceiling |
| `intercept_large_late` | Intercept decreases by 2.4 at 74%, spread phi = 0.40 | Ceiling |
| `intercept_large_outside_grid` | Intercept increases by 2.4 at 85%, spread phi = 0.40 | Ceiling |
| `intercept_weak_late` | Intercept increases by 2.4 at 74%, spread phi = 0.92 | Ceiling |
| `slope_moderate_early` | Slope increases by 0.30 at 22%, spread phi = 0.40 | Ceiling |
| `slope_moderate_outside_grid` | Slope decreases by 0.30 at 15%, spread phi = 0.40 | Ceiling |

Break indices are rounded down. The theoretical hedge slope multiplies the
trend level on both sides of a break. A slope jump can therefore also cause
a price-level jump when the trend is away from zero.

## Decision rules

- **Ceiling:** The exact one-sided 97.5% Clopper–Pearson upper confidence
  bound on 540-bootstrap advancement must be at most 2% in every ceiling
  scenario. Use all trials as the denominator.
- **Gain:** The paired one-sided 97.5% lower confidence bound on the
  540-bootstrap advancement rate minus the 180-CUSUM advancement rate must
  be at least +10 percentage points in every gain scenario. Compute the
  bound as the 98.75% one-sided exact lower bound on paired gains minus the
  98.75% one-sided exact upper bound on paired losses, using all trials in
  each denominator. The union bound covers the difference without an
  independence assumption between gains and losses. Report the 540-CUSUM
  comparison using the same method as a diagnostic.
- **Preserve:** Apply the same paired lower bound against each CUSUM arm.
  Require at least -5 percentage points against both on `stable_strong`.
- Passing the study requires passing every bound above. Each gate uses
  marginal coverage. The claim is the conjunction of all passes. Report
  descriptive two-sided Wilson 95% intervals for each arm, all terminal
  rules, the number reaching stability and counts by outcome within that
  subset, and paired gains and losses. Tool errors and unsupported
  bootstrap fits are visible as their own outcomes. Check the full
  seed-by-arm ledger and report all failed bounds. No adaptation to these
  confirmation outcomes is part of the study.

## Reproduce

Use Python 3.12.10, NumPy 2.5.4, SciPy 1.18.1 and statsmodels 0.14.6. Run
from the EdgeLab repository root with EdgeLab importable (for example,
through an editable install of `.[research]`). Use the same location of
this protocol in the three commands below. The output directory must be
new. Choose `--workers 4` or another positive number independent of results.

```text
python research/bootstrap_validation_20261114/run.py --self-check
python research/bootstrap_validation_20261114/run.py --output /path/to/new-run --workers 4
python research/bootstrap_validation_20261114/run.py --verify /path/to/new-run
```

The JSON includes hashes of the protocol, runner, exact vendored reference
experiment files, and imported EdgeLab numerical code and contracts. The
CSV preserves per-seed, per-arm bootstrap probabilities and terminal gates.
