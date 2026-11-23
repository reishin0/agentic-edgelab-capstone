"use strict";

(() => {
  const byId = id => document.getElementById(id);
  const panel = byId("pair-console");
  const assetA = byId("pair-a"), assetB = byId("pair-b");
  const history = byId("pair-history"), start = byId("pair-start"), end = byId("pair-end");
  const prepare = byId("pair-prepare"), review = byId("pair-review"), candidate = byId("pair-candidate");
  const approve = byId("pair-approve"), discard = byId("pair-discard"), output = byId("pair-output");
  const retry = byId("pair-retry"), protocolDetails = byId("pair-protocol-details"), protocol = byId("pair-protocol");
  const ready = byId("pair-ready");
  const base = "/api/pair-console";
  const assets = new Set();
  const stages = {
    preparing: "Preparing plan",
    awaiting_approval: "Ready for approval",
    executing: "Running test",
    complete: "Report ready",
    failed: "Stopped before report",
    discarded: "Plan set aside",
  };
  let job = null, submitting = false, checking = false, needsRetry = false, timer, readVersion = 0;
  let uncertainWrite = null;
  let historyVersion = 0, pairKey = "", historyMessage = "Choose two distinct assets from the waitlists.";
  let acknowledged = null, pendingAcknowledgment = null;
  try { acknowledged = sessionStorage.getItem("edgelab-pair-report-seen"); } catch {}

  async function request(route = "", body) {
    const init = {cache: "no-store", signal: AbortSignal.timeout(8000)};
    if (body) Object.assign(init, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const response = await fetch(base + route, init);
    if (response.ok) return response.json();
    const data = await response.json().catch(() => null);
    throw Object.assign(new Error(data?.error ?? "The pair console could not complete the request."), {status: response.status});
  }

  const selectedPair = () => [assetA.value.trim().toUpperCase(), assetB.value.trim().toUpperCase()];
  const running = () => job?.state === "preparing" || job?.state === "executing";
  const active = () => running() || job?.state === "awaiting_approval";

  function beginRead() {
    clearTimeout(timer);
    return ++readVersion;
  }

  function retryUncertainWrite(message) {
    if (!uncertainWrite?.retries) return false;
    uncertainWrite.retries--;
    clearTimeout(timer);
    timer = setTimeout(() => synchronize(message), 1000);
    return true;
  }

  function showError(message = "") {
    const error = byId("pair-error");
    error.textContent = message;
    error.hidden = !message;
  }

  function renderControls() {
    const captured = history.value === "capture";
    const locked = submitting || checking || needsRetry || active();
    assetA.disabled = assetB.disabled = locked || assets.size < 2;
    history.disabled = locked;
    start.disabled = end.disabled = locked || !captured;
    byId("pair-capture-dates").hidden = !captured;
    byId("pair-input-note").textContent = captured ?
      "Fetch raw daily closes for these two U.S. equities from Alpaca IEX. The reviewed in-sample window runs from January 2 to October 31, 2025." : historyMessage;
    prepare.textContent = captured ? "Fetch history and prepare plan" : "Prepare plan";
    prepare.disabled = locked || !history.value || (captured && (!start.value || !end.value || start.value > end.value));
    const chosen = job?.state === "awaiting_approval" ? job.candidates.find(item => item.digest === candidate.value) : null;
    const canReview = job?.state === "awaiting_approval" && !submitting && !checking && !needsRetry;
    candidate.disabled = discard.disabled = !canReview;
    approve.disabled = !canReview || !chosen?.protocol || history.value !== job?.history?.digest;
    byId("pair-run-id").textContent = job?.run_id ?? "";
    byId("pair-digest").textContent = chosen?.digest ?? "";
    byId("pair-approval-summary").hidden = !chosen;
    protocolDetails.hidden = !chosen?.protocol;
    const json = chosen?.protocol ? JSON.stringify(chosen.protocol, null, 2) : "";
    if (protocol.textContent !== json) {
      protocol.textContent = json;
      protocol.scrollTop = 0;
    }
    retry.hidden = !needsRetry;
    retry.disabled = checking || submitting;
  }

  function historyOption(item) {
    return new Option(`${item.symbols.join(" / ")} · ${item.start} to ${item.end} · ${item.classification}`, item.digest);
  }

  async function loadHistory(saved = null, force = false) {
    const [a, b] = selectedPair();
    const key = a + "|" + b;
    if (!force && key === pairKey) return;
    pairKey = key;
    const version = ++historyVersion;
    const field = byId("pair-history-field");
    field.hidden = !saved;
    history.replaceChildren(...(saved ? [historyOption(saved)] : []));
    const valid = assets.has(a) && assets.has(b) && a !== b;
    historyMessage = saved ? "Uses the selected frozen sample. Review the plan before running the test." :
      valid ? "Checking frozen history…" : "Choose two distinct assets from the waitlists.";
    renderControls();
    if (!valid) return;
    try {
      const data = await request("/history?" + new URLSearchParams({a, b}));
      if (version !== historyVersion) return;
      const histories = data.histories;
      if (saved && !histories.some(item => item.digest === saved.digest)) histories.unshift(saved);
      const options = histories.map(historyOption);
      if (data.capture_allowed) options.push(new Option("Fetch U.S. equity history from Alpaca IEX", "capture"));
      if (options.length) {
        history.replaceChildren(new Option("Choose a history source", ""), ...options);
        history.value = saved?.digest ?? histories[0]?.digest ?? "";
        field.hidden = false;
      }
      historyMessage = histories.length ? "Uses the selected frozen sample. Review the plan before running the test." :
        "No matching frozen history is configured for this pair.";
    } catch (error) {
      if (version !== historyVersion) return;
      historyMessage = error.message;
      pairKey = "";
    }
    renderControls();
  }

  function showJob(next) {
    const changed = job?.id !== next.id;
    const newHistory = job?.history?.digest !== next.history?.digest;
    const wasActive = active();
    const wasReviewing = !changed && job.state === "awaiting_approval";
    job = next;
    if (changed || newHistory || (wasActive && !active())) {
      [assetA.value, assetB.value] = job.symbols;
      if (active()) {
        ++historyVersion;
        pairKey = "";
        const saved = job.history;
        byId("pair-history-field").hidden = !saved;
        history.replaceChildren(...(saved ? [historyOption(saved)] : []));
        historyMessage = saved ? "Uses the frozen sample bound to this run." : "Resolving the history for this pair.";
      } else {
        loadHistory(job.history, true);
      }
    }
    byId("pair-stage").textContent = `${job.symbols.join(" / ")} · ${stages[job.state]}`;
    const log = job.console.join("\n\n");
    if (output.textContent !== log) {
      const follow = output.scrollHeight - output.scrollTop - output.clientHeight < 20;
      output.textContent = log;
      output.hidden = !log;
      if (follow) output.scrollTop = output.scrollHeight;
    }
    review.hidden = job.state !== "awaiting_approval";
    if (!review.hidden && !wasReviewing) {
      candidate.replaceChildren(new Option("Choose a lag policy", ""), ...job.candidates.map(item =>
        new Option(item.lag_policy, item.digest)));
    }
    if (job.state === "complete" && job.id !== acknowledged && ready.dataset.job !== job.id) {
      ready.textContent = `${job.symbols.join(" / ")} report is ready. Click Refresh, then select this pair in the saved run list.`;
      ready.dataset.job = job.id;
      ready.dataset.run = job.run_id;
    }
    renderControls();
    clearTimeout(timer);
    if (active()) timer = setTimeout(poll, running() ? 1000 : 5000);
  }

  async function poll() {
    const version = beginRead();
    try {
      const next = await request("?" + new URLSearchParams({job: job.id}));
      if (version !== readVersion) return;
      showError();
      showJob(next);
    } catch (error) {
      if (version !== readVersion) return;
      showError(error.message);
      if (error.status === 404) {
        await synchronize();
      } else {
        timer = setTimeout(poll, 2500);
      }
    }
  }

  async function submit(route, body) {
    submitting = true;
    beginRead();
    showError();
    renderControls();
    try {
      const next = await request(route, body);
      if (!next?.id) throw new Error("The operation response could not be confirmed.");
      showJob(next);
    } catch (error) {
      showError(error.message);
      if (!error.status || error.status >= 500) {
        uncertainWrite = {jobId: job?.id, state: job?.state, retries: 2};
        await synchronize("The operation was not confirmed. Check the saved job before retrying.");
      } else if (error.status === 409) {
        await synchronize(error.message);
      }
    } finally {
      submitting = false;
      renderControls();
    }
  }

  byId("refresh").addEventListener("click", () => {
    if (ready.textContent) pendingAcknowledgment = {job: ready.dataset.job, run: ready.dataset.run};
    if (!submitting) synchronize();
  });
  document.addEventListener("edgelab:runs-refreshed", event => {
    if (!pendingAcknowledgment || !event.detail?.runIds?.includes(pendingAcknowledgment.run)) return;
    acknowledged = pendingAcknowledgment.job;
    pendingAcknowledgment = null;
    try { sessionStorage.setItem("edgelab-pair-report-seen", acknowledged); } catch {}
    if (ready.dataset.job === acknowledged) ready.textContent = "";
  });
  retry.addEventListener("click", () => synchronize());
  for (const asset of [assetA, assetB]) {
    asset.addEventListener("input", () => loadHistory());
    asset.addEventListener("change", () => loadHistory());
  }
  for (const control of [history, start, end, candidate]) control.addEventListener("change", renderControls);

  byId("pair-setup").addEventListener("submit", event => {
    event.preventDefault();
    if (prepare.disabled) return;
    const [a, b] = selectedPair();
    const capture = history.value === "capture" ? {start: start.value, end: end.value} : null;
    submit("/prepare", {a, b, manifest: capture ? null : history.value, capture});
  });
  approve.addEventListener("click", () => {
    if (!approve.disabled) submit("/approve", {job_id: job.id, digest: candidate.value});
  });
  discard.addEventListener("click", () => {
    if (!discard.disabled) submit("/discard", {job_id: job.id});
  });

  async function synchronize(message = "") {
    const version = beginRead();
    const previous = job;
    checking = true;
    renderControls();
    try {
      const data = await request();
      if (version !== readVersion) return;
      needsRetry = false;
      const recovered = data?.job && (data.job.id !== previous?.id || data.job.state !== previous?.state);
      if (uncertainWrite && (data?.job?.id !== uncertainWrite.jobId || data?.job?.state !== uncertainWrite.state)) {
        uncertainWrite = null;
      }
      showError(recovered ? "" : message);
      if (data === null) {
        uncertainWrite = null;
        pendingAcknowledgment = null;
        job = null;
        pairKey = "";
        historyVersion++;
        ready.textContent = "";
        delete ready.dataset.job;
        delete ready.dataset.run;
        panel.hidden = true;
        byId("dashboard-mode").textContent = "Local read-only";
        return;
      }
      assets.clear();
      const options = data.assets.map(asset => {
        assets.add(asset.symbol);
        return new Option(asset.kind === "stock" ? "U.S. equity" : asset.kind.toUpperCase(), asset.symbol);
      });
      byId("pair-assets").replaceChildren(...options);
      byId("dashboard-mode").textContent = "Local research";
      panel.hidden = false;
      if (data.job) {
        showJob(data.job);
      } else {
        job = null;
        byId("pair-stage").textContent = "";
        output.hidden = true;
        review.hidden = true;
        if (assets.size < 2) historyMessage = "Pair research requires at least two configured waitlist assets.";
      }
      if (!active() && !pairKey) loadHistory(null, true);
      if (uncertainWrite) {
        needsRetry = retryUncertainWrite(message);
        if (!needsRetry) uncertainWrite = null;
      }
    } catch (error) {
      if (version !== readVersion) return;
      needsRetry = true;
      panel.hidden = false;
      byId("pair-stage").textContent = "Console unavailable";
      showError("Could not read the console status. Retry to check for saved work.");
      if (!retryUncertainWrite(message) && active()) timer = setTimeout(() => synchronize(), 2500);
    } finally {
      if (version === readVersion) {
        checking = false;
        renderControls();
      }
    }
  }
  synchronize();
})();
