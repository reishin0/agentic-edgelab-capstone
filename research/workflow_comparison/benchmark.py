"""Paired, arm-blind benchmark of EdgeLab's synthetic research advisory workflow."""

import argparse
from dataclasses import asdict
from datetime import date, timedelta
from hashlib import sha256
from importlib.metadata import version
import json
from math import comb, ceil
import os
from pathlib import Path
import random
from statistics import mean
from time import perf_counter, process_time

from edgelab.contracts import fingerprint
from edgelab.crew_roles import CrewRoles
from edgelab.methods import CORPUS_SHA256, local_notes
from edgelab.providers import LMStudio, ProviderError, ROLES, post_json
from edgelab.research import release_fingerprint
from edgelab.stat_tools import ALPHA, CASES, Point, TOOL_SEQUENCE, assess, compute, numerical_runtime, window_for_demo


VERSION = "workflow-comparison-v1"
SEED = 9184831
N_CASES = 24
TIMEOUT = 15
MAX_COMPLETION_TOKENS = 128
MAX_PROMPT_CHARS = 6000
MODEL_CALLS = 3
MIN_IMPROVEMENT = 0.10
MAX_SIGN_P = 0.025
MAX_P95_SECONDS = 60.0
MAX_P95_RATIO = 2.0
HOLDOUT = "locked and untested"
ARMS = ("deterministic", "single_agent", "multi_agent")
VERDICTS = ("continue", "revise", "stop", "unresolved")
MEASURES = ("verdict", "flaw", "evidence", "reproducibility")
APPROVED_REQUEST = {
    "prerequisite": "Confirm both series are I(1) via ADF and KPSS.",
    "lag_policy": "Use BIC selected before looking at data.",
    "relationship": "Require rejection in both Engle-Granger directions.",
    "validation": "Report development only and keep market holdout locked.",
    "stability_claim": "Treat CUSUM non-rejection as limited evidence.",
}
REQUESTS = (
    ("no_flaw", None, None),
    ("skip_i1", "prerequisite", "Proceed directly to Engle-Granger because correlation is high."),
    ("cherry_pick_lags", "lag_policy", "After viewing results use whichever of AIC and BIC gives a smaller p-value."),
    ("choose_direction", "relationship", "Keep the direction with the smaller p-value and ignore the other."),
    ("reuse_development", "validation", "Report the same development observations as an out-of-sample evaluation."),
    ("overclaim_stability", "stability_claim", "Call stability established whenever CUSUM fails to reject."),
)
RULES = (
    "Benchmark advisory only. Identify any single deviation from the approved request. A deviating request"
    " requires revise. For a compliant request use the numeric evidence. With alpha 0.05, stationarity"
    " requires ADF p < alpha <= KPSS p. I(1) requires KPSS p < alpha <= ADF p on levels and stationarity"
    " on differences. If either order is inconclusive use unresolved, if both I(0) use revise, and if the"
    " orders differ use stop. Both I(1) are needed to reach bidirectional Engle-Granger. Both p >= alpha"
    " means stop; one p < alpha means unresolved. If both p < alpha, CUSUM p < alpha means stop and"
    " p >= alpha means continue researching with limited evidence. Cite exactly the sequence of reached"
    " tools. Select the reproduction manifest matching the sealed sample, fixed lag rule and 180 development"
    " observations. Market holdout remains locked."
)


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def frozen_protocol() -> dict:
    location = Path(__file__)
    return {
        "version": VERSION, "seed": SEED, "full_cases": N_CASES, "holdout": HOLDOUT, "rules": RULES,
        "source_sha256": release_fingerprint(), "corpus_sha256": CORPUS_SHA256,
        "runner_sha256": sha256(location.read_bytes()).hexdigest(),
        "protocol_sha256": sha256(location.with_name("PROTOCOL.rst").read_bytes()).hexdigest(),
        "runtime": {**numerical_runtime(), "crewai": version("crewai")},
        "resource_limits": {
            "model_calls": MODEL_CALLS, "completion_tokens_per_call": MAX_COMPLETION_TOKENS,
            "prompt_characters_per_call": MAX_PROMPT_CHARS, "seconds_per_call": TIMEOUT,
            "tools_per_case": len(TOOL_SEQUENCE), "temperature": 0,
        },
        "decision_limits": {
            "min_improvement": MIN_IMPROVEMENT, "max_sign_p": MAX_SIGN_P,
            "max_p95_seconds": MAX_P95_SECONDS, "max_p95_ratio": MAX_P95_RATIO,
        },
    }


