One-use equity holdout run
==========================

The `research protocol <DISCOVERY_PROTOCOL_DRAFT.md>`_ defines the proposed
2025-11-03 through 2026-10-30 window, next-session quote replay, stressed
execution and acceptance gates. The development shortlist and custodian
snapshot must exist before an exact approval manifest can be prepared.

Freeze the manifest
-------------------

Feed ``approval.manifest_digest`` a version-1 JSON manifest. Its ``schema``
must be 1, its ``action`` must be ``read-only-historical-simulation`` and
``run_id`` is a fresh UUID. The manifest includes the following records.
``prepare_holdout.prepare(host, run_id=..., output_directory=...)`` builds
the unsigned manifest and digest from the configured development report and
custodian metadata. It does not open the sealed snapshot. Use
``prepare_holdout.manifest_bytes(manifest)`` to save the readable copy.

- ``protocol`` carries the protocol ``id`` and raw-file ``sha256``.
- ``code`` records the immutable ``revision``, ``files`` as relative path
  to SHA-256, and ``dependency_lock_sha256``. Serialize
  ``holdout_host.runtime_lock()`` in the reviewed Python environment for
  the dependency receipt. The host verifies the current numerical and
  cryptography package versions, Python, platform and SQLite. Install
  the ``cryptography`` package in that environment before freezing it.
  Include ``holdout.REQUIRED_CODE_PATHS`` in the code pins.
- ``development`` carries ``start``, ``end`` and ``inputs`` with raw byte
  SHA-256 entries for ``manifest``, ``report``, ``calendar``, ``universe``,
  ``bars`` and ``execution``. ``candidates`` is the exact ordered shortlist
  with ``pair``, formation ``intercept`` and formation ``beta`` for each pair.
- ``parameters`` is the output of ``holdout.frozen_parameters()`` for the
  reviewed release. ``output_directory`` is a relative path within the
  configured results root.
- ``holdout`` specifies ``universe`` as ``us-equity``, ``start`` and ``end``,
  a ``snapshot_id`` and ``artifacts`` with ``path``, ``fields`` and ``sha256``.
  Artifact paths and fields must match the runner's fixed contract.
  Inspect ``holdout.ARTIFACT_FIELDS`` for the exact field lists.

The three payloads use JSON envelopes with ``schema_version: 1``,
``snapshot_id``, ``classification`` (``equity-holdout`` for the market
run), ``start``, ``end``, sorted ``instrument_ids``, ``fields`` and ``data``.
The calendar data contains ``calendar`` (``holdout.CALENDAR_ID``) and
``close_ms`` (session date to close). ``holdout.holdout_calendar()``
provides the exact schedule shape. Bars and execution records nest under
instrument ID and session date. Use the same field units as the development
exports, and include both arms' required marks, action terms and borrow
evidence. The runner checks corporate actions against the price inputs and
recomputes the prior 20-session median volume from raw share volume.
The `NYSE Group cash-equity calendar announcement
<https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx>`_
specifies this window's holidays and early closes.

The custodian prepares exactly the required sessions, instruments and fields
in a separate protected directory. The custodian supplies a published
metadata receipt with ``schema``, ``snapshot_id``, ``start``, ``end``, sorted
``instrument_ids``, and ``artifacts`` with the same records plus byte
``size``. Pin the receipt's raw SHA-256 through the approved host
configuration. Use the custodian's checksums in the manifest so study
preparation never needs to read market holdout payloads.

Authenticate and execute
------------------------

Present the exact manifest and digest to the research approver. Explicit
approval of the displayed run and digest is required before the approver's
trusted signing process creates a receipt. The receipt is UTF-8 JSON with
``claims`` and base64 ``signature``. Claims comprise ``receipt_id``,
``principal``, UTC ``issued_at``, ``run_id``, ``manifest_sha256`` and
``action``. Ed25519 signs the bytes of ``holdout_host.RECEIPT_CONTEXT``
followed by canonical claims JSON, encoded as UTF-8 with sorted keys,
compact separators and finite values. Pin the 32-byte public key obtained
directly from the approver. EdgeLab provides verification only.

An operator-owned host configures ``holdout_host.LocalHoldoutHost`` with the
approver identity/public key, protected persistent SQLite authority journal,
exact protocol and source paths, runtime lock, pinned development inputs,
custodian receipt and separate sealed snapshot and output roots. Protect
all of these directories against untrusted writes and keep the same
authority journal for the entire equity scope. Then call
``host.run(manifest, approved_receipt_bytes)``. The host runs offline and
only opens verified artifacts in the approved read-only simulation. No
broker SDK or order path is required.

To recover an interrupted run, invoke the same host and exact manifest
with the original receipt and ``resume=True``. Preserve the authority
database and staged output. A completed or overlapping study cannot claim
the window again. Run publication and recovery depend on the authority
ledger and file digests. Copy existing completed output for review rather
than replaying the market snapshot for presentation.

Deployment qualification still needs the operator-owned signing path,
independently verified host inputs, and credentials or data rights for the
external custodian. Approval of source code or a draft protocol conveys no
market-data authorization.
