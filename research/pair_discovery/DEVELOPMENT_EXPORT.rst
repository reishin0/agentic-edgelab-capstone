Preparing a development-only equity export
==========================================

Use ``universe.json`` from a verified pre-formation IWB inventory and
historical security master. Keep permanent IDs consistent across membership,
prices, actions and execution evidence. The prepared screen input expects
cash equity and listing classifications known by December 29, 2023. The
latest fund holdings or provider asset listings cannot establish those facts.

Use the raw ``primary_doc.xml`` from the `September 2023 SEC filing
<https://www.sec.gov/Archives/edgar/data/1100663/000175272423265165>`_ and
a dated crosswalk. The command writes coverage and produces ``universe.json``
when the complete candidate-equity inventory is resolved ::

    python -B -m research.pair_discovery.universe_ingest --filing-xml PATH/primary_doc.xml --security-master PATH/crosswalk.csv --output-dir PATH/new-universe

Crosswalk header ::

    cusip,isin,instrument_id,issuer_id,kind,sector,country,primary_listing,alias_from,alias_to,class_from,class_to,known_on,source

Supply one or both filing identifiers as applicable, permanent identifiers,
the listing country and MIC, and the timestamped source receipt. Interval
end dates are exclusive and can be blank. ``known_on`` must attest to
availability before the eligibility cutoff. Run without ``--security-master``
to inventory unmatched filing positions. Record the coverage output and
exact universe SHA-256 in the downstream source receipt.

Keep the following normalized files next to a source receipt ::

    universe.json
    daily_history.csv
    execution.json    # optional until statistical survivors are known

The daily CSV header is ::

    instrument_id,session,raw_close_usd,consolidated_volume,split_ratio,distribution_usd_per_post_split_share,action_known_on

Use the raw daily close in USD and unadjusted total market share volume.
The action fields require a contemporaneous corporate-action source. The
neutral action is a 1:1 split ratio and zero distribution. If the provider
cannot establish action coverage, leave the affected observation absent.
The runner reports absent dates and retains incomplete formation pairs in
the original Holm denominator. See ``INPUTS.rst`` for execution record fields.

The UTF-8 JSON receipt has ``schema: 1``, ``classification``
(``equity-development`` or ``synthetic-development``) and ``files``. Use
keys ``universe``, ``daily_history``, and optional ``execution``. Each value
contains ``name`` from the filenames above, a lowercase raw byte ``sha256``,
``source`` naming the source and receipt, and ``through`` in ISO date form.
Use the effective knowledge cutoff for the universe and the actual last
covered session for each vendor export. The universe cutoff cannot exceed
December 29, 2023. The last development session is October 31, 2025.

From the EdgeLab checkout, run ::

    python -B -m research.pair_discovery.prepare_development --receipt PATH/receipt.json --output PATH/new-development-bundle
    python -B -m research.pair_discovery.screen --manifest PATH/new-development-bundle/manifest.json --output PATH/new-screen-report.json

The preparer checks declared coverage before opening any data, rejects
sessions and permanent IDs outside scope, and records input digests and
per-instrument gaps. Keep the original source receipt alongside the
generated report and its inputs. Where vendor history extends into the
holdout, ask the custodian for a development-only export before invoking the
preparer. A SHA-256 pins bytes. Source eligibility and export scope depend
on the custodian's independently verified records.
