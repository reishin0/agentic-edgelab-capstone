EdgeLab implementation cross-check
==================================

This review uses the LM Studio Architecture (1.1), Refined Agent Design
(Final, 2.1), RAG (3.1), ToT RAG (4.1), and Multi-Agent Architecture
(Revised, 5.1) checkpoint documents. These documents specify a design;
the implementation and verification scopes below are narrower.

1.1: Local research architecture
--------------------------------

The researcher chooses the pair and window. The Planner sees only
method evidence and a blinded protocol problem. Python computes the
statistics and the Reporter reads the controller's evidence record.
The loopback LM Studio connector supports opt-in Planner ranking. It is
not a requirement for statistical execution. The current Critic and
Reporter are deterministic; multi-model reporting and shared-server
operation are not verified by the historical workflow.

2.1: ReAct, statistical tools and memory
----------------------------------------

LangGraph is the single workflow authority. It separates a saved plan
from exact-digest approval, then routes one allowed tool at a time.
SQLite stores requests, results and milestone findings. Conflicting
diagnostics or missing evidence prevent dependent calls. The historical
adapter freezes paired daily closes against the session calendar; it
does not fetch the later holdout. This release does not implement
holdout execution/spent accounting, Johansen or Gregory-Hansen
follow-ups, or LiteLLM/OpenRouter failover.

3.1: Method grounding
---------------------

The local eight-note corpus supports a credential-free method gate
and traceable citations; the optional Pinecone connector has separate
embedding, filtering and reranking. Local corpus evidence is not proof
of a live Pinecone run. Retrieved method evidence cannot replace a tool
result or waive approval. Quote verification and version/access
governance beyond the existing public-corpus contract are not claimed.

4.1: Plan-only search and interfaces
-----------------------------------

Width-two, depth-four beam search records retained and pruned protocol
nodes without accessing prices. Current alternatives are deliberately
narrower than the checkpoint example. MCP's stdio catalog and
host-bound planning trace do not expose sample values or dispatch
statistics. Optional CrewAI role tasks and LCEL method retrieval run
under LangGraph. The `bundled Advanced MCP gateway <integrations/advanced_mcp/README.md>`_
adds authenticated synthetic report reads and operator-approved draft saves
in separate loopback profiles. Reconciliation and resume apply to its scope
of draft operations and their backend receipts.

5.1: Collaboration and delivery
-------------------------------

Proposal, evidence review and reporting are separate responsibilities.
LangGraph controls coordination; the operator approves the exact frozen
candidate through the CLI. A stat-critic-v1 finding names the rule and
links the recorded result digest. The controller owns the verdict, and
the read-only report command checks the stored links and emits the
report and reproducibility ledger without reconstructing or exporting
raw bars. Local CLI approval is not multi-user authentication. Durable
reservation/result handling is implemented; native graph checkpoints,
authenticated escalation tickets and matched-budget comparisons remain
open.

Operating scope
---------------

Only the in-sample paired relationship workflow is in scope here.
Market news and options remain independent observational context; no
data from those caches affect the protocol or verdict. The public demo
uses synthetic inputs and does not open credentials or provider caches.
No market recommendation, trading action, model service restart, or
holdout authorization follows from an in-sample result.
