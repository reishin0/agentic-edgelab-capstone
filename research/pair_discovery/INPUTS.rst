EdgeLab equity discovery inputs
===============================

Run from the EdgeLab checkout with the research dependencies installed ::

    python -B -m research.pair_discovery.screen --manifest PATH/manifest.json --output PATH/new-report.json
    python -B -m unittest discover -s research/pair_discovery -v

This runner accepts frozen development exports with no provider I/O. The
source normalization and universe import are documented in
`DEVELOPMENT_EXPORT.rst <DEVELOPMENT_EXPORT.rst>`_. The
version-1 manifest has ``schema_version``, ``classification``
(``equity-development`` or ``synthetic-development``), ``through``
(``2025-10-31``), ``holdout_start`` (``2025-11-03``), and ``files``.
Each file record has ``name``, ``sha256`` (raw bytes), ``source`` (including
the immutable source receipt identifier) and ``through``. The exporter must
produce physically separate development files, even where the source holds
more dates. The metadata, path and byte checks do not authenticate publisher
claims. A real run requires an independently verified export and provenance.

- ``calendar`` uses ``calendar.json`` with ``calendar`` (source/calendar ID)
  and ``sessions`` (sorted ISO dates). The runner validates the last 60
  eligibility sessions in 2023, all 252 formation sessions in 2024, and
  209 calibration sessions in 2025. July 3, 2025 closes at 13:00 New York time.
- ``universe`` uses ``universe.json`` with ``index`` (``IWB``), ``effective``,
  ``published`` and ``members``. Member records have ``id`` (permanent
  instrument ID), ``issuer`` (permanent issuer ID), ``kind`` (``common_stock``
  for eligible shares), ``sector``, ``country`` (``US`` for eligible
  instruments), ``primary_listing`` and ``known_on``. Unknown classification
  is an export gap to resolve before selection.
- ``bars`` uses ``bars.json``, keyed by instrument ID and then session date.
  Records have ``close`` (raw dollars per share), ``volume`` (unadjusted
  consolidated shares), ``split`` (post-action shares per prior share,
  normally 1), ``distribution`` (effective dollar amount per post-action
  share, normally 0) and ``action_known_on`` (date). Export cash/stock
  corporate actions with verified economic terms. An action that cannot be
  represented by this contract must be treated as an incomplete history.
- Optional ``execution`` uses ``execution.json``, keyed by instrument ID
  and calibration session. Records have ``quotes``, ``mark`` (a quote or
  null), ``median_volume`` (trailing 20 sessions, ending before today),
  ``borrow``, ``financing_bps``, ``commission_per_share`` and ``sell_fee_bps``.
  A quote includes ``at_ms``, ``bid``, ``ask``, ``bid_size``, ``ask_size``, and
  optional ``eligible``. Borrow records include ``at_ms``,
  ``available_shares``, ``rate_bps``, and optional ``restricted`` and
  ``recalled``. Intraday times are New York wall-clock milliseconds, prices
  dollars, quantities shares, borrow/financing annual bps, and sell fees
  bps per fill. The optional corporate-action fields are ``halted``,
  ``split_ratio``, ``cash_in_lieu`` (price), ``dividend`` (per current
  share) and ``settlement`` (per current share). Pass only source-qualified
  bid/ask quotes, including invalidation updates and applicable trading
  restrictions. Verify consistency between signal and execution action feeds.

Report output includes the complete tested family, formation evidence, the
Holm adjustment, rolling diagnostics, and conditional execution ledgers and
development shortlist. The development report cannot promote a tradable pair.

The `NYSE published calendar <https://ir.theice.com/press/news-details/2022/NYSE-Group-Announces-2023-2024-and-2025-Holiday-and-Early-Closings-Calendar/default.aspx>`_
and the `January 9 closure <https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx>`_
are the development session references.

Holdout preparation
-------------------

The protocol draft defines economic gates and a digest-bound approval. See
`HOLDOUT_RUNBOOK.rst <HOLDOUT_RUNBOOK.rst>`_ for the frozen manifest format,
custodian responsibilities and operator-owned receipt verification. The
development CLI has no holdout mode.