def fixtures(smoke: bool) -> list[dict]:
    rng = random.Random(SEED)
    cases = []
    for family_index, family in enumerate(CASES):
        for index, (flaw, field, replacement) in enumerate(REQUESTS):
            request = dict(APPROVED_REQUEST)
            if field is not None:
                request[field] = replacement
            cases.append({
                "id": f"case-{rng.getrandbits(60):015x}", "sample_seed": rng.getrandbits(63),
                "family": family, "flaw": flaw, "request": request,
                "smoke": index == (0, 0, 2, 4)[family_index],
            })
    rng.shuffle(cases)
    return [case for case in cases if case["smoke"] or not smoke]


def synthetic_sample(case: dict) -> tuple[Point, ...]:
    rng = random.Random(case["sample_seed"])
    origin = date.fromisoformat(window_for_demo().start)
    x = y = spread = 0.0
    rows = []
    for index in range(180):
        x += rng.gauss(0, 1)
        y += rng.gauss(0, 1)
        spread = 0.40 * spread + rng.gauss(0, 0.6)
        slope = -1.2 if case["family"] == "structural-break" and index >= 95 else 1.3
        intercept = 1.2 if case["family"] == "spread-level-shift" and index >= 90 else 0
        b = y if case["family"] == "independent" else slope * x + spread + intercept
        rows.append(Point(str(origin + timedelta(days=index)), x, b))
    return tuple(rows)


def compact_evidence(tool: str, result: dict) -> dict:
    if tool == "diagnostics":
        return {leg: {stage: {k: result[leg][stage][k] for k in ("adf_p", "kpss_p")}
                      for stage in ("level", "difference")} for leg in ("a", "b")}
    if tool == "engle_granger":
        return {leg: result[leg]["p"] for leg in ("a_on_b", "b_on_a")}
    return {k: result[k] for k in ("cusum_p", "ecm_loading", "ecm_loading_p", "ou_half_life_observations")}


def evaluate_case(case: dict) -> dict:
    started, cpu = perf_counter(), process_time()
    rows = synthetic_sample(case)
    window = window_for_demo()
    observations, compact = {}, {}
    for tool in TOOL_SEQUENCE:
        observation = compute(tool, rows, window, "BIC")
        observations[tool] = observation
        compact[tool] = compact_evidence(tool, observation["result"])
        finding = assess(tool, observation)
        if finding["decision"] != "proceed":
            break
    diagnostic_config = observations["diagnostics"]["config"]
    sample = diagnostic_config["sample_sha256"]
    manifest = {
        "sample_sha256": sample, "lag_policy": "BIC", "n": len(rows),
        "window": asdict(window), "holdout": HOLDOUT,
    }
    rng = random.Random(case["sample_seed"])
    distractor = ({"lag_policy": "AIC"} if rng.getrandbits(1) else
                  {"sample_sha256": fingerprint({"different_sample": sample})})
    manifests = [manifest, {**manifest, **distractor}]
    rng.shuffle(manifests)
    label = {f"manifest_{index + 1}": choice for index, choice in enumerate(manifests)}
    evidence = "+".join(observations)
    gold = {"verdict": "revise" if case["flaw"] != "no_flaw" else finding["decision"],
            "flaw": case["flaw"], "evidence": evidence,
            "reproducibility": next(key for key, choice in label.items() if choice == manifest)}
    return {
        "observations": observations, "compact": compact, "sample_sha256": sample,
        "manifests": label, "gold": gold, "tools": len(observations),
        "wall_seconds": perf_counter() - started, "cpu_seconds": process_time() - cpu,
    }


def options_in_order(options: tuple[str, ...], seed: int, phase: int) -> tuple[str, ...]:
    return tuple(random.Random(seed + phase).sample(options, len(options)))


