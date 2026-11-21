Reconstructing development-only daily history
=============================================

Run from the EdgeLab checkout. Use the `development export contract
<DEVELOPMENT_EXPORT.rst>`_ for the final bundle and its source receipt.

For a locally supplied official `SEC 2023 Q4 N-PORT archive
<https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets>`_,
the following route reads the exact IWB accession and joins the same
security-master crosswalk as the raw XML importer ::

    python -B -m research.pair_discovery.universe_nport_archive --archive PATH/2023q4_nport.zip --archive-sha256 HEX --security-master PATH/crosswalk.csv --output-dir PATH/new-universe

Holdings and identifier tables are read in place from the ZIP. Retain the
original archive hash and output coverage receipt.

The Alpaca acquisition plan identifies permanent instrument IDs and
official ticker intervals known by the applicable session. Different ticker
segments are queried with separately pinned ``asof`` dates. The collector
uses raw daily SIP bars and publishes gaps and source checksums. It ends
at October 31, 2025 or earlier. Set the plan from the historical security
master. Keep any small provider qualification probes out of market screen
inputs.

Create plan JSON with ``schema: 1``, ``window`` (``start`` and ``end``
as reviewed exchange-session dates), sorted permanent ``instruments``,
and ``aliases``. Each alias has ``instrument_id``, Alpaca ``symbol``,
inclusive ``effective_from`` and ``effective_through``, ``known_on`` and
``source``. Alias coverage must be unambiguous on each queried session.
Use the execution-independent instrument universe and report uncovered
symbol intervals. Invoke the bounded market-data-only acquisition ::

    python -B -m research.pair_discovery.alpaca_bars --plan PATH/plan.json --settings-file PATH/edgelab.env --output PATH/alpaca_bars.json --max-requests REQUEST_BUDGET --allow-provider-io

Keep the plan, provider receipt, and output hash. Use the raw prices only
after the identity and action joins. A successful request for a control
instrument establishes transport and format coverage for its exact dates.

The corporate-action packet is independent of the price packet. Its JSON
contains ``schema: 1``, ``source`` (the custodian and immutable receipt
identifier), and ``instruments`` keyed by the same permanent IDs. Each
instrument has ``covered`` (inclusive intervals of certified sessions),
``events`` (effective session to ``split``, ``distribution``, ``known_on``
and ``evidence``), and ``unresolved`` (effective session to explanation).
An event's split ratio is post-action shares per old share. Its cash
distribution is measured per post-split share. Use effective dates and
publication evidence, and reconcile unsupported actions explicitly.

The join emits a neutral action only for a session within certified
coverage where no effective event was recorded. Missing coverage, missing
prices, and unresolved actions are excluded from the CSV and enumerated
in its receipt. A modern vendor response that lists some dates or no
events does not by itself establish complete historical coverage.

Save the raw-byte SHA-256 for each independent source and run ::

    python -B -m research.pair_discovery.join_daily_history --bars PATH/alpaca_bars.json --bars-sha256 HEX --actions PATH/actions.json --actions-sha256 HEX --output PATH/new-history

The result includes ``daily_history.csv`` and a receipt containing both
source pins, the normalized CSV hash and per-instrument coverage. Use
the normalized CSV hash and provenance when preparing the screen bundle.
