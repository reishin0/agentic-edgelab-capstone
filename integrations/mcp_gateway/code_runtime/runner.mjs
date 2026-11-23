// Trusted host shim. User source is evaluated only by QuickJS compiled to WebAssembly.
import readline from "node:readline";
import { getQuickJS } from "quickjs-emscripten";

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const send = (message) => process.stdout.write(JSON.stringify(message) + "\n");
let context, runtime, program, watchdog, poller;
let started = false, finished = false, terminal;
let nextId = 0, logBytes = 0;
const pending = new Map(), logs = [];
let limits;

function finish(error = undefined) {
  if (finished) return;
  let result;
  if (!error) {
    try { result = JSON.parse(terminal); }
    catch { error = "Guest result is not valid JSON"; }
  }
  finished = true;
  clearTimeout(watchdog);
  clearInterval(poller);
  if (!error && pending.size) error = "Execution ended with unfinished capability calls";
  send(error ? { type: "error", message: String(error).slice(0, 1024) }
    : { type: "done", result, logs });
  // Each process owns one guest. Exit releases every guest handle, including failed/OOM guests.
  input.close();
  process.stdin.destroy();
  process.exitCode = error ? 1 : 0;
}

function guestError(handle) {
  const data = context.dump(handle);
  return String(data?.message ?? "Guest JavaScript failed").slice(0, 1024);
}

function pump() {
  if (finished || !program) return;
  try {
    const jobs = runtime.executePendingJobs(64);
    if (jobs.error) {
      const message = guestError(jobs.error);
      jobs.error.dispose();
      return finish(message);
    }
    if (terminal === undefined) {
      const state = context.getPromiseState(program);
      if (state.type === "rejected") {
        const message = guestError(state.error);
        state.error.dispose();
        return finish(message);
      }
      if (state.type === "fulfilled") {
        terminal = context.getString(state.value);
        state.value.dispose();
        if (Buffer.byteLength(terminal, "utf8") > limits.max_result_bytes)
          return finish("Result too large; filter or summarize before returning");
      }
    }
    if (terminal !== undefined && pending.size === 0 && !runtime.hasPendingJob()) finish();
  } catch {
    finish("Guest execution failed or exceeded its memory/time limit");
  }
}