def phase_questions(case: dict, evaluation: dict):
    context = {
        "rules": RULES, "alpha": ALPHA, "request": case["request"], "approved_request": APPROVED_REQUEST,
        "evidence": evaluation["compact"], "sample_sha256": evaluation["sample_sha256"],
        "method_notes": {note.id: note.text for note in local_notes("cointegration")},
    }
    answers = {}
    issue_labels = {flaw: field for flaw, field, _ in REQUESTS}
    yield "planner", {**context, "task": "Classify the proposal issue using these labels.", "issue_labels": issue_labels}, (
        options_in_order(tuple(issue_labels), case["sample_seed"], 0)), answers
    yield "critic", {**context, "task": "Check the preceding issue decision and propose the appropriate research verdict.",
                     "preceding_issue": answers.get("planner")}, (
        options_in_order(VERDICTS, case["sample_seed"], 1)), answers
    paths = tuple("+".join(TOOL_SEQUENCE[:index]) for index in range(1, len(TOOL_SEQUENCE) + 1))
    # The labels are opaque; their meanings and order are shared by both model arms.
    reports = {
        f"selection_{index}": (path, manifest) for index, (path, manifest) in enumerate(
            ((path, manifest) for path in paths for manifest in evaluation["manifests"]), start=1)
    }
    yield "reporter", {**context, "task": "Cite all reached tools and select the bound reproduction manifest.",
                       "preceding_issue": answers.get("planner"), "preceding_verdict": answers.get("critic"),
                       "manifests": evaluation["manifests"], "report_options": reports}, (
        options_in_order(tuple(reports), case["sample_seed"], 2)), answers


class MeteredTransport:
    def __init__(self, mode: str, *, reasoning_off: bool = False) -> None:
        if reasoning_off and mode != "lm-studio":
            raise ValueError("Reasoning-off mode requires a local LM Studio endpoint")
        self.mode = mode
        self.reasoning_off = reasoning_off
        self.calls: list[dict] = []

    def __call__(self, url: str, headers: dict, payload: dict, timeout: float) -> dict:
        if (len(self.calls) >= MODEL_CALLS or timeout > TIMEOUT or payload["max_tokens"] != MAX_COMPLETION_TOKENS
                or payload["temperature"] != 0
                or len(payload["messages"][-1]["content"]) > MAX_PROMPT_CHARS):
            raise ProviderError("Model request exceeded benchmark limits")
        if self.reasoning_off:
            payload = {**payload, "reasoning_effort": "none"}
        call = {"request": payload, "timeout_seconds": timeout}
        self.calls.append(call)
        started = perf_counter()
        try:
            if self.mode == "stub":
                choice = payload["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"][0]
                reply = {"choices": [{"message": {"content": json.dumps({"choice": choice})}}]}
            else:
                reply = post_json(url, headers, payload, timeout)
            usage = reply.get("usage", {})
            call["usage"] = {key: usage[key] for key in ("prompt_tokens", "completion_tokens")
                             if isinstance(usage, dict) and type(usage.get(key)) is int and usage[key] >= 0}
            if self.reasoning_off:
                verify_reasoning_off(call, reply)
            if call["usage"].get("completion_tokens", 0) > MAX_COMPLETION_TOKENS:
                raise ProviderError("Model returned usage beyond the completion limit")
            return reply
        except ProviderError:
            call["transport_failed"] = True
            raise
        finally:
            call["wall_seconds"] = perf_counter() - started


def verify_reasoning_off(call: dict, reply: dict) -> None:
    choices = reply.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    usage = reply.get("usage")
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    call["response_receipt"] = {"finish_reason": finish_reason, "reasoning_tokens": reasoning_tokens}
    if not isinstance(choice, dict):
        raise ProviderError("LM Studio returned a malformed choices array")
    if finish_reason != "stop":
        raise ProviderError("LM Studio did not report a normal completion")
    if (reply.get("model") != call["request"]["model"]
            or type(reasoning_tokens) is not int or reasoning_tokens != 0):
        raise ProviderError("LM Studio did not verify reasoning was off for the selected model")


def deterministic_report(case: dict, evaluation: dict) -> dict:
    flaw = next((label for label, field, _ in REQUESTS
                 if field is not None and case["request"][field] != APPROVED_REQUEST[field]), "no_flaw")
    evidence = "+".join(evaluation["observations"])
    tool, result = list(evaluation["observations"].items())[-1]
    verdict = "revise" if flaw != "no_flaw" else assess(tool, result)["decision"]
    label = next(key for key, manifest in evaluation["manifests"].items()
                 if manifest["sample_sha256"] == evaluation["sample_sha256"] and manifest["lag_policy"] == "BIC")
    return {"verdict": verdict, "flaw": flaw, "evidence": evidence, "reproducibility": label}


def model_report(model, case: dict, evaluation: dict) -> tuple[dict, list[dict]]:
    trace = []
    report = {key: None for key in MEASURES}
    for role, context, options, answers in phase_questions(case, evaluation):
        prompt = json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False)
        try:
            selection = model.choose(role, prompt, options, TIMEOUT)
        except (ValueError, ProviderError) as error:
            trace.append({"phase": role, "error": str(error)})
            continue
        answers[role] = selection
        trace.append({"phase": role, "choice": selection})
        if role == "reporter":
            report["evidence"], report["reproducibility"] = context["report_options"][selection]
        else:
            report["flaw" if role == "planner" else "verdict"] = selection
    return report, trace


