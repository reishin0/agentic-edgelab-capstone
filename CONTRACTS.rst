EdgeLab message contracts and release gates
===========================================

Version 1
---------

* Plan request: mode, canonical run UUID, case or null for a frozen
  history, retrieval backend, and version. It carries no sample values.
* Execute request: mode, run UUID, exact SHA-256 plan digest, and version.
  LangGraph validates the full object before projecting into graph state.
  Existing trusted Python callers may omit version (treated as v1).
* Statistical operation: version, run UUID, approval digest, tool,
  in-sample window, and sample digest. Reservations and observations share
  this binding. A stored result cannot be used for another run or tool.
* Observation and review: the numerical result and config carry hashes;
  the Critic finding cites the complete result digest. Reporting rederives
  v1 findings and checks approved method citations against the public corpus.
  Legacy finished records remain readable under their original checks.
  Pending approvals from older source releases are not migrated.
* MCP read request: version=1 plus the fixed topic for method search; just
  version=1 for catalog or branch view. Unknown fields and versions fail
  before reading the journal. Branch scope comes from the host-selected
  journal/run, not the request. The service does not expose numerical
  execution, approval, writes, or holdout access.

Permission model
----------------

The local OS operator owns the CLI, journals, and subprocess configuration.
An approval digest binds a plan; it is not an identity credential. MCP is
local stdio, not an authenticated multi-user service. A trusted host must
choose the journal and run; do not expose this interface on a network.
Local hashes verify bindings and detect tested inconsistencies; they do
not protect against an operator who can replace the entire trusted ledger.
No holdout is opened by this release.

Acceptance
----------

In `tests/test_contract_acceptance.py`, all tested authorization denials
must precede protected work; all injected evidence changes must be caught;
and all injected blocking failures must stop at the correct stage with
no downstream calls. The reference protocol set must match in at least
95% of the fixed cases. These are deterministic regression gates, not
estimates of production failure rates.

`SIMULATION_PLAN.rst` defines the separate, larger synthetic statistical
study. Five repeated workflow runs are descriptive only; they do not
estimate false advancement or missed detection rates.
