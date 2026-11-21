# EdgeLab equity discovery and market holdout protocol

**Draft for review.** The dates, data requirements and gates below are
proposed research settings. The existing equity holdout beginning
2025-11-03 remains locked. This draft creates no permission to read it.
The exploratory 963-stock screen used a later membership snapshot and
provides development context only. The synthetic confirmation failed
the bootstrap's negative-control gates, so this draft uses the current
CUSUM pipeline.

## Development-only discovery

**Question.** Can a fixed, long–short US equity pair selected from a
point-in-time family exhibit stable cointegration and generate positive
net simulated performance with executable prices and borrow?

| Stage | Proposed NY session dates | Use |
| --- | --- | --- |
| Eligibility | 60 exchange sessions through 2023-12-29 | Rank liquidity and freeze the pair family |
| Formation | 2024-01-02 through 2024-12-31 | Estimate hedge and test the full family |
| Calibration | 2025-01-02 through 2025-10-31 | Fixed rolling stability checks and execution feasibility |
| Locked holdout | 2025-11-03 through 2026-10-30 | One evaluation after digest-bound approval |

Use a versioned NY exchange calendar and require every expected daily
observation for the statistical windows. Freeze an archived IWB holdings
file with effective date and documented publication timestamp preceding
the first formation session. Map its US-listed common stocks to permanent
instrument IDs, primary listing, and sector as known then. Retain names
that later change symbol or leave the universe. If historical holdings
or sector provenance is missing, leave development unresolved and
resolve the source or date choice before any run. Keep ETFs, funds and
non-common share types outside the family.

Require at least $10 million median daily dollar volume and a $5 or higher
unadjusted close at eligibility end. Use the last 60 sessions' unadjusted
consolidated trading volume times unadjusted close for that rank. Record
unknown eligibility separately. Take at most 30 eligible names in each
sector, ordered by descending median dollar volume with permanent ID as
the tie break. Freeze all unordered same-sector pairs of distinct
issuers and record exclusions. The selection uses membership, sector
and liquidity only. Short availability, future returns and later market
coverage have no role in choosing the family. Incomplete histories,
invalid observations and numerical failures stay in the denominators
and receive p = 1 for family correction.

Use daily log total-return series rebuilt forward from raw prices and
effective, as-of corporate actions. Prohibit retroactively applying
future actions. Align on the exchange calendar without forward fills.
Preserve timestamps, source receipts, hashes, action conventions and
delistings. For each pair use permanent-ID ordering to define A and B.
Seed the signal buffer from the same fixed-anchor total-return series.
Keep signal amounts and trade accounting distinct so cash corporate
actions enter realized simulated P&L once.
Apply EdgeLab's ADF/KPSS requirement that both legs are I(1), followed by
the existing constant, BIC, lag-cap-six Engle–Granger tests in both
directions. Take the larger directional p-value and apply Holm at 5%
to the complete frozen family. Require a 5% CUSUM non-rejection on the
formation sample. Treat the evidence as a screen for further research.

Fit **log(A) = intercept + beta × log(B) + spread** by OLS on formation.
Require beta > 0, negative ECM loading for the same orientation, and an
estimated spread AR(1) half-life from 2 through 30 sessions. Repeat the
upstream and stability checks on the trailing 180 development sessions
ending on the final NY session of August, September, and October 2025.
Require both directional p-values below 5%, CUSUM p-value at least
5%, and a positive OLS beta within 25% of the formation beta in every
window. These retention filters apply only to formation discoveries.
The current bootstrap is research-only.

## Frozen simulation and shortlist

At each session close, calculate the spread using the frozen formation
hedge and standardize it by its mean and sample SD over the prior 60
complete sessions. Permit development data to seed this trailing buffer
for the holdout. Enter after the close z-score crosses either +2 or -2
outward. Trade the two legs in opposite directions with gross dollar
weights 1 to beta. Round both legs down to whole shares and skip any
rounding that changes either target weight by more than 5 percentage
points. Use $25,000 gross pair exposure on a fixed $25,000 research
capital base, one position per pair, with shared-issuer capacity
evaluated separately if a portfolio claim is made.

At a daily close schedule an exit when the z-score is at most +0.5
after a positive entry or at least -0.5 after a negative entry. Schedule
an adverse exit if it reaches +4 after a positive entry or -4 after a
negative entry. Apply a time stop at the close of the 20th NY session
counting the session of the opening fill as the first. Monitor stability after each
month end with the trailing 180-session statistic and the same
calibration checks. Only issue new signals after that month's evidence
is available. A gate failure disables new entries and schedules any
open positions for liquidation. A holdout gate failure also prevents
the tradable label. The model hedge stays frozen. A position is flat
before another entry signal is eligible. The last signal cutoff reserves
one complete NY session for final liquidation. Price paths, skipped
signals, gate states and positions remain in the replay ledger.

Attempt fills at the next session's first synchronized regular-market
consolidated best bid/offer snapshot at or after 09:35 New York time,
through 09:40. Each quote must be current within one second, be from
eligible venues, and form a noncrossed bid/ask. Use the ask on buys
and bid on sells. Cap each simulated fill at 25% of displayed size
and 0.1% of trailing 20-session median share volume for that leg.
Skip both legs when contemporaneous quote, depth, or short evidence
fails. Open positions continue to accrue costs during exit delays.
Record halts, forced recalls, splits, ordinary and special dividends,
and delisting or cash settlements on their actual effective schedule.
Carry split-adjusted share quantities and actual dividend cash flows in
the execution ledger. An unresolved position or missing terminal mark
fails the economic verdict. Daily marks use the contemporaneous
liquidation-side bid or ask. Missing marks keep drawdown unresolved.

