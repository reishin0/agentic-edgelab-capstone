"use strict";

const $ = id => document.getElementById(id);
const toolNames = {diagnostics: "ADF / KPSS", engle_granger: "Engle-Granger", stability: "CUSUM / ECM / OU"};
const nice = text => String(text ?? "").replaceAll("_", " ").replaceAll("-", " ");
const num = value => !Number.isFinite(value) ? "-" :
  value === 0 ? "0" : Math.abs(value) < .001 ? value.toExponential(2) : value.toFixed(3);

function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

function safeLink(label, url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:" && !parsed.username && !parsed.password) {
      const link = node("a", label);
      link.href = parsed.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    }
  } catch {}
  return node("strong", label);
}

function empty(id, message) {
  $(id).replaceChildren(node("p", message, "empty"));
}

function table(headers, rows) {
  const wrapper = node("div", undefined, "table-wrap");
  const t = node("table"), header = t.createTHead().insertRow(), body = t.createTBody();
  headers.forEach(label => header.append(node("th", label)));
  rows.forEach(row => {
    const tr = body.insertRow();
    row.forEach(value => {
      const td = tr.insertCell();
      if (value instanceof Node) td.append(value);
      else td.textContent = value ?? "-";
    });
  });
  wrapper.append(t);
  return wrapper;
}

function tag(text) {
  const klass = ["continue", "proceed", "stop", "revise", "unresolved", "retained", "pruned"].includes(text) ? text : "";
  return node("span", nice(text), "tag " + klass);
}

function group(id, items) {
  $(id).replaceChildren(...items.map(([label, value, detail]) => {
    const el = node("div");
    el.append(node("span", label), node("strong", value), node("small", detail));
    return el;
  }));
}

function svg(tag, attrs, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
  if (text !== undefined) el.textContent = text;
  return el;
}

function chart(id, data, lines, label, missing, seriesLabels = {}) {
  if (!data || !lines.every(key => Array.isArray(data[key]) && data[key].length > 1 &&
      data[key].every(Number.isFinite))) return empty(id, missing);
  const flat = lines.flatMap(key => data[key]);
  const spread = id === "spread";
  const min = Math.min(...flat, ...(spread ? [0] : [])), max = Math.max(...flat, ...(spread ? [0] : []));
  const padding = (max - min) * .06 || 1, lo = min - padding, hi = max + padding;
  const x = i => 56 + 456 * i / (data.days.length - 1);
  const y = n => 163 - 144 * (n - lo) / (hi - lo);
  const figure = svg("svg", {viewBox: "0 0 528 190", role: "img", "aria-label": label});
  for (let i = 0; i <= 4; i++) {
    const value = lo + i * (hi - lo) / 4, yp = y(value);
    figure.append(svg("line", {x1: 56, x2: 512, y1: yp, y2: yp, class: "grid"}),
      svg("text", {x: 48, y: yp + 4, "text-anchor": "end", class: "axis"}, value.toFixed(1)));
  }
  if (spread) figure.append(svg("line", {x1: 56, x2: 512, y1: y(0), y2: y(0), class: "grid zero"}));
  for (const [fraction, anchor] of [[0, "start"], [.5, "middle"], [1, "end"]]) {
    const i = Math.round((data.days.length - 1) * fraction);
    figure.append(svg("text", {x: x(i), y: 184, "text-anchor": anchor, class: "axis"}, data.days[i]));
  }
  for (const key of lines) {
    figure.append(svg("polyline", {class: "series-" + key,
      points: data[key].map((value, i) => x(i).toFixed(2) + "," + y(value).toFixed(2)).join(" ")}));
  }
  const cursor = svg("line", {y1: 19, y2: 163, class: "grid zero", visibility: "hidden"});
  figure.append(cursor);
  const detail = node("small", data.source, "empty");
  figure.addEventListener("pointermove", event => {
    const rect = figure.getBoundingClientRect();
    const px = (event.clientX - rect.left) * 528 / rect.width;
    const i = Math.min(data.days.length - 1, Math.max(0, Math.round((px - 56) / 456 * (data.days.length - 1))));
    cursor.setAttribute("x1", x(i));
    cursor.setAttribute("x2", x(i));
    cursor.setAttribute("visibility", "visible");
    detail.textContent = [data.days[i], ...lines.map(key =>
      (seriesLabels[key] ?? key.toUpperCase()) + " " + num(data[key][i]))].join(" | ");
  });
  figure.addEventListener("pointerleave", () => {
    cursor.setAttribute("visibility", "hidden");
    detail.textContent = data.source;
  });
  $(id).replaceChildren(figure, detail);
}

function diagnostics(run) {
  const diagnostics = run?.reviews?.diagnostics?.details?.series;
  const relationship = run?.reviews?.engle_granger?.details;
  const fit = run?.results?.engle_granger?.observation?.result?.a_on_b;
  const stability = run?.reviews?.stability?.details;
  const rows = [];
  for (const [key, item] of Object.entries(diagnostics ?? {})) {
    for (const stage of ["level", "difference"]) {
      rows.push([key.toUpperCase() + " / " + stage, "ADF p " + num(item[stage].adf_p) +
        " | KPSS p " + num(item[stage].kpss_p), "Derived " + item.derived_order + " / tool " + item.reported_order]);
    }
  }
  if (Number.isFinite(relationship?.a_on_b)) rows.push(["Bidirectional cointegration",
    "A on B p " + num(relationship.a_on_b) + " | B on A p " + num(relationship.b_on_a), run.reviews.engle_granger.reason]);
  if (Number.isFinite(fit?.slope) && Number.isFinite(relationship?.a_on_b))
    rows.push(["A on B hedge ratio", num(fit.slope), "In-sample OLS slope"]);
  if (Number.isFinite(stability?.cusum_p)) rows.push(["CUSUM", "p " + num(stability.cusum_p), run.reviews.stability.reason]);
  if (Number.isFinite(stability?.ecm_loading)) {
    rows.push(["ECM loading", num(stability.ecm_loading) + " | p " + num(stability.ecm_loading_p), "Descriptive"]);
    rows.push(["AR(1) / implied OU", "phi " + num(stability.ar1_phi) + " | half-life " +
      (stability.ou_half_life_observations === null ? "not applicable" : num(stability.ou_half_life_observations) + " observations"),
      "Descriptive"]);
  }
  if (rows.length) $("stats").replaceChildren(table(["Check", "Measured evidence", "Interpretation"], rows));
  else empty("stats", "No versioned numerical diagnostics available for this run.");
}

