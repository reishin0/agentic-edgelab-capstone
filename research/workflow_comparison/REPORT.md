# Matched-budget workflow comparison

**Verdict for this benchmark: insufficient improvement.** The CrewAI
roles improved the mean score by 2.08 percentage points over the
single-agent arm, below the frozen 10-point threshold. The paired
comparison had five wins, three losses, and sixteen ties (one-sided
exact sign-test p = 0.3633). The latency gate passed. Keep the
deterministic tools and rules authoritative.

| Arm | Mean score | Verdict | Flaw | Evidence | Reproduction | p95 per case |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Deterministic | 100% | 24/24 | 24/24 | 24/24 | 24/24 | <0.001 s |
| Single agent | 28.13% | 2/24 | 4/24 | 9/24 | 12/24 | 22.75 s |
| CrewAI roles | 30.21% | 5/24 | 3/24 | 9/24 | 12/24 | 27.85 s |

All 24 paired cases completed with Qwen2.5 1.5B Instruct Q4_K_M on a
private CPU-only worker. Each agent arm made 72 model calls, with
complete usage and zero structured-response failures. The single-agent
arm used 69,046 input tokens, 887 output tokens and 470.65 seconds of
wall time. The CrewAI arm used 69,841 input tokens, 729 output tokens
and 611.67 seconds. The shared deterministic calculations took about
3.02 seconds across 49 tool calls. API charges were $0.

The deterministic script directly implements the scored rules, creating
a 100% ceiling. Twenty of 24 expected verdicts were `revise`. The CrewAI
arm reused production role instructions, including a Reporter instruction
about presentation order during a scratch task about evidence selection.
The benchmark measures structured conformance of these specific roles
and model. The [next-stage design](NEXT_STAGE.rst) uses independently
adjudicated cases and open-ended scoring to test whether the agents add
research value beyond that checklist.

The saved bundle reproduced the installed scorer's result across all 72
blinded reports, and the transport trace matched each arm's model and
token counts. The market holdout remained locked.

- Bundle fingerprint
  `3412ae29b5476f2262564123982e73c53ab7e5b7fa6c5728c0a133cfc7a5646d`
- Model weights SHA-256
  `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`
- Reproduction commands and resource limits are in [the protocol](PROTOCOL.rst).
