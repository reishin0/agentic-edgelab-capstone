"use strict";

(() => {
  const dialog = $("research-settings"), trigger = $("open-settings");
  const provider = $("settings-provider"), model = $("settings-model"), filter = $("settings-model-filter");
  const free = $("settings-free"), source = $("settings-retrieval"), refresh = $("settings-refresh-models");
  const test = $("settings-test-model"), modelStatus = $("settings-model-status");
  const evidenceStatus = $("settings-retrieval-status"), command = $("settings-command");
  const copy = $("settings-copy"), copyStatus = $("settings-copy-status");
  const storage = "edgelab.research-settings.v1";
  const safeId = /^[a-zA-Z0-9][a-zA-Z0-9._/@:+-]{0,199}$/;
  const selected = {};
  let models = [], catalogRequest = 0, modelTest = 0, pendingTest = 0, retrievalTest = 0;

  try {
    const saved = JSON.parse(localStorage.getItem(storage));
    if (["lm_studio", "openrouter", "none"].includes(saved?.provider)) provider.value = saved.provider;
    if (["local", "pinecone"].includes(saved?.retrieval)) source.value = saved.retrieval;
    for (const key of ["lm_studio", "openrouter"]) {
      if (typeof saved?.models?.[key] === "string" && safeId.test(saved.models[key])) selected[key] = saved.models[key];
    }
  } catch { /* Local storage is optional. */ }

  function save() {
    try { localStorage.setItem(storage, JSON.stringify({provider: provider.value, retrieval: source.value, models: selected})); }
    catch { /* The visible settings still work. */ }
  }

  async function post(action, data) {
    const response = await fetch("/api/settings/" + action, {
      method: "POST", cache: "no-store", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)
    });
    const reply = await response.json();
    if (!response.ok) throw new Error(reply.error || "Settings request failed.");
    return reply;
  }

  function updateCommand() {
    const kind = provider.value, id = selected[kind];
    const choice = models.find(row => row.id === id && (kind !== "lm_studio" || row.loaded));
    const ready = kind === "none" || Boolean(choice);
    const flags = [];
    if (ready && kind !== "none") {
      flags.push(kind === "lm_studio" ? "--lm-studio" : "--openrouter", "--model-id", `'${id}'`);
      if (kind === "openrouter" && !free.checked) flags.push("--allow-paid-model");
    }
    if (source.value === "pinecone") flags.push("--retrieval", "pinecone");
    const needsProviders = kind !== "none" || source.value === "pinecone";
    if (needsProviders) flags.push("--allow-provider-io");
    const settingsFile = needsProviders ? " --settings-file providers.env" : "";
    command.value = ready ? "python -B -m edgelab.capstone" + settingsFile + " plan" +
      (flags.length ? " " + flags.join(" ") : "") : "";
    command.placeholder = kind === "lm_studio" ? "Choose a loaded model or run without model advice." :
      "Refresh the OpenRouter catalog and choose a model.";
    test.disabled = !choice || (pendingTest !== 0 && pendingTest === modelTest);
    copy.disabled = !ready;
    copyStatus.textContent = "";
  }

  function showModelDetail() {
    const row = models.find(entry => entry.id === model.value);
    const detail = $("settings-model-detail");
    if (!row) { detail.textContent = ""; return; }
    const fields = [row.id];
    if (row.context_length) fields.push(Number(row.context_length).toLocaleString() + " token context");
    if (row.free === true) fields.push("Free · structured output");
    else if (row.pricing) {
      for (const [key, label] of [["prompt", "input"], ["completion", "output"]]) {
        if (row.pricing[key] != null) fields.push("$" + (Number(row.pricing[key]) * 1e6).toLocaleString() + " / 1M " + label);
      }
      if (Number(row.pricing.request) > 0) fields.push("$" + row.pricing.request + " / request");
    }
    detail.textContent = fields.join(" · ");
  }

  function renderModels() {
    const kind = provider.value, query = filter.value.trim().toLowerCase();
    const visible = models.filter(row => (row.name + " " + row.id).toLowerCase().includes(query));
    const placeholder = node("option", visible.length ? "Choose a model" : "No matching models");
    placeholder.value = "";
    model.replaceChildren(placeholder, ...visible.map(row => {
      const suffix = kind === "lm_studio" ? row.loaded ? " · Loaded" : " · On disk" : row.free ? " · Free" : " · Paid";
      const item = node("option", row.name + suffix);
      item.value = row.id;
      item.disabled = kind === "lm_studio" && !row.loaded;
      return item;
    }));
    model.value = visible.some(row => row.id === selected[kind]) ? selected[kind] : "";
    model.disabled = !visible.length;
    $("settings-catalog").hidden = !models.length;
    showModelDetail();
    updateCommand();
  }

  async function loadCatalog() {
    const kind = provider.value, request = ++catalogRequest;
    modelTest++;
    models = [];
    renderModels();
    refresh.disabled = true;
    modelStatus.textContent = "Reading the model catalog…";
    try {
      const result = await post("catalog", {provider: kind, free_only: free.checked});
      if (request !== catalogRequest) return;
      models = result.models.filter(row => safeId.test(row.id));
      if (kind === "lm_studio") {
        const keys = new Set(models.map(row => row.model_key));
        models.push(...(result.downloaded || []).filter(row => !keys.has(row.model_key))
          .map(row => ({id: row.file, name: row.file.split("/").at(-1), loaded: false})));
        const loaded = models.filter(row => row.loaded);
        if (!selected[kind] && loaded.length === 1) selected[kind] = loaded[0].id;
        modelStatus.textContent = loaded.length ? `${loaded.length} loaded model(s).` :
          "Load a model in LM Studio and start its local server, then refresh.";
      } else {
        modelStatus.textContent = result.status === "not_configured" ? "Set OPENROUTER_API_KEY in providers.env." :
          result.status !== "available" ? "The OpenRouter catalog could not be read." : `${models.length} compatible model(s).`;
      }
      renderModels();
    } catch (error) {
      if (request === catalogRequest) modelStatus.textContent = error.message;
    } finally {
      if (request === catalogRequest) refresh.disabled = false;
    }
  }

  function changeProvider() {
    catalogRequest++;
    modelTest++;
    models = [];
    free.checked = true;
    filter.value = "";
    refresh.disabled = false;
    const kind = provider.value;
    $("settings-model-controls").hidden = kind === "none";
    $("settings-cloud").hidden = kind !== "openrouter";
    modelStatus.textContent = kind === "openrouter" ? "Refresh models to query the OpenRouter API." : "";
    renderModels();
    if (kind === "lm_studio") loadCatalog();
  }

  trigger.addEventListener("click", () => { dialog.showModal(); changeProvider(); });
  $("close-settings").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => trigger.focus());
  provider.addEventListener("change", () => { save(); changeProvider(); });
  refresh.addEventListener("click", loadCatalog);
  free.addEventListener("change", () => {
    catalogRequest++;
    modelTest++;
    models = [];
    refresh.disabled = false;
    modelStatus.textContent = "Refresh the catalog with this price filter.";
    renderModels();
  });
  filter.addEventListener("input", renderModels);
  model.addEventListener("change", () => {
    modelTest++;
    selected[provider.value] = model.value;
    save();
    showModelDetail();
    updateCommand();
  });
  test.addEventListener("click", async () => {
    const request = ++modelTest;
    pendingTest = request;
    test.disabled = true;
    modelStatus.textContent = "Sending a synthetic choice test…";
    try {
      const reply = await post("test-model", {provider: provider.value, model_id: selected[provider.value], free_only: free.checked});
      if (request === modelTest) modelStatus.textContent = reply.status === "passed" ?
        `Synthetic choice passed · ${reply.elapsed_ms} ms.` : (reply.message || "Synthetic test did not pass.");
    } catch (error) {
      if (request === modelTest) modelStatus.textContent = error.message;
    } finally {
      if (request === pendingTest) pendingTest = 0;
      if (request === modelTest) updateCommand();
    }
  });

  source.addEventListener("change", () => {
    retrievalTest++;
    evidenceStatus.replaceChildren();
    $("settings-test-retrieval").disabled = false;
    save();
    updateCommand();
  });
  const names = {adf: "ADF", kpss: "KPSS", eg: "Engle–Granger"};
  $("settings-test-retrieval").addEventListener("click", async event => {
    const button = event.currentTarget, request = ++retrievalTest;
    button.disabled = true;
    evidenceStatus.textContent = "Checking the required method notes…";
    try {
      const reply = await post("test-retrieval", {backend: source.value});
      if (request !== retrievalTest) return;
      const required = reply.required, elapsed = Number.isFinite(required.elapsed_ms) ? ` · ${required.elapsed_ms} ms` : "";
      evidenceStatus.replaceChildren(node("p", `${reply.corpus_version} · ${required.verified_ids.length}/3 verified${elapsed}`));
      if (required.missing_ids.length) {
        evidenceStatus.append(node("p", "Missing " + required.missing_ids.map(key => names[key]).join(", ") + ". Planning will be unresolved."));
      }
      if (reply.citations.length) {
        const citations = node("ul");
        for (const citation of reply.citations) {
          const item = node("li");
          item.append(safeLink(names[citation.id] + " · " + citation.locator, citation.url));
          citations.append(item);
        }
        evidenceStatus.append(citations);
      }
      if (reply.semantic) {
        const recall = reply.semantic.recall_at_3;
        evidenceStatus.append(node("p", Number.isFinite(recall) ? `Semantic top three · ${Math.round(recall * 3)}/3 required notes` :
          "Semantic ranking unavailable."));
      }
    } catch (error) {
      if (request === retrievalTest) evidenceStatus.textContent = error.message;
    } finally {
      if (request === retrievalTest) button.disabled = false;
    }
  });
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(command.value); copyStatus.textContent = "Copied"; }
    catch { command.focus(); command.select(); copyStatus.textContent = "Command selected for copying"; }
  });
})();
