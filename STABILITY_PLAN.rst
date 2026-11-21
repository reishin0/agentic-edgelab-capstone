EdgeLab: from spread signal to tradability
=========================================

Research proposal only. No orders, broker privileges, live deployment,
or new execution authority. The historical SPY/QQQ stop remains unchanged.
Stocks, FX quotes, and crypto USD spot instruments are separate universes.
Candidate lists rank market coverage or liquidity, not cointegration.

1. Freeze the experiment
------------------------

Freeze point-in-time universe membership, exact instrument/venue and quote
conventions, sample windows, entry/exit policies, and trial budget. Align
real observation timestamps without filling missing or closed sessions.
Use separate formation and calibration periods and one locked chronological
holdout. Keep all screened pairs and apply a predeclared family-wise
adjustment (e.g., Holm for the fixed pair family, with a conservative
bidirectional pair p-value). Report missing and ineligible pairs separately.
For FX, normalize quotes consistently and map both base and quote currency
exposures; exclude duplicate inverses, peg artifacts and algebraically
redundant crosses. CNY and CNH are different instruments, not substitutions.
For crypto, choose an exact venue, UTC bar convention and liquidity window;
do not mix spot with perpetual price series without a basis model.

2. Estimate and challenge the relationship
------------------------------------------

Verify both inputs meet the declared integration prerequisite. Fit the
formation hedge ratio and use the bidirectional Engle-Granger gate under
the frozen specification. Only an eligible pair advances. Examine rolling
hedge ratios, residual level and volatility, error-correction sign and
reversion half-life with uncertainty. Predeclare rolling windows, acceptable
hedge drift, a minimum effective holdout horizon and stability stop rules.
CUSUM non-rejection means no break detected, not proof of stability.
A break-aware diagnostic can inform a new protocol; it cannot authorize
an automatic refit or reuse of the holdout.

3. Define a research-only entry
-------------------------------

For a positive hedge coefficient, define the log spread as
``s = log(A) - intercept - beta * log(B)``. Estimate center and scale
from past observations only. A sufficiently positive deviation suggests
short A and long B, with hedge-weighted rather than equal-share exposure;
a negative deviation reverses the legs. Block this simple long-short
mapping when the coefficient is nonpositive, unstable or cannot be
translated to the venue's contract notionals.

Use a standardized deviation (z-score or a predeclared robust equivalent)
as the candidate signal and show ``exp(s - center) - 1`` as an optional
percentage readout, not as expected profit. A larger gap can indicate a
break. As an initial research hypothesis, test an entry at 2 standard
deviations with a distinct inner exit band; choose thresholds and a time
stop on calibration only. Do not activate from the dashboard or infer
tradability from a threshold crossing.

4. Verify the economics and both legs
-------------------------------------

Use synchronized, executable bid/ask quotes and include both legs'
entry/exit fees, slippage, financing, borrow or funding, FX rollover and
currency conversion. Check depth for proposed sizes, minimum order sizes,
margin, short eligibility and actual borrow capacity before any signal
could enter paper simulation. Fail closed on stale prices, missing quotes,
borrow, a spread break, or insufficient net convergence after stressed
costs. Model legging risk, partial fills and unhedged exposure. Public
Kraken market availability is not account or region approval for a short;
an unavailable short leg means no pair trade, not a one-sided substitute.

5. Validate, then decide
------------------------

Freeze the model and triggers before opening the holdout. Record every
signal, skipped entry, executable fill assumption, cost and net result.
Assess net performance, independent convergence episodes, uncertainty,
drawdown, tail losses, regime sensitivity and capacity versus a no-trade
baseline. A positive midpoint simulation is not enough. Only a candidate
that clears prespecified stability, economics and execution criteria may
be considered for separate forward paper testing. Otherwise record stop
or unresolved. The dashboard should report the evidence at each gate:
statistical candidate, stability, after-cost validation and execution
eligibility as distinct outcomes.

References: statsmodels Engle-Granger and CUSUM documentation;
Gregory and Hansen (1996), residual-based cointegration tests with regime
shifts; Gatev, Goetzmann and Rouwenhorst (NBER WP 7032), pairs-trading
research and transaction-cost treatment.