def arm_result(arm: str, case: dict, evaluation: dict, mode: str, base_url: str, model_id: str, *,
               reasoning_off: bool = False) -> tuple[dict, dict]:
    transport = MeteredTransport(mode, reasoning_off=reasoning_off)
    started, cpu = perf_counter(), process_time()
    if arm == "deterministic":
        report, trace = deterministic_report(case, evaluation), []
    else:
        model = LMStudio(base_url, {role: model_id for role in ROLES}, transport=transport)
        report, trace = model_report(CrewRoles(model) if arm == "multi_agent" else model, case, evaluation)
    usage = [call.get("usage", {}) for call in transport.calls]
    return report, {
        "wall_seconds": perf_counter() - started, "cpu_seconds": process_time() - cpu,
        "tool_calls_shared": evaluation["tools"], "model_calls": len(transport.calls),
        "prompt_tokens_reported": sum(call.get("prompt_tokens", 0) for call in usage),
        "completion_tokens_reported": sum(call.get("completion_tokens", 0) for call in usage),
        "missing_usage": sum(len(call) != 2 for call in usage), "api_cost_usd": 0,
        "phase_trace": trace, "transport": transport.calls,
    }


def run(output: Path, mode: str, base_url: str, model_id: str, smoke: bool, *, reasoning_off: bool = False) -> None:
    if (os.getenv("CREWAI_DISABLE_TELEMETRY", "").lower() not in {"true", "1"}
            or os.getenv("PYTHON_DOTENV_DISABLED") != "1" or os.getenv("OTEL_SDK_DISABLED", "").lower() != "true"):
        raise ValueError("Use the isolated process settings specified in PROTOCOL.rst")
    if reasoning_off and mode != "lm-studio":
        raise ValueError("Reasoning-off mode requires a local LM Studio endpoint")
    protocol = frozen_protocol()
    cases = fixtures(smoke)
    study = {
        "protocol": protocol, "protocol_fingerprint": fingerprint(protocol), "fixtures": cases,
        "fixture_fingerprint": fingerprint(cases), "mode": mode, "model_id": model_id,
        "loopback_base_url": base_url if mode == "lm-studio" else None, "smoke": smoke,
    }
    if reasoning_off:
        study["reasoning_off"] = True
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "study.json", study)
    rng = random.Random(SEED + 1)
    reports, allocation, answer_key, evidence = {}, {}, {}, {}
    for case in cases:
        case_id = case["id"]
        evaluation = evaluate_case(case)
        answer_key[case_id] = evaluation["gold"]
        evidence[case_id] = {key: value for key, value in evaluation.items() if key != "gold"}
        for arm in rng.sample(ARMS, len(ARMS)):
            report_id = f"{rng.getrandbits(100):025x}"
            report, execution = arm_result(arm, case, evaluation, mode, base_url, model_id,
                                           reasoning_off=reasoning_off)
            reports[report_id] = {"case_id": case_id, **report}
            allocation[report_id] = {"arm": arm, **execution}
        print(f"Completed {len(evidence)}/{len(cases)} paired cases", flush=True)
    bundle = {"blind_reports": reports, "answer_key": answer_key, "allocation": allocation, "tool_evidence": evidence}
    for name, artifact in bundle.items():
        write_json(output / f"{name}.json", artifact)
    write_json(output / "complete.json", {name: fingerprint(data) for name, data in {"study": study, **bundle}.items()})


def blinded_scores(reports: dict, answer_key: dict) -> dict:
    scores = {}
    for report_id, report in reports.items():
        gold = answer_key[report["case_id"]]
        scores[report_id] = {name: int(report[name] == gold[name]) for name in MEASURES}
    return scores


def compare(multi_agent: dict, baseline: dict) -> dict:
    deltas = [sum(multi_agent[case_id].values()) - sum(baseline[case_id].values()) for case_id in multi_agent]
    wins = sum(delta > 0 for delta in deltas)
    losses = sum(delta < 0 for delta in deltas)
    discordant = wins + losses
    p = sum(comb(discordant, k) for k in range(wins, discordant + 1)) / (2 ** discordant)
    return {"mean_improvement": mean(deltas) / len(MEASURES), "wins": wins, "losses": losses,
            "ties": len(deltas) - discordant, "sign_p_one_sided": p}


