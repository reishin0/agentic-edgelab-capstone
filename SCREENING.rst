EdgeLab Screening Results
=========================

This records exploratory in-sample screens run with the batch runner in
``957b6b6``. A candidate had to pass the unit-root prerequisite, Holm-adjusted
bidirectional Engle-Granger testing at 5%, and the in-sample CUSUM check.
The larger directional p-value was used for correction; blocked or
ineligible comparisons counted as p=1. No pair qualified.

FX screen
---------

The screen covered all 36 unordered combinations of nine currencies
quoted in USD. Each used 511 aligned daily ECB reference-rate observations
from 2024-01-02 through 2025-12-31. The combined 36-pair correction was a
retrospective summary of the completed batches, not independent validation.
EUR/USD versus NOK/USD had the lowest larger directional p-value (0.0280),
but it failed the combined correction and CUSUM flagged instability.
The 2026 holdout remained locked.

Equity pilot
------------

The first wave used 24 names from a 1,000-stock IWB holdings snapshot dated
2026-11-06. It obtained 209 aligned Alpaca IEX daily raw USD closes per
stock from 2025-01-02 through 2025-10-31. Of 12 preselected comparisons, nine
ran and three were blocked by in-window corporate actions: XOM/CVX,
HD/LOW, and MRK/PFE. The blocked pairs remained in the correction.
JPM/BAC had the lowest larger directional p-value (0.2675), with an
adjusted value of 1.0000. The holdout beginning 2025-11-03 remained locked.

This is not a full-universe screen: 976 names did not have histories in
the pilot. The IWB snapshot postdates the sample and is not point-in-time
2025 membership. The pilot used raw prices, not total returns. Broader
coverage requires bulk corporate-action-adjusted history and a separately
defined pair family. Neither screen assessed trade execution or profitability.
Raw market data, journals, and run artifacts are kept outside version control.
