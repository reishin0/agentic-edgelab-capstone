# Advanced MCP source

This copy comes from the sibling `advanced_mcp` project, based on commit
`edaf21b` (Add durable workflows with approval and restart-safe resume). The
November 23, 2026 EdgeLab draft candidate supplied the Python package and the
draft profile. Its changes to `code_mode.py`, `config.py`, `durable.py`, and
`gateway.py` bind draft saves to exact operator approval and require a
matching backend receipt to reconcile an uncertain save. The report profile
came from the read-only EdgeLab qualification against the same gateway.

This integration verifies receipt schemas, request bindings, and checksums
for the EdgeLab save/receipt pair. A bound authenticated refusal records a
terminal `not_saved` operation. A write with an unverified outcome requires
the canonical backend receipt before resumption.

The bundled gateway requires an explicit profile path. Install the project
in editable mode so durable workflows can bind the checkout's Python source,
QuickJS runner, and dependency lockfiles. The two supplied profiles listen on
loopback and take credentials from the operator's environment.