function planningBranchTable(branches) {
  return table(["Depth", "Decision path", "Disposition"], branches.map(branch => {
    const disposition = node("div");
    disposition.append(tag(branch.retained ? "retained" : "pruned"), node("div", branch.reason));
    const path = branch.decisions.map(nice).join(" → ");
    return [branch.depth, path + (branch.score == null ? "" : " · score " + num(branch.score)), disposition];
  }));
}

function trace(run, loading) {
  const plan = run?.plan;
  const verdict = run?.verdict;
  const status = run ? nice(run.status) : loading ? "Loading selected run…" : "No run selected";
  $("decision").replaceChildren(node("strong", verdict ? "Controller: " + nice(verdict.verdict) : status),
    node("span", verdict?.reason ?? plan?.reason ?? ""));
  if (run) {
    const rows = run.timeline.map(event => {
      const [stage, tool] = event.key.split(":");
      const label = toolNames[tool] ?? "";
      const detail = node("div");
      const finding = run.reviews[tool];
      const summary = {
        plan: ["Planner / propose", plan.reason],
        approval: ["Human / authorize", run.approval?.digest?.slice(0, 12) + " | in-sample only"],
        reserved: ["Controller / act", label + " | authorized reservation"],
        result: ["Tool / observe", label + (run.results[tool]?.observation?.error ? " | tool error" : " | recorded")],
        review: ["Critic / review", label + " | " + (finding?.rule_id ?? "legacy finding")],
        verdict: ["Controller / decide", verdict?.evidence_refs?.join(", ") || "No numerical result"],
      }[stage] ?? [stage, ""];
      detail.append(node("strong", summary[1]));
      if (stage === "review" && finding) {
        detail.append(node("div", finding.reason), tag(finding.decision));
      }
      const ref = node("small", event.sha256.slice(0, 12));
      ref.title = event.sha256;
      const refRow = node("div");
      refRow.append(ref);
      detail.append(refRow);
      return [summary[0], detail];
    });
    $("events").replaceChildren(table(["Stage", "Evidence / decision"], rows));
  } else empty("events", "No journal events.");
  const branches = plan?.branches ?? [];
  if (branches.length) $("branches").replaceChildren(planningBranchTable(branches));
  else empty("branches", "No recorded planning branches.");
  if (plan?.method_evidence?.length) {
    $("methods").replaceChildren(...plan.method_evidence.map(note => {
      const row = node("div", undefined, "list-row");
      const titles = {adf: "Augmented Dickey-Fuller", kpss: "KPSS stationarity test", eg: "Engle-Granger cointegration"};
      row.append(safeLink(titles[note.id] ?? note.id, note.url), node("small", note.id + " | " + note.locator));
      return row;
    }));
  } else empty("methods", "No accepted method notes.");
}

const lagPolicies = {
  AIC: ["Akaike Information Criterion", "Lighter penalty for extra lags; can retain more."],
  BIC: ["Bayesian Information Criterion", "Stronger penalty for extra lags in this sample; tends to retain fewer."]
};
let lagRun = null;
let lagChoice = null;

function candidateFor(run, policy) {
  return run?.plan?.candidates?.find(item => item.protocol.lag_policy === policy);
}

function reviewedLagResult(run, tool, policy) {
  const observation = run?.results?.[tool]?.observation;
  const finding = run?.reviews?.[tool];
  const hash = run?.timeline?.find(event => event.key === "result:" + tool)?.sha256;
  if (finding?.evidence_sha256 !== hash || !hash ||
      ["EVIDENCE_BINDING_FAILED", "INVALID_EVIDENCE", "NO_OBSERVATION"].includes(finding?.rule_id) ||
      observation?.config?.lag_policy !== policy) return null;
  return observation.result;
}

function renderLagPolicy() {
  const run = lagRun;
  const approved = run?.approval;
  const candidate = candidateFor(run, lagChoice);
  $("lag-panel").hidden = !candidate;
  for (const policy of Object.keys(lagPolicies)) {
    const button = $("lag-" + policy.toLowerCase());
    button.disabled = !candidateFor(run, policy);
    button.setAttribute("aria-pressed", String(policy === lagChoice));
  }
  if (!candidate) {
    $("lag-details").replaceChildren();
    return;
  }
  const [title, effect] = lagPolicies[lagChoice];
  const proposed = node("div");
  proposed.append(node("strong", title), node("p", effect),
    node("p", "Applies to ADF and Engle-Granger residual lag selection. KPSS uses its own rule."));
  const digest = node("details");
  digest.append(node("summary", "Candidate digest"), node("code", candidate.digest));
  proposed.append(digest);

  const observed = node("div");
  if (!approved) {
    observed.append(node("strong", "Preview only"), node("p", "No lags measured. The run is awaiting approval."));
  } else if (approved.digest !== candidate.digest) {
    observed.append(node("strong", "Not executed"), node("p", approved.protocol.lag_policy +
      " was approved for this run. No alternative result was computed."));
  } else {
    observed.append(node("strong", "Approved policy: " + lagChoice));
    const diagnostic = reviewedLagResult(run, "diagnostics", lagChoice);
    const eg = reviewedLagResult(run, "engle_granger", lagChoice);
    const rows = [];
    for (const series of ["a", "b"]) {
      const values = diagnostic?.[series];
      if (Number.isInteger(values?.level?.adf_lags) && Number.isInteger(values?.difference?.adf_lags)) {
        rows.push(["ADF " + series.toUpperCase() + " (level / difference)",
          values.level.adf_lags + " / " + values.difference.adf_lags]);
      }
    }
    for (const [key, label] of [["a_on_b", "A on B"], ["b_on_a", "B on A"]]) {
      if (Number.isInteger(eg?.[key]?.residual_lag)) rows.push(["Engle-Granger " + label, eg[key].residual_lag]);
    }
    if (rows.length) {
      observed.append(table(["Reviewed check", "Selected lags"], rows));
      if (!eg) observed.append(node("small", "No reviewed Engle-Granger result."));
    } else observed.append(node("p", "No reviewed lag counts are available."));
  }
  $("lag-details").replaceChildren(proposed, observed);
}