Before each opening short, require timestamped historical locate or
availability evidence for the exact instrument and size, as well as
a dated borrow fee. Apply continuously available borrow, observed
recall events and applicable short restrictions during the position's
life. Use documented commissions, exchange and regulatory fees,
cash or margin financing, and borrow accrued by calendar day at known
rates. Treat missing rates or unverified capacity as a feasibility
failure. Use an additional 5 basis points of adverse slippage per fill
on top of the recorded spread and explicit fees. The base replay
assumes simultaneous entry to permit comparison; the stress test
exercises legging risk.

On calibration only, require at least 12 completed round trips, 80%
simulated execution of otherwise eligible entry signals, and positive
net P&L after base costs. Run the same stress model defined below on
calibration to compute the ranking metric. The entry-fill denominator includes skipped
entry attempts due to lack of quote, depth, or borrow. Rank survivors
by formation Holm-adjusted p-value, then calibration net return after
stress, then permanent-ID pair order. Keep the first ten or fewer.
Freeze exact pair IDs, hedge estimates, rules, diagnostics and source
hashes in the approval manifest. If no pair survives, report an empty
shortlist and stop this protocol while the holdout stays locked.

## One approved holdout evaluation

A pair earns **tradable under the tested assumptions** only if its
approved holdout simulation passes all the following gates.

- Every monthly stability check passes. Required data, corporate actions,
  quotes, fees, borrow availability and terminal position records
  reconcile with the frozen inputs. At least 95% of scheduled sampling
  days have quote/depth coverage for both legs, in addition to complete
  statistical and daily mark records.
- At least 20 nonoverlapping completed round trips, at least 80% entry
  execution under the calibration denominator, net return of at least
  2% on allocated capital, and maximum peak-to-trough drawdown no greater
  than 5% of starting capital. Use chronological daily liquidation-side
  net equity and include the starting capital in the peak series.
- Under stress, net return stays strictly positive, at least 15 round
  trips close, all simulated inventory is resolved, and maximum drawdown
  stays within 7.5%. Replay using twice the recorded half-spread around
  each reference midpoint, 10 basis points of additional slippage per
  fill, twice documented borrow cost, and half the base display-size
  participation cap. Delay the second entry or exit leg by 60 seconds
  and recheck quotes and depth through the execution window. If the
  second leg lacks an eligible fill, flatten the filled leg at the
  next executable quote and charge both transactions. Record forced
  reversals, duration and P&L as legging losses. Require positive net
  results and proven capacity at the stated size only.

Report gross and net results, cost decomposition, turnover, holding
time, unhedged exposures, forced exits and all skip reasons. Report all
shortlist outcomes against a flat no-trade reference. These are fixed
economic replay gates and the empirical statement is about the
evaluated window. Any test of statistical superiority to a no-trade
baseline would need its own predeclared dependence treatment and
shortlist-wide multiplicity control. Label each
shortlisted pair **continue researching** when every gate clears,
**revise** when an observed remediable method issue invalidates the
claim, **stop** for a failed economic or stability gate, or **unresolved**
when evidence required for a verdict is missing. Use that precedence:
missing decisive evidence, then observed invalidity, then gate results.
The tradable label requires a valid complete result and the pass verdict.
The thresholds characterize this fixed historical replay and economic
claim rather than a guarantee of future performance.

## Inputs needed before approval can be requested

- Exact membership and sector archives with as-of timestamps, instrument
  history, exchange calendar, corporate actions and raw/adjusted price
  conventions. The prior screen's later IWB snapshot does not meet the
  point-in-time requirement.
- An eligible historical feed with synchronized consolidated quote
  timestamps, sizes, trade-condition definitions, halts, marks and
  delisting handling. The previous daily IEX bars do not establish these
  execution inputs.
- Historical locate inventory, available quantity, restrictions, fee and
  recall data for both possible short legs, plus a named historical
  commission, fee and financing schedule with timestamps. If records
  are unavailable, development can produce a statistical candidate
  report. The economic approval remains pending.
- Freeze the exact dates, research capital, costs, capacity and gates
  once source coverage is known. Document the available historical
  window and data rights. Only development receipts may be opened
  while preparing the candidate list.

## Bind later approval to exact scope

Prepare an immutable manifest only after reviewing the development
results. Serialize the manifest with EdgeLab's existing canonical JSON
encoding and compute its SHA-256. Include
the protocol version and content hash, immutable code revision plus
individual file hashes, dependency lock, run UUID, candidate IDs and
formation estimates, exact windows and approved fields, threshold/time
and fee configuration, development input hashes, holdout snapshot
identity and checksums furnished by the custodian, output directory,
and permitted actions. If the raw holdout needs sealing, use a separate
custodian receipt so EdgeLab can verify identity after approval without
reading its contents during development.

Present the runnable artifact and full manifest digest for separate
user approval. The approval receipt must name the user, run UUID,
digest, date, and scope of one read-only historical simulation against
the listed snapshot. Validate it and atomically record authorization
before the runner opens any holdout bytes. Read only the named paths,
windows, instruments and fields. Preserve the journal, manifest, data
and source hash checks, and signed-off outcomes. An interrupted job may
only resume its original frozen run after exact identity checks and
without publishing interim selection information. Any other access,
change in inputs or threshold, or reuse of the holdout for selection
requires a new review. Record any exposed holdout window as consumed
and reserve fresh unseen data for a revised protocol. No orders or
broker operations belong to this authorization path.