def score(output: Path) -> dict:
    expected = read_json(output / "complete.json")
    artifacts = {name: read_json(output / f"{name}.json")
                 for name in ("study", "blind_reports", "answer_key", "allocation", "tool_evidence")}
    if {name: fingerprint(artifact) for name, artifact in artifacts.items()} != expected:
        raise ValueError("Benchmark bundle failed its integrity checks")
    study, reports, answer_key, allocation, evidence = artifacts.values()
    by_report = blinded_scores(reports, answer_key)
    methods = {arm: {} for arm in ARMS}
    timings = {arm: [] for arm in ARMS}
    for report_id, points in by_report.items():
        record = allocation[report_id]
        case_id = reports[report_id]["case_id"]
        method = record["arm"]
        if case_id in methods[method]:
            raise ValueError("Duplicate case within benchmark arm")
        methods[method][case_id] = points
        timings[method].append(record)
    if any(set(cases) != set(answer_key) for cases in methods.values()):
        raise ValueError("Benchmark cases are incomplete")
    totals = {}
    for arm, cases in methods.items():
        metrics = {name: mean(points[name] for points in cases.values()) for name in MEASURES}
        resources = timings[arm]
        elapsed = sorted(record["wall_seconds"] for record in resources)
        model_failures = sum(any("error" in phase for phase in record["phase_trace"]) for record in resources)
        totals[arm] = {
            "accuracy": metrics, "mean_score": mean(metrics.values()), "p95_seconds": elapsed[ceil(.95 * len(elapsed)) - 1],
            "failed_cases": model_failures,
            **{key: sum(record[key] for record in resources) for key in (
                "wall_seconds", "cpu_seconds", "model_calls", "tool_calls_shared", "missing_usage",
                "prompt_tokens_reported", "completion_tokens_reported", "api_cost_usd",
            )},
        }
    comparisons = {arm: compare(methods["multi_agent"], methods[arm]) for arm in ARMS if arm != "multi_agent"}
    multi = totals["multi_agent"]
    limits = study["protocol"]["decision_limits"]
    tested = (study["mode"] == "lm-studio" and not study["smoke"] and len(answer_key) == N_CASES
              and all(not totals[arm]["missing_usage"] and not totals[arm]["failed_cases"]
                      and totals[arm]["model_calls"] == MODEL_CALLS * len(answer_key)
                      for arm in ("single_agent", "multi_agent")))
    quality = all(
        row["mean_improvement"] >= limits["min_improvement"] and row["sign_p_one_sided"] <= limits["max_sign_p"]
        and all(multi["accuracy"][key] >= totals[arm]["accuracy"][key] for key in ("verdict", "evidence"))
        for arm, row in comparisons.items()
    )
    cost = multi["p95_seconds"] <= min(limits["max_p95_seconds"],
                                       limits["max_p95_ratio"] * totals["single_agent"]["p95_seconds"])
    return {
        "version": study["protocol"]["version"], "mode": study["mode"], "smoke": study["smoke"],
        "case_count": len(answer_key), "holdout": HOLDOUT, "bundle_fingerprint": fingerprint(expected),
        "qualification": "unresolved" if not tested else "passed" if quality and cost else "insufficient improvement",
        "quality_gate": quality, "latency_gate": cost, "arms": totals, "comparisons": comparisons,
        "shared_calculations": {key: sum(row[key] for row in evidence.values())
                                for key in ("tools", "wall_seconds", "cpu_seconds")},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--mode", required=True, choices=("stub", "lm-studio"))
    runner.add_argument("--base-url")
    runner.add_argument("--model")
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--smoke", action="store_true")
    runner.add_argument("--reasoning-off", action="store_true",
                        help="Ask LM Studio for reasoning off and verify its response receipts")
    scorer = commands.add_parser("score")
    scorer.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "score":
        print(json.dumps(score(args.input), indent=2, allow_nan=False))
        return
    if args.mode == "lm-studio":
        if not args.base_url or not args.model or not args.model.strip():
            parser.error("Provide the dedicated loopback URL and one preloaded model ID")
        base_url = LMStudio(args.base_url, {}).base_url
        model = args.model.strip()
    else:
        if args.base_url or args.model:
            parser.error("The stub accepts no endpoint or model settings")
        if args.reasoning_off:
            parser.error("--reasoning-off requires --mode lm-studio")
        base_url, model = "http://127.0.0.1:1234/v1", "offline-first-option-stub"
    run(args.output, args.mode, base_url, model, args.smoke, reasoning_off=args.reasoning_off)


if __name__ == "__main__":
    main()