for (const policy of Object.keys(lagPolicies)) {
  $("lag-" + policy.toLowerCase()).addEventListener("click", () => {
    lagChoice = policy;
    renderLagPolicy();
  });
}

function showOutcome(run, [a, b]) {
  const panel = $("outcome");
  const plan = run?.plan;
  panel.hidden = !plan;
  if (!plan) return panel.replaceChildren();
  const history = plan.input?.history;
  const sampleLabel = !history || history.classification === "synthetic" ? "Synthetic in-sample" : "Historical in-sample";
  const verdict = run.verdict;
  const policy = run.approval?.protocol?.lag_policy;
  const finding = run.reviews?.engle_granger;
  const hasEvidence = policy && verdict?.evidence_refs?.includes("result:engle_granger") &&
    reviewedLagResult(run, "engle_granger", policy);
  const details = hasEvidence ? finding?.details : null;
  const recordedP = [details?.a_on_b, details?.b_on_a, details?.alpha].every(Number.isFinite);
  const conclusions = {EG_NEITHER_REJECTS: "Cointegration not established",
    EG_BOTH_REJECT: "In-sample evidence of cointegration", EG_DISAGREEMENT: "Directional tests disagree"};
  const heading = node("div", undefined, "panel-head");
  heading.append(node("h2", a + " / " + b), tag(verdict?.verdict ?? run.status));
  panel.className = "panel outcome";
  if (["stop", "unresolved", "revise"].includes(verdict?.verdict)) panel.classList.add("is-" + verdict.verdict);

  const summary = node("div", undefined, "outcome-summary");
  const description = node("div");
  const label = recordedP ? conclusions[finding.rule_id] ?? "Reviewed cointegration result" :
    verdict ? "No reviewed cointegration result" : "Outcome pending";
  const window = plan.input?.window;
  const n = run.approval?.sample_n ?? history?.sessions?.length;
  description.append(node("h3", label), node("p", verdict?.reason ?? plan.reason),
    node("small", [window ? window.start + " to " + window.end : null,
      Number.isInteger(n) ? n + " observations" : null, policy ? policy + " approved" : null,
      sampleLabel].filter(Boolean).join(" | ")));
  summary.append(description);
  if (recordedP) {
    const evidence = node("div");
    evidence.append(node("strong", "Engle-Granger | threshold " + details.alpha),
      table(["Direction", "Recorded p-value"], [[a + " on " + b, String(details.a_on_b)],
        [b + " on " + a, String(details.b_on_a)]]));
    summary.append(evidence);
  }
  const footer = node("div", undefined, "outcome-footer");
  footer.append(node("small", "Stability: " + (run.reviews?.stability ? nice(run.reviews.stability.decision) :
    run.results?.stability ? "No reviewed finding" : "Not run")),
    node("small", "Holdout: Locked and untested"), node("small", "Tradability: Not assessed"));
  panel.replaceChildren(heading, summary, footer);
}

function decisionDetails(label, ...content) {
  const details = node("details");
  details.append(node("summary", label), ...content);
  return details;
}

function showAgentCoordination(run, pair, loading) {
  const planner = $("planner-record"), controller = $("controller-record");
  const critic = $("critic-record"), reporter = $("reporter-record");
  for (const card of [planner, controller, critic, reporter]) card.replaceChildren();
  $("agent-run").textContent = run ? `${pair.join(" / ")} · Run ${run.plan.run_id.slice(0, 8)}` :
    loading ? "Loading selected run…" : "Select a saved run to see its decisions.";
  if (!run) return;

  const {plan, approval, verdict, timeline, results = {}, reviews = {}} = run;
  const candidates = plan.candidates ?? [];
  const policies = candidates.map(candidate => candidate.protocol.lag_policy).filter(Boolean);
  const branches = plan.branches ?? [];
  planner.append(node("strong", candidates.length ? `Eligible protocols: ${policies.join(", ") || candidates.length}` :
    "No eligible protocol"), node("p", plan.reason));
  if (branches.length) {
    const kept = branches.filter(branch => branch.retained).length;
    planner.append(decisionDetails(`Planning branches (${kept} retained of ${branches.length})`, planningBranchTable(branches)));
  }
  if (plan.protocol_review?.outcome) {
    planner.append(node("p", `Protocol review: ${nice(plan.protocol_review.outcome)}` +
      (plan.protocol_review.issues?.length ? ` · ${plan.protocol_review.issues.map(nice).join(", ")}` : "")));
  }

  if (approval) {
    const {protocol, sample_n: observations, digest, authority} = approval;
    controller.append(node("strong", protocol.lag_policy ? `${protocol.lag_policy} protocol approved` : "Protocol approved"));
    if (Number.isInteger(observations)) controller.append(node("p", `${observations} approved observations`));
    if (run.status === "reconciliation required") {
      controller.append(node("p", "Execution blocked. A reserved call has no recorded result."));
    }
    const reservations = new Set(timeline.filter(event => event.key.startsWith("reserved:")).map(event => event.key.slice(9)));
    const steps = node("ul", undefined, "agent-steps");
    for (const tool of protocol.steps ?? Object.keys(toolNames)) {
      const state = results[tool] ? (results[tool].observation?.error ? "Tool error recorded" : "Result recorded") :
        reservations.has(tool) ? "Reserved, result pending" : "No call recorded";
      const step = node("li");
      step.append(node("strong", toolNames[tool] ?? nice(tool)), node("p", state));
      steps.append(step);
    }
    controller.append(steps);
    if (digest) controller.append(decisionDetails("Exact approval", node("code", digest), node("p", authority)));
  } else {
    controller.append(node("strong", "No approval recorded"), node("p", candidates.length ?
      "Waiting for the researcher to choose a protocol." : "Execution did not start."));
  }

  const findings = Object.entries(reviews);
  const reviewList = node("ol", undefined, "agent-steps");
  for (const [tool, finding] of findings) {
    const entry = node("li");
    entry.append(node("strong", `${toolNames[tool] ?? nice(tool)} · ${nice(finding.decision)}`), node("p", finding.reason));
    if (finding.rule_id) entry.append(node("small", `Rule ${finding.rule_id}`));
    reviewList.append(entry);
  }
  if (findings.length) critic.append(reviewList);
  const waiting = Object.keys(results).filter(tool => !reviews[tool]).map(tool => toolNames[tool] ?? nice(tool));
  if (waiting.length) critic.append(node("p", `Awaiting review: ${waiting.join(", ")}.`));
  else if (!findings.length) critic.append(node("strong", "No numerical review recorded"));

  if (verdict) {
    reporter.append(node("strong", `Recorded verdict · ${nice(verdict.verdict)}`), node("p", verdict.reason));
    const references = verdict.evidence_refs ?? [];
    if (references.length) {
      const list = node("ul");
      for (const ref of references) list.append(node("li", toolNames[ref.replace(/^result:/, "")] ?? ref));
      reporter.append(node("p", "Cited results"), list);
    } else reporter.append(node("p", "No numerical results cited."));
  } else reporter.append(node("strong", "Waiting for a recorded verdict"));

  for (const [role, card] of [["planner", planner], ["critic", critic]]) {
    const choices = plan.model_decisions?.filter(choice => choice.role === role) ?? [];
    if (!choices.length) continue;
    const list = node("ul");
    list.append(...choices.map(({stage, choice}) => node("li", `${nice(stage)} · ${nice(choice)}`)));
    const model = plan.role_models?.[role];
    const label = model ? `Recorded model choices · ${model}` : "Recorded model choices";
    card.append(decisionDetails(label, list));
  }
}

