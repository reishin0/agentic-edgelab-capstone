Matched-budget EdgeLab workflow evaluation
==========================================

The comparison tests whether distinct Planner, Critic and Reporter roles
improve advisory research reports. One arm uses EdgeLab's CrewAI role
adapter. The single-agent arm makes the same three decisions with one model
identity, using an explicit handoff of its preceding answers. The deterministic
arm applies the same published rules directly. The model arms share the
model ID, schema, phase prompts and answer options. CrewAI's existing role
framing and task execution are the treatment. This is a controlled comparison
of the advisory layer. Its scratch reports have no execution or approval
authority. It does not run the production LangGraph protocol-selection path.

Frozen evaluation
-----------------

Use 24 paired synthetic cases: six generated examples each of a shared trend,
independent walks, changing slope, and an intercept shift. Use only the 180
development observations generated per case. Each case also presents a
simulated request with either no flaw or one of five flaws. The study rotates
through all six proposal types four times. Sample family names, seeds, proposal
labels, answer key, and competing reports are absent from the model prompts.
Case order, option order, arm order, and opaque report identifiers are
randomized reproducibly. All arms receive the same data through the same
guarded statistical tools, identical compact test values and fingerprints,
grounding notes, and benchmark-specific acceptance rules. Stages stop as soon
as a canonical prerequisite fails. All arms receive the exact shared evidence
and cannot dispatch more numerical tools. A broken request requires a
revision even if the corrected reference analysis favors continuation.

Record four equally weighted exact-match checks against the frozen answer key:
proposed verdict, flawed proposal diagnosis, citation of the complete reached
tool sequence, and identification of the bound reproduction manifest. A
report missing a valid phase gets zero on that phase's checks. Model/provider
failures remain in the denominator. The scorer evaluates every arm-blind
report first, then joins results to the allocation key. Tool results, original
prompt/choice traces, allocation, and frozen fixtures allow independent
inspection and deterministic rescoring. These are rubric-based measures;
the study does not measure open-ended prose quality or production deployment.

Resource budgets and decision
-----------------------------

Each model arm gets exactly three allowed calls per case, each capped at 128
completion tokens, 15 seconds and a 6,000-character prompt. Both arms use
temperature zero. The shared calculation budget is three tool calls per
case with the same early stop. Report actual attempted model calls, provider
usage where reported, arm and shared-calculation wall time, Python process
CPU time, and API charges (zero for the designated local endpoint). Machine
energy and local infrastructure cost are outside this measurement.

The verdict is unresolved for stubs, smoke subsets, incomplete usage
accounting or model response failures. Report scores and failures even in
those runs. To qualify, multi-agent mean score must improve by at least ten
percentage points against both baselines. For both paired comparisons require
a one-sided exact sign-test p-value at most 0.025 on non-tied case scores.
Multi-agent verdict and evidence accuracy must each meet or exceed both
baselines. The multi-agent p95 per-case wall time must be at most twice the
single-agent p95 and at most 60 seconds. The thresholds and cases are fixed
before execution. A deterministic ceiling is a valid outcome: if direct
rules already solve the structured task, retain them and use this benchmark
to test whether advisory roles introduce regressions.

Reproduction
------------

Run from the EdgeLab source root in the isolated environment containing
the existing research and crew extras ::

    $env:CREWAI_DISABLE_TELEMETRY = "true"
    $env:OTEL_SDK_DISABLED = "true"
    $env:PYTHON_DOTENV_DISABLED = "1"
    $env:LANGSMITH_TRACING = "false"
    $env:LANGCHAIN_TRACING_V2 = "false"
    python -m research.workflow_comparison.benchmark run --mode stub --output pilot
    python -m research.workflow_comparison.benchmark score --input pilot

For full model measurement use a dedicated, preloaded loopback LM Studio
endpoint with a single explicit model ID. The benchmark does not start or
reconfigure a model server ::

    python -m research.workflow_comparison.benchmark run --mode lm-studio --base-url http://127.0.0.1:1234/v1 --model MODEL_ID --output measured
    python -m research.workflow_comparison.benchmark score --input measured

Provide a new output directory for each run. The --smoke flag uses four
cases to check the transport and artifact paths. It never qualifies. The
market holdout stays locked.