async function start(config) {
  limits = config;
  const deadline = Date.now() + config.timeout_ms;
  watchdog = setTimeout(() => finish("Execution deadline exceeded; backend outcomes may be unknown"), config.timeout_ms);
  const QuickJS = await getQuickJS();
  if (finished) return;
  runtime = QuickJS.newRuntime();
  runtime.setMemoryLimit(config.memory_bytes);
  runtime.setMaxStackSize(256 * 1024);
  runtime.setInterruptHandler(() => Date.now() >= deadline);
  context = runtime.newContext();

  const bridge = context.newFunction("bridge", (nameHandle, argsHandle) => {
    const reject = (message) => ({ error: context.newError(message) });
    if (config.mode !== "execute") return reject("Search cannot invoke capabilities");
    if (nextId >= config.max_calls) return reject("Capability call budget exceeded");
    let name, args;
    try {
      name = context.getString(nameHandle);
      const raw = context.getString(argsHandle);
      if (name.length > 256 || Buffer.byteLength(raw) > config.max_argument_bytes)
        return reject("Capability arguments too large");
      args = JSON.parse(raw);
      if (!args || typeof args !== "object" || Array.isArray(args)) return reject("Arguments must be an object");
    } catch {
      return reject("Invalid capability arguments");
    }
    const id = ++nextId;
    const deferred = context.newPromise();
    pending.set(id, deferred);
    send({ type: "call", id, name, arguments: args });
    return deferred.handle;
  });
  const logger = context.newFunction("capture", (valueHandle) => {
    let text;
    try { text = context.getString(valueHandle).slice(0, 2048); }
    catch { return context.undefined; }
    const size = Buffer.byteLength(text);
    if (logBytes + size <= 8192 && logs.length < 40) { logs.push(text); logBytes += size; }
    return context.undefined;
  });
  context.setProp(context.global, "__bridge", bridge);
  context.setProp(context.global, "__capture", logger);
  bridge.dispose(); logger.dispose();
  const catalog = context.newString(JSON.stringify(config.catalog ?? []));
  context.setProp(context.global, "__catalogJSON", catalog);
  catalog.dispose();
  const mode = JSON.stringify(config.mode);
  const replayContext = JSON.stringify(config.replay_context ?? null);
  const bootstrap = context.evalCode(`(() => {
    const bridge = globalThis.__bridge, capture = globalThis.__capture;
    const catalog = JSON.parse(globalThis.__catalogJSON);
    delete globalThis.__bridge; delete globalThis.__capture; delete globalThis.__catalogJSON;
    const mode = ${mode};
    const replay = ${replayContext};
    if (replay) {
      const NativeDate = Date;
      function WorkflowDate(...args) {
        if (new.target) return Reflect.construct(NativeDate, args.length ? args : [replay.time_ms], new.target);
        return new NativeDate(replay.time_ms).toString();
      }
      Object.setPrototypeOf(WorkflowDate, NativeDate);
      WorkflowDate.prototype = NativeDate.prototype;
      Object.defineProperty(WorkflowDate.prototype, "constructor", {value: WorkflowDate});
      WorkflowDate.now = () => replay.time_ms;
      globalThis.Date = WorkflowDate;
      let seed = replay.seed >>> 0 || 1;
      Math.random = () => { seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5; return (seed >>> 0) / 4294967296; };
    }
    const call = async (name, args = {}) => JSON.parse(await bridge(String(name), JSON.stringify(args)));
    const connectors = new Proxy(Object.create(null), {
      get: (_, namespace) => namespace === "then" ? undefined : new Proxy(Object.create(null), {
        get: (_, method) => method === "then" ? undefined : (args = {}) => call(String(namespace) + "__" + String(method), args)
      })
    });
    const search = (query, options = {}) => call("@platform/search", {query, limit: options.limit ?? 10});
    const describe = (path) => call("@platform/describe", {path});
    const target = Object.create(null);
    const api = mode === "search" ? Object.freeze({ tools: async () => catalog }) : new Proxy(target, {
      get: (_, name) => name === "then" ? undefined : name === "call" ? call :
        name === "connectors" ? connectors : name === "search" ? search : name === "describe" ? describe :
        typeof name === "string" ? (args = {}) => call(name, args) : undefined
    });
    Object.defineProperty(globalThis, "codemode", { value: api, writable: false, configurable: false });
    if (mode === "execute") Object.defineProperty(globalThis, "connectors", {value: connectors, writable: false, configurable: false});
    const log = (...values) => capture(values.map(value => typeof value === "string" ? value : JSON.stringify(value)).join(" "));
    Object.defineProperty(globalThis, "console", { value: Object.freeze({ log, warn: log, error: log }), writable: false });
  })()`, "bootstrap.js");
  context.unwrapResult(bootstrap).dispose();
  const fn = context.unwrapResult(context.evalCode(`(${config.code}\n)`, "plan.js"));
  if (context.typeof(fn) !== "function") { fn.dispose(); return finish("Code must be an async function expression"); }
  context.setProp(context.global, "__program", fn);
  fn.dispose();
  program = context.unwrapResult(context.evalCode(
    "Promise.resolve().then(() => __program()).then(value => { const text = JSON.stringify(value === undefined ? null : value); if (typeof text !== 'string') throw new Error('Result must be JSON serializable'); return text; })", "invoke.js"));
  poller = setInterval(pump, 2);
  pump();
}

input.on("line", (line) => {
  if (finished) return;
  try {
    const message = JSON.parse(line);
    if (!started) {
      started = true;
      start(message).catch(() => finish("Guest compilation or runtime initialization failed"));
    } else if (message.type === "reply" && pending.has(message.id)) {
      const deferred = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) {
        const error = context.newError(String(message.error).slice(0, 1024));
        deferred.reject(error); error.dispose();
      } else {
        const value = context.newString(JSON.stringify(message.result));
        deferred.resolve(value); value.dispose();
      }
      deferred.dispose();
      pump();
    }
  } catch { finish("Invalid sandbox protocol message"); }
});
input.on("close", () => { if (!finished) finish("Sandbox input closed"); });