function show(run, cfg, demo, loading = false) {
  if (!run || lagRun?.plan?.run_id !== run.plan.run_id || !candidateFor(run, lagChoice)) {
    lagChoice = run?.approval?.protocol?.lag_policy ?? run?.plan?.candidates?.[0]?.protocol?.lag_policy ?? null;
  }
  lagRun = run;
  renderLagPolicy();
  const plan = run?.plan;
  const symbols = run?.symbols ?? plan?.input?.history?.source
    ?.match(/; A=([A-Z]{3}\/[A-Z]{3}|[A-Z0-9.-]+), B=([A-Z]{3}\/[A-Z]{3}|[A-Z0-9.-]+);/)?.slice(1);
  const pair = symbols ?? ["A", "B"];
  showOutcome(run, pair);
  showAgentCoordination(run, pair, loading);
  const modelDecisions = plan?.model_decisions?.length ?? 0;
  const resultCount = Object.keys(run?.results ?? {}).length;
  group("metrics", [
    ["Verdict", run?.verdict ? nice(run.verdict.verdict) : run ? nice(run.status) : loading ? "Loading" : "No run",
      demo ? "Synthetic demonstration" : "Local journal"],
    ["Planning branches", plan?.branches?.length ?? "-", (plan?.candidates?.length ?? 0) + " approvable candidates at planning"],
    ["Numerical tools", resultCount + " / 3", Object.keys(run?.reviews ?? {}).length + " milestone reviews"],
    ["Chronological holdout", "Locked", "Never queried by this dashboard"]
  ]);
  const observedPine = plan?.retrieval === "pinecone" ?
    plan.retrieval_observation === "provider_error" ? "Retrieval failed in selected run" :
    plan.retrieval_observation === "completed" ? "Retrieval recorded in selected run" : "Selected by legacy run; outcome not instrumented" :
    plan ? "Local notes used in selected run" : "No selected-run evidence";
  const observedLM = modelDecisions ? modelDecisions + " structured decision(s) recorded" :
    plan?.model_attempts ? "Attempted; no structured decision" : "No model decision in selected run";
  const modelName = plan?.role_models?.planner;
  const modelDetail = plan?.adviser_provider ?
    (plan.adviser_provider === "openrouter" ? "OpenRouter" : "LM Studio") + " · " + observedLM : observedLM;
  group("providers", [
    ["Planner model", modelName || "No model recorded", cfg.default_planner ?
      modelDetail + " · Default for new plans: " + cfg.default_planner : modelDetail],
    ["Pinecone", cfg.pinecone, observedPine],
    ["MCP interface", "Host-bound", "Tool catalog and planning trace; no dashboard calls"],
    ["News credentials", cfg.news, "Dashboard reads only the local cache"]
  ]);
  const window = plan?.input?.window;
  $("window").textContent = window ? window.start + " to " + window.end : loading ? "" : "No approved sample";
  const chartMissing = plan?.input?.history ?
    "Historical chart requires a matching manifest and recorded observations." : "Awaiting approved synthetic observations.";
  const seriesLabels = symbols ? {a: "A = " + pair[0], b: "B = " + pair[1]} : {a: "A", b: "B"};
  $("series-a-label").textContent = seriesLabels.a;
  $("series-b-label").textContent = seriesLabels.b;
  chart("series", run?.chart, ["a", "b"], `Approved in-sample series ${seriesLabels.a} and ${seriesLabels.b}`,
    chartMissing, seriesLabels);
  chart("spread", run?.chart, ["spread"], "In-sample OLS residual A on B",
    run?.chart ? "No fitted relationship recorded." : chartMissing);
  diagnostics(run);
  trace(run, loading);
}

async function api(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error("Could not read local evidence (HTTP " + response.status + ").");
  return response.json();
}

const viewContent = $("view-content");
const viewLinks = Array.from($("view-nav").querySelectorAll("a"));
const viewPanels = viewContent.querySelectorAll("[data-view]");
const etfView = $("etf-entry-costs");
const etfTip = $("etf-entry-dialog");
let etfSnapshotAvailable = false;
let activeView;
let activeHash;

