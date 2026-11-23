"""Search/execute interface with a process-owned QuickJS WebAssembly guest.

The guest sees JSON and a capability RPC, never Python functions or credentials.
MCP remains an optional transport; the same interface is available as HTTP JSON.
"""
import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from mcp import types

from .auth import principal


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT / "code_runtime"


class CodeModeError(Exception):
    pass


class ExecutionPaused(CodeModeError):
    """A host control signal that guest JavaScript cannot catch or bypass."""


def node_executable():
    configured = os.environ.get("ADVANCED_MCP_NODE")
    candidates = [configured, shutil.which("node")]
    profile = os.environ.get("USERPROFILE")
    if profile:
        candidates.append(str(Path(profile) / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise CodeModeError("Node.js 20+ is required; set ADVANCED_MCP_NODE or install Node.js")


def json_type(schema, depth=0):
    """Conservative discovery hints; host-side JSON Schema remains authoritative."""
    if not isinstance(schema, dict) or depth > 6 or "$ref" in schema:
        return "unknown"
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"]) or "never"
    for union in ("anyOf", "oneOf"):
        if union in schema:
            return " | ".join(json_type(s, depth + 1) for s in schema[union])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(json_type({**schema, "type": item}, depth + 1) for item in kind)
    if kind == "object" or "properties" in schema:
        required = schema.get("required", [])
        fields = [f"{json.dumps(k)}{'' if k in required else '?'}: {json_type(v, depth + 1)}"
                  for k, v in schema.get("properties", {}).items()]
        return "{ " + "; ".join(fields) + " }"
    if kind == "array":
        return f"Array<{json_type(schema.get('items', {}), depth + 1)}>"
    return {"integer": "number", "number": "number", "string": "string", "boolean": "boolean", "null": "null"}.get(kind, "unknown")


class CodeExecutor:
    def __init__(self, config):
        self.config = config
        self.active = 0

    def preflight(self):
        node_executable()
        if not (RUNTIME_DIR / "node_modules/quickjs-emscripten/package.json").is_file():
            raise CodeModeError("Code runtime missing. Run pnpm install --frozen-lockfile in the bundled code_runtime directory")

    async def run(self, mode, code, catalog, invoke, *, replay_context=None):
        config = self.config
        if not isinstance(code, str) or len(code.encode("utf-8")) > config.max_code_bytes:
            raise CodeModeError("Code exceeds the configured size limit")
        if self.active >= config.max_executions:
            raise CodeModeError("Code executor is at capacity")
        self.preflight()
        self.active += 1
        process, tasks = None, set()
        calls, completed = 0, 0
        permits = asyncio.Semaphore(config.max_parallel_calls)
        write_lock = asyncio.Lock()
        interrupted = asyncio.get_running_loop().create_future()
        try:
            # The host shim needs no provider tokens or user environment variables.
            environment = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
            async with asyncio.timeout(config.timeout_seconds):
                process = await asyncio.create_subprocess_exec(node_executable(), "--max-old-space-size=128",
                    str(RUNTIME_DIR / "runner.mjs"), cwd=RUNTIME_DIR, env=environment,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=config.max_frame_bytes,
                    **({"creationflags": 0x08000000} if os.name == "nt" else {}))

                async def send(message):
                    raw = json.dumps(message, ensure_ascii=True, allow_nan=False).encode() + b"\n"
                    if len(raw) > config.max_frame_bytes:
                        raise CodeModeError("Capability response too large; narrow the upstream operation")
                    async with write_lock:
                        process.stdin.write(raw)
                        await process.stdin.drain()

                initial = {"mode": mode, "code": code, "catalog": catalog,
                    "timeout_ms": int(config.timeout_seconds * 1000), "memory_bytes": config.memory_mb * 1024 * 1024,
                    "max_calls": config.max_calls, "max_result_bytes": config.max_result_bytes,
                    "max_argument_bytes": config.max_code_bytes}
                if replay_context is not None:
                    initial["replay_context"] = replay_context
                raw = json.dumps(initial, ensure_ascii=True).encode() + b"\n"
                if len(raw) > config.max_catalog_bytes:
                    raise CodeModeError("Authorized catalog exceeds the sandbox catalog limit")
                process.stdin.write(raw)
                await process.stdin.drain()

                async def dispatch(message):
                    nonlocal completed
                    try:
                        async with permits:
                            if replay_context is None:
                                result = await invoke(message["name"], message["arguments"])
                            else:
                                result = await invoke(message["id"], message["name"], message["arguments"])
                        await send({"type": "reply", "id": message["id"], "result": result})
                        completed += 1
                    except asyncio.CancelledError:
                        raise
                    except ExecutionPaused as exc:
                        if not interrupted.done():
                            interrupted.set_result(exc)
                    except Exception as exc:
                        await send({"type": "reply", "id": message["id"], "error": str(exc)[:1024]})

                while True:
                    read_task = asyncio.create_task(process.stdout.readline())
                    try:
                        await asyncio.wait((read_task, interrupted), return_when=asyncio.FIRST_COMPLETED)
                        if interrupted.done():
                            raise interrupted.result()
                        line = read_task.result()
                    finally:
                        if not read_task.done():
                            read_task.cancel()
                            await asyncio.gather(read_task, return_exceptions=True)
                    if not line:
                        raise CodeModeError("Sandbox stopped without a result")
                    message = json.loads(line)
                    if message.get("type") == "call":
                        if mode != "execute" or calls >= config.max_calls:
                            raise CodeModeError("Capability call budget or mode violation")
                        if not isinstance(message.get("name"), str) or not isinstance(message.get("arguments"), dict):
                            raise CodeModeError("Malformed capability request")
                        calls += 1
                        task = asyncio.create_task(dispatch(message))
                        tasks.add(task)
                        # Keep failures observable; task count is bounded by max_calls.
                    elif message.get("type") == "error":
                        raise CodeModeError(message.get("message", "Sandbox failed"))
                    elif message.get("type") == "done":
                        if tasks:
                            await asyncio.gather(*tasks)
                        if interrupted.done():
                            raise interrupted.result()
                        result = {"result": message.get("result"), "logs": message.get("logs", []),
                                  "calls": calls, "completed_calls": completed}
                        if len(json.dumps(result).encode()) > config.max_frame_bytes:
                            raise CodeModeError("Sandbox result exceeds output limit")
                        return result
                    else:
                        raise CodeModeError("Unexpected sandbox protocol message")
        except TimeoutError:
            raise CodeModeError("Execution deadline exceeded; backend outcomes may be unknown") from None
        except (ValueError, OSError) as exc:
            raise CodeModeError("Sandbox protocol or process failure") from exc
        finally:
            if process and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.active -= 1


class CodeSurface:
    def __init__(self, gateway):
        self.gateway = gateway
        self.executor = CodeExecutor(gateway.settings.code_mode)

    @staticmethod
    def tools():
        schema = {"type": "object", "properties": {"code": {"type": "string", "maxLength": 32768}},
                  "required": ["code"], "additionalProperties": False}
        return [types.Tool(name="search", description="Run a JavaScript async function to inspect authorized capabilities. await codemode.tools() returns names, descriptions, inputSchema, outputSchema, readOnly and TypeScript declaration. Return only relevant definitions. No capability execution in search.",
                inputSchema=schema, annotations=types.ToolAnnotations(readOnlyHint=True)),
            types.Tool(name="execute", description="Run a JavaScript async function. Discover with codemode.search(query) and codemode.describe(path); invoke connectors.NAME.method(args), codemode.NAME(args) or codemode.call(path,args). Use loops, Promise.all and return focused JSON. Host validates every call. No filesystem, environment, imports or direct network API. Writes require allow_writes:true plus existing permissions. No automatic retries.",
                inputSchema={**schema, "properties": {**schema["properties"], "allow_writes": {"type": "boolean", "default": False}}},
                annotations=types.ToolAnnotations(readOnlyHint=False))]

    def read_only(self, name):
        backend, _, tool = name.partition("__")
        pool = self.gateway.pools.get(backend)
        return bool(pool and tool in pool.config.read_only_tools)

    async def catalog(self):
        tools = await self.gateway.list_tools()
        result = []
        for tool in tools:
            if "__" not in tool.name:
                continue
            item = tool.model_dump(mode="json", exclude_none=True)
            backend, _, method = tool.name.partition("__")
            connector = self.gateway.connectors[backend]
            item.update({"path": f"{backend}.{method}", "connector": backend, "transport": connector.config.kind})
            if hasattr(connector, "describe"):
                item.update(connector.describe(method))
            item["readOnly"] = self.read_only(tool.name)
            item["declaration"] = f"connectors[{json.dumps(backend)}][{json.dumps(method)}](args: {json_type(item['inputSchema'])}): Promise<{json_type(item.get('outputSchema') or {})}>"
            result.append(item)
        return result

    async def run(self, name, arguments):
        started, outcome = time.monotonic(), "error"
        mode = name if name in {"search", "execute"} else "unknown"
        try:
            result = await self._run(name, arguments)
            outcome = "success"
            return result
        finally:
            user = principal.get()
            self.gateway.code_runs.labels(mode, outcome).inc()
            logging.getLogger("advanced_mcp.audit").info(json.dumps({"event": "code_execution",
                "subject": user.subject if user else "unauthenticated", "mode": mode,
                "outcome": outcome, "seconds": round(time.monotonic() - started, 6)}))

    async def _run(self, name, arguments):
        from .gateway import SCHEMA_REGISTRY
        import jsonschema
        if principal.get() is None:
            raise CodeModeError("Authentication required")
        tool = next((t for t in self.tools() if t.name == name), None)
        if tool is None:
            raise CodeModeError("Unknown Code Mode operation")
        try:
            jsonschema.validate(arguments, tool.inputSchema, registry=SCHEMA_REGISTRY)
        except jsonschema.ValidationError:
            raise CodeModeError("Arguments do not match the Code Mode schema") from None
        async def invoke(capability, values):
            return await self.invoke(capability, values, allow_writes=arguments.get("allow_writes", False))
        try:
            async with asyncio.timeout(self.gateway.settings.code_mode.timeout_seconds):
                catalog = await self.catalog() if name == "search" else []
                return await self.executor.run(name, arguments["code"], catalog, invoke)
        except TimeoutError:
            raise CodeModeError("Code Mode deadline exceeded; backend outcomes may be unknown") from None

    async def invoke(self, capability, values, *, allow_writes=False, durable_approved=False):
        if capability in {"@platform/search", "@platform/describe"}:
            return await self.discover(capability, values)
        if "__" not in capability and "." in capability:
            capability = capability.replace(".", "__", 1)
        if capability in {"search", "execute", "gateway_search_tools"}:
            raise CodeModeError("Code Mode cannot invoke orchestration tools recursively")
        if not allow_writes and not self.read_only(capability):
            raise CodeModeError("Capability is not declared read-only; explicit allow_writes is required")
        result = await self.gateway.call_tool(capability, values, durable_approved=durable_approved)
        if result.isError:
            text = next((c.text for c in result.content if isinstance(c, types.TextContent)), "Capability failed")
            raise CodeModeError(text[:1024])
        if result.structuredContent is not None:
            return result.structuredContent
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except ValueError:
                return texts[0]
        return {"content": [c.model_dump(mode="json") for c in result.content]}

    async def discover(self, operation, values):
        """Host-side progressive discovery uses the same principal-filtered catalog."""
        import jsonschema
        if operation == "@platform/search":
            schema = {"type": "object", "properties": {"query": {"type": "string", "maxLength": 300},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["query"], "additionalProperties": False}
        else:
            schema = {"type": "object", "properties": {"path": {"type": "string", "maxLength": 256}},
                "required": ["path"], "additionalProperties": False}
        try:
            jsonschema.validate(values, schema)
        except jsonschema.ValidationError:
            raise CodeModeError("Invalid discovery arguments") from None
        catalog = await self.catalog()
        if operation == "@platform/describe":
            path = values["path"].replace("__", ".", 1)
            match = next((item for item in catalog if item["path"] == path), None)
            if match is None:
                raise CodeModeError("Capability unavailable or access denied")
            return match
        words = values["query"].casefold().split()
        matches = [item for item in catalog if all(word in (item["path"] + " " + item.get("description", "")).casefold() for word in words)]
        return {"results": [{key: item[key] for key in ("path", "name", "description", "readOnly", "transport") if key in item}
                            for item in matches[:values.get("limit", 10)]], "matched": len(matches)}