function fragmentId() {
  const fragment = location.hash.slice(1);
  try { return decodeURIComponent(fragment); }
  catch { return fragment; }
}

function showView(moveFocus = false) {
  const hash = location.hash;
  const target = $(fragmentId());
  const targetView = target?.closest("[data-view]");
  const current = viewLinks.find(link => link.hash === `#${targetView?.id}`) ?? viewLinks[0];
  const view = $(current.hash.slice(1));
  $("etf-entry-nav").hidden = !etfSnapshotAvailable && view !== etfView;
  if (view === activeView && hash === activeHash && !moveFocus) return;

  const focusWasInView = activeView?.contains(document.activeElement);
  if (activeView === etfView && view !== etfView && etfTip.open) etfTip.close();
  for (const panel of viewPanels) panel.hidden = panel !== view;
  for (const link of viewLinks) {
    if (link === current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  activeView = view;
  activeHash = hash;
  viewContent.scrollTop = 0;
  window.scrollTo(0, 0);
  if (target !== view && view.contains(target) && !target.hidden) {
    target.scrollIntoView({block: "start"});
  }
  if (moveFocus || focusWasInView) view.focus({preventScroll: true});
}

for (const link of [$("dashboard-home"), ...viewLinks]) {
  link.addEventListener("click", event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (location.hash !== link.hash) history.pushState(null, "", link.hash);
    showView(true);
  });
}

history.scrollRestoration = "manual";
window.addEventListener("popstate", () => showView());
window.addEventListener("hashchange", () => showView());
showView();

let runContext = {configuration: {}, demo: false};
const runListRead = {request: 0, loading: false, error: ""};
const runEvidenceRead = {request: 0, loading: false, error: ""};
let shownRunId = null;
let manualRefreshPending = false;
let refreshNotice = "";

function showRunReadStatus() {
  const loading = $("run-loading");
  const errorNotice = $("load-error");
  const error = runEvidenceRead.error || runListRead.error;
  errorNotice.textContent = error;
  errorNotice.hidden = !error;
  const busy = runListRead.loading || runEvidenceRead.loading;
  if (manualRefreshPending && !busy) {
    manualRefreshPending = false;
    if (!error) {
      const time = new Date().toLocaleTimeString();
      refreshNotice = $("run-select").disabled ? `Checked at ${time}. No saved runs found.` :
        `Saved results refreshed at ${time}.`;
    }
  }
  if (busy) loading.textContent = manualRefreshPending ? "Refreshing saved results…" :
    runEvidenceRead.loading ? "Loading run evidence…" : "Loading saved runs…";
  else loading.textContent = error ? "" : refreshNotice;
  loading.hidden = !loading.textContent;
}

async function refreshSelectedRun() {
  const request = ++runEvidenceRead.request;
  const select = $("run-select");
  const id = select.disabled ? null : select.value;
  runEvidenceRead.loading = Boolean(id);
  runEvidenceRead.error = "";
  showRunReadStatus();
  let rendering = false;
  try {
    if (id && shownRunId !== id) {
      shownRunId = null;
      show(null, runContext.configuration, runContext.demo, true);
    }
    const run = id ? await api("/api/run?id=" + encodeURIComponent(id)) : null;
    if (request !== runEvidenceRead.request) return;
    if (id && run?.plan?.run_id !== id) throw new Error("Local evidence did not match the selected run.");
    rendering = true;
    show(run, runContext.configuration, runContext.demo);
    shownRunId = id;
  } catch (error) {
    if (request !== runEvidenceRead.request) return;
    const haveEvidence = !rendering && id && shownRunId === id;
    if (!haveEvidence) {
      shownRunId = null;
      show(null, runContext.configuration, runContext.demo);
    }
    runEvidenceRead.error = "Selected run unavailable. " + error.message +
      (haveEvidence ? " Showing the last loaded evidence." : "");
  } finally {
    if (request === runEvidenceRead.request) {
      runEvidenceRead.loading = false;
      showRunReadStatus();
    }
  }
}

async function refreshRuns() {
  const request = ++runListRead.request;
  const evidenceRequest = runEvidenceRead.request;
  const select = $("run-select");
  runListRead.loading = true;
  runListRead.error = "";
  showRunReadStatus();
  try {
    const data = await api("/api/runs");
    if (request !== runListRead.request) return;
    const selected = select.disabled ? null : select.value;
    const id = data.runs.find(run => run.id === selected)?.id ?? data.runs[0]?.id ?? null;
    const options = data.runs.map(run => {
      const option = node("option", (run.symbols?.join(" / ") ?? nice(run.label)) + " | " + run.id.slice(0, 8));
      option.value = run.id;
      return option;
    });
    runContext = {configuration: data.configuration, demo: data.demo};
    select.replaceChildren(...(options.length ? options : [node("option", "No recorded runs")]));
    if (id) select.value = id;
    select.disabled = !id;
    if (id !== selected || evidenceRequest === runEvidenceRead.request) refreshSelectedRun();
    document.dispatchEvent(new CustomEvent("edgelab:runs-refreshed", {
      detail: {runIds: data.runs.map(run => run.id)}
    }));
  } catch (error) {
    if (request !== runListRead.request) return;
    runListRead.error = "Run list unavailable. " + error.message;
    if (select.disabled) select.replaceChildren(node("option", "Run list unavailable"));
    else runListRead.error += " Showing the last loaded run list.";
  } finally {
    if (request === runListRead.request) {
      runListRead.loading = false;
      showRunReadStatus();
    }
  }
}

let newsRequest = 0;
let newsLoading = false;
async function refreshNews() {
  const request = ++newsRequest;
  newsLoading = true;
  const host = $("headlines");
  try {
    const data = await api("/api/news");
    if (request !== newsRequest) return;
    const rows = [];
    if (data.origin === "synthetic") rows.push(node("div", "Illustrative headlines | No provider request | " +
      data.symbols.join(", "), "list-row"));
    else if (data.ingested_at) rows.push(node("div", "Alpaca | Ingested " + data.ingested_at + " | " +
      data.symbols.join(", ") + " | Provider delivery may be delayed", "list-row"));
    for (const item of data.articles) {
      const row = node("div", undefined, "list-row");
      row.append(safeLink(item.headline, item.url),
        node("small", [item.source, item.created_at, item.symbols.join(", ")].filter(Boolean).join(" | ")));
      rows.push(row);
    }
    if (!data.articles.length) rows.push(node("p", data.ingested_at ? "No matching headlines in the snapshot." :
      "No cached headlines available.", "empty"));
    $("news-state").textContent = data.status;
    if (rows.length === host.children.length && rows.every((row, index) => row.isEqualNode(host.children[index]))) {
      return;
    }
    const focused = document.activeElement;
    const previousLink = host.contains(focused) ? focused.closest("a") : null;
    const replacement = previousLink && rows.map(row => row.querySelector("a")).find(link =>
      link?.href === previousLink.href && link.textContent === previousLink.textContent);
    host.replaceChildren(...rows);
    replacement?.focus({preventScroll: true});
  } catch {
    if (request !== newsRequest) return;
    $("news-state").textContent = "Unavailable";
    empty("headlines", "Local news cache could not be read.");
  } finally {
    if (request === newsRequest) newsLoading = false;
  }
}

const waitlistPageSize = 25;
let waitlistRequest = 0;
let waitlist = null;
let waitlistPage = 0;
let waitlistLoadFailed = false;

function renderWaitlist() {
  if (!waitlist) {
    $("waitlist-page").hidden = true;
    empty("waitlist-rows", waitlistLoadFailed ? "The stock waitlist could not be loaded. Use Refresh to try again." :
      "No pinned stock universe in this view.");
    return;
  }
  const query = $("waitlist-filter").value.trim().toUpperCase();
  const matched = waitlist.entries.filter(row => row.symbol.includes(query) || row.sector.toUpperCase().includes(query));
  waitlistPage = Math.max(0, Math.min(waitlistPage, Math.ceil(matched.length / waitlistPageSize) - 1));
  const offset = waitlistPage * waitlistPageSize;
  const visible = matched.slice(offset, offset + waitlistPageSize);
  $("waitlist-page").hidden = false;
  $("waitlist-prev").disabled = waitlistPage === 0;
  $("waitlist-next").disabled = offset + waitlistPageSize >= matched.length;
  $("waitlist-count").textContent = visible.length ? (offset + 1) + "-" + (offset + visible.length) +
    " of " + matched.length.toLocaleString() : "0 matches";
  if (visible.length) $("waitlist-rows").replaceChildren(table(["Rank", "Ticker", "Sector", "Exchange"],
    visible.map(row => [row.rank, row.symbol, row.sector, row.exchange])));
  else empty("waitlist-rows", "No matching tickers or sectors.");
}

async function loadWaitlist() {
  const request = ++waitlistRequest;
  try {
    const snapshot = await api("/api/waitlist");
    if (request !== waitlistRequest) return;
    if (snapshot !== null && !Array.isArray(snapshot.entries)) throw new Error("Invalid stock waitlist response");
    waitlist = snapshot;
    waitlistLoadFailed = false;
  } catch {
    if (request !== waitlistRequest) return;
    waitlistLoadFailed = true;
  }
  $("waitlist-filter").disabled = !waitlist;
  const source = $("waitlist-source"), state = $("waitlist-state");
  if (waitlist) {
    const n = waitlist.entries.length;
    state.textContent = waitlistLoadFailed ? "Stock refresh failed | Showing last loaded snapshot" :
      n.toLocaleString() + " stocks | " + (n * (n - 1) / 2).toLocaleString() + " possible pairs | Not tested";
    source.replaceChildren(safeLink(waitlist.fund, waitlist.source), node("span", " | " + waitlist.as_of));
  } else {
    state.textContent = waitlistLoadFailed ? "Stock waitlist unavailable" : "No stock waitlist configured";
    source.textContent = "Local research universe";
  }
  renderWaitlist();
}

let crossAssetsRequest = 0;
let crossAssetsLoaded = false;

function crossAssetsNotice(message) {
  const notice = node("p", message, "cross-assets-notice empty");
  notice.setAttribute("role", "status");
  return notice;
}

function renderCrossAssets(host, snapshot) {
  if (!snapshot) {
    host.replaceChildren(crossAssetsNotice("No FX or crypto snapshot configured."));
    return;
  }
  const money = new Intl.NumberFormat("en-US", {notation: "compact", style: "currency", currency: "USD",
    maximumFractionDigits: 2});
  host.replaceChildren(...[["fx", "FX spot", "BIS final turnover tables"],
    ["crypto", "Crypto spot", "Kraken public market snapshot"]].map(([key, title, source]) => {
    const group = snapshot[key];
    const panel = node("article", undefined, "panel");
    const heading = node("div", undefined, "panel-head");
    heading.append(node("h3", title), tag("Not tested"));
    const date = key === "crypto" ? new Date(group.as_of).toISOString().slice(0, 19).replace("T", " ") + " UTC" : group.as_of;
    const provenance = node("div", undefined, "list-row");
    provenance.append(safeLink(source, group.source), node("small", date + " | " + group.metric));
    const headers = ["Rank", "Instrument", "Turnover"];
    if (key === "crypto") headers.push("Margin sell");
    panel.append(heading, provenance, table(headers, group.entries.map(row => {
      const values = [row.rank, row.symbol, money.format(row.turnover_usd)];
      if (key === "crypto") values.push(row.sell_margin_listed ? "Venue lists" : "Not listed");
      return values;
    })), node("p", key === "crypto" ? "Account, region, and borrow availability not verified." :
      "Survey ranking, not live quotes or broker eligibility.", "empty"));
    return panel;
  }));
}

async function loadCrossAssets() {
  const request = ++crossAssetsRequest;
  const host = $("cross-assets");
  try {
    const snapshot = await api("/api/cross-assets");
    if (request !== crossAssetsRequest) return;
    renderCrossAssets(host, snapshot);
    crossAssetsLoaded = snapshot !== null;
  } catch {
    if (request !== crossAssetsRequest) return;
    const notice = crossAssetsNotice(crossAssetsLoaded ? "FX and crypto refresh failed. Showing the last loaded snapshot." :
      "The FX and crypto snapshot could not be loaded. Use Refresh to try again.");
    host.querySelector(".cross-assets-notice")?.remove();
    host.prepend(notice);
  }
  if (!host.hidden) return;
  host.hidden = false;
  if (fragmentId() === "cross-assets" && activeView?.contains(host)) {
    host.scrollIntoView({block: "start"});
  }
}

let leaderboardRequest = 0;
let leaderboardLoaded = false;
let leaderboardLoading = false;
async function loadLeaderboard() {
  const request = ++leaderboardRequest;
  leaderboardLoading = true;
  const host = $("leaderboard-groups");
  const state = $("leaderboard-state");
  try {
    const snapshot = await api("/api/leaderboard");
    if (request !== leaderboardRequest) return;
    if (snapshot !== null && !Array.isArray(snapshot.groups)) throw new Error("Invalid pair leaderboard response");
    if (!snapshot?.groups?.length) {
      leaderboardLoaded = false;
      state.textContent = "";
      return empty("leaderboard-groups", "No pair screening snapshot loaded");
    }
    const count = new Intl.NumberFormat("en-US");
    const labels = {
      "stock-stock": "Stock / stock (raw pilot)",
      "stock-stock-adjusted": "Stock / stock (adjusted)",
      "fx-crypto": "FX / crypto",
    };
    const stability = {"break_detected": "Break detected", "no_break_detected": "No break detected"};
    host.replaceChildren(...snapshot.groups.map(group => {
      const panel = node("article", undefined, "panel");
      const heading = node("div", undefined, "panel-head");
      heading.append(node("h3", labels[group.kind]), node("span", group.label));
      const coverage = node("p", [
        count.format(group.possible) + " possible",
        count.format(group.complete_history) + " with complete history",
        count.format(group.screened ?? group.rows.length - group.data_blocked) + " screened",
        count.format(group.data_blocked) + " data blocked",
        count.format(group.unlisted) + " not screened",
        ...(group.screened == null ? [] : [count.format(group.rows.length) + " ranked rows shown"]),
      ].join(" | "), "leaderboard-coverage");
      const rows = group.rows.map(row => [
        row.rank, row.symbols.join(" vs "), num(row.p), num(row.holm_p),
        stability[row.stability] ?? "Not run", tag(row.status), row.reason,
      ]);
      const headers = ["Rank", "Pair", "Pair p", "Holm p", "Stability", "Verdict", "Reason"];
      const grid = table(headers, rows);
      grid.querySelectorAll("tbody tr").forEach(tr => {
        Array.from(tr.cells).forEach((cell, i) => { cell.dataset.label = headers[i]; });
      });
      panel.append(heading, coverage, grid);
      return panel;
    }));
    leaderboardLoaded = true;
    state.textContent = "No validated trade";
  } catch {
    if (request !== leaderboardRequest) return;
    if (leaderboardLoaded) {
      state.textContent = "No validated trade | Refresh failed. Showing last loaded snapshot.";
      return;
    }
    state.textContent = "";
    empty("leaderboard-groups", "Pair screening evidence is unavailable");
  } finally {
    if (request === leaderboardRequest) leaderboardLoading = false;
  }
}

function stabilityLabel(key) {
  const controls = {
    independent_walks: "Independent random walks",
    correlated_walks: "Correlated random walks",
    stable_strong: "Strong spread, no shift",
    stable_weak: "Weak spread, no shift",
  };
  if (Object.hasOwn(controls, key)) return controls[key];
  const [strength, size, date] = key.split("_");
  return `${strength === "strong" ? "Strong" : "Weak"} spread, +${size} at ${date}%`;
}

function studyCount(count, total) {
  return total ? `${count}/${total} (${(100 * count / total).toFixed(1)}%)` : "-";
}

function stabilityTable(label, headers, rows) {
  const grid = table(headers, rows);
  grid.tabIndex = 0;
  grid.setAttribute("role", "region");
  grid.setAttribute("aria-label", label);
  return grid;
}

function showBreakDateAccuracy(accuracy) {
  const panel = node("article", undefined, "panel stability-table");
  const heading = node("div", undefined, "panel-head");
  heading.append(node("h3", "Break-date accuracy"));
  panel.append(heading);
  if (accuracy?.status !== "available") {
    panel.append(node("p", accuracy?.status === "missing" ? "Cached trial ledger not found." :
      "Cached trial ledger could not be verified.", "empty"));
    return panel;
  }
  heading.append(node("span", `Weak spreads | Shift at ${accuracy.planted_split_percent}% of sample`));
  panel.append(node("p", `Break flagged within ${accuracy.tolerance_observations} observations of the planted date, out of all trials that reached the stability check. Missed detections count against accuracy. CUSUM is not scored because it did not report a break date.`, "leaderboard-coverage"),
    stabilityTable("Break-date accuracy by shift", ["Planted shift", "Revised (88 dates)", "All dates (433)"],
      accuracy.scenarios.map(row => [row.scenario === "weak_0.6_88" ? "Small (+0.6)" : "Large (+2.4)",
        studyCount(row.within_tolerance.revised, row.eligible),
        studyCount(row.within_tolerance.all_dates, row.eligible)])));
  return panel;
}

let stabilityRequest = 0;
let stabilitySnapshotLabel = "";
async function loadStabilityStudy() {
  const request = ++stabilityRequest;
  const host = $("stability-results"), state = $("stability-state");
  try {
    const study = await api("/api/synthetic-stability");
    if (request !== stabilityRequest) return;
    if (!study) {
      stabilitySnapshotLabel = "";
      state.textContent = "";
      return empty("stability-results", "No synthetic stability snapshot loaded");
    }
    const summary = node("div", undefined, "decision");
    const targetMet = study.late_gain_pp >= study.late_target_pp;
    summary.append(node("strong", targetMet ? "Late-break benchmark met." : "Late-break benchmark not met."),
      document.createTextNode(`All-date scanning reduced mean late-break advancement by ${study.late_gain_pp.toFixed(2)} percentage points (target: ${study.late_target_pp}).`));
    const note = node("p", `${study.samples.toLocaleString("en-US")} fresh synthetic samples; ${study.formation_observations} observations per sample. Counts show passage through the full screen. Lower is better for random walks and shifted spreads; higher is better for stable spreads. Production rules unchanged.`, "leaderboard-coverage");
    const arms = ["cusum", "revised", "all_dates"];
    const grid = stabilityTable("Synthetic scenario comparison", ["Scenario", "CUSUM", "Revised (88 dates)", "All dates (433)"], study.scenarios.map(row => [
      stabilityLabel(row.scenario), ...arms.map(arm => studyCount(row.advanced[arm], row.n)),
    ]));
    const panel = node("div", undefined, "panel stability-table wide");
    panel.append(note, grid);
    host.replaceChildren(summary, showBreakDateAccuracy(study.break_date_accuracy), panel);
    stabilitySnapshotLabel = `${study.date} | Market holdout ${study.holdout}`;
    state.textContent = stabilitySnapshotLabel;
  } catch {
    if (request !== stabilityRequest) return;
    if (stabilitySnapshotLabel) {
      state.textContent = stabilitySnapshotLabel + " | Refresh failed. Showing last loaded snapshot.";
      return;
    }
    state.textContent = "";
    empty("stability-results", "Synthetic stability evidence is unavailable");
  }
}

function renderEtfEntryCosts(study) {
  const money = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD"});
  const budget = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD", maximumFractionDigits: 0});
  const count = new Intl.NumberFormat("en-US");
  const scenarios = ["base", "stress"];
  const ceiling = Math.ceil(Math.max(1, ...study.rows.flatMap(row =>
    scenarios.map(scenario => row[`${scenario}_mean_cost_bps`]))));
  const chartRows = study.rows.map(row => {
    const line = node("div", undefined, "etf-chart-row");
    const bars = node("div");
    for (const scenario of scenarios) {
      const meter = node("meter", undefined, `etf-meter-${scenario}`);
      meter.max = ceiling;
      meter.value = row[`${scenario}_mean_cost_bps`];
      bars.append(meter);
    }
    line.append(node("strong", row.symbol), bars);
    return line;
  });

  const grid = table(["ETF", "Median spread", "Mean base", "Mean stress"], study.rows.map(row => {
    const symbol = node("strong", row.symbol);
    if (row.symbol === study.reference) symbol.append(node("small", "Reference"));
    return [symbol, `${row.median_spread_bps.toFixed(3)} bp`, ...scenarios.map(scenario => {
      const cost = node("span", `${row[`${scenario}_mean_cost_bps`].toFixed(3)} bp`);
      cost.append(node("small", money.format(row[`${scenario}_mean_cost_usd`])));
      return cost;
    })];
  }));
  grid.firstElementChild.setAttribute("aria-label", "ETF spread and modeled purchase cost");
  $("etf-entry-scope").textContent = `${study.start_date} to ${study.end_date} | ${study.matched_sessions}/${study.scheduled_sessions} mornings matched | ${budget.format(study.budget_usd)} per purchase`;
  $("etf-entry-clocks").textContent = `${count.format(study.clocks_fit)} of ${count.format(study.clocks_checked)} checked seconds fit all purchases`;
  $("etf-entry-bars").replaceChildren(...chartRows);
  $("etf-entry-table").replaceChildren(grid);
}

let etfEntryRequest = 0;
function showEtfEntryStatus(message) {
  const status = $("etf-entry-status");
  status.textContent = message;
  status.hidden = !message;
}

async function loadEtfEntryCosts() {
  const request = ++etfEntryRequest;
  showEtfEntryStatus(etfSnapshotAvailable ? "Refreshing ETF costs…" : "Loading ETF costs…");
  try {
    const study = await api("/api/etf-entry-costs");
    if (request !== etfEntryRequest) return;
    if (study !== null) {
      if (!study.rows?.length) throw new Error("ETF cost snapshot is incomplete.");
      renderEtfEntryCosts(study);
      etfSnapshotAvailable = true;
      showEtfEntryStatus("");
    } else {
      etfSnapshotAvailable = false;
      showEtfEntryStatus("No saved ETF cost snapshot is available.");
    }
  } catch {
    if (request !== etfEntryRequest) return;
    showEtfEntryStatus(etfSnapshotAvailable ? "Could not refresh ETF costs. Showing the last loaded snapshot." :
      "Could not load ETF costs. Use Refresh to try again.");
  }
  if (!etfSnapshotAvailable && etfTip.open) etfTip.close();
  $("etf-entry-panel").hidden = !etfSnapshotAvailable;
  $("etf-entry-tip").hidden = !etfSnapshotAvailable;
  showView();
}

$("etf-entry-tip").addEventListener("click", () => etfTip.showModal());
$("etf-tip-close").addEventListener("click", () => etfTip.close());
etfTip.addEventListener("close", () => {
  if (activeView === etfView && !etfTip.open) {
    (etfSnapshotAvailable ? $("etf-entry-tip") : etfView).focus({preventScroll: true});
  }
});

$("waitlist-filter").addEventListener("input", () => { waitlistPage = 0; renderWaitlist(); });
$("waitlist-prev").addEventListener("click", () => { waitlistPage--; renderWaitlist(); });
$("waitlist-next").addEventListener("click", () => { waitlistPage++; renderWaitlist(); });
$("run-select").addEventListener("change", refreshSelectedRun);
function refreshDashboard(manual = false) {
  if (manual) {
    manualRefreshPending = true;
    refreshNotice = "";
  }
  refreshRuns();
  refreshNews();
  loadWaitlist();
  loadCrossAssets();
  loadLeaderboard();
  loadStabilityStudy();
  loadEtfEntryCosts();
}
$("refresh").addEventListener("click", () => refreshDashboard(true));
refreshDashboard();
setInterval(() => {
  if (document.hidden) return;
  if (!newsLoading) refreshNews();
  if (!leaderboardLoading) loadLeaderboard();
}, 20000);
