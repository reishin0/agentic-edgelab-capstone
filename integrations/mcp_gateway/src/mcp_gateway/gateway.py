import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import jsonschema
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from referencing import Registry
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .auth import Authenticator, authorized, principal, subject_token
from .backend import BackendUnavailable, OperationNotApplied
from .config import Settings
from .connectors import create_connector
from .exchange import TokenExchange

log = logging.getLogger("mcp_gateway.audit")
# An explicit empty registry supports local schema references but has no network retriever.
SCHEMA_REGISTRY = Registry()

SEARCH_TOOL = types.Tool(name="gateway_search_tools", description="Find authorized tools by name or description. Returns their input schemas.",
    inputSchema={"type": "object", "properties": {"query": {"type": "string", "maxLength": 300},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["query"], "additionalProperties": False})


class Gateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.exchange = TokenExchange()
        self.connectors = {b.name: create_connector(b) for b in settings.backends}
        self.pools = self.connectors  # Compatibility for existing integrations and tests.
        self.registry = CollectorRegistry()
        self.calls = Counter("mcp_gateway_tool_calls", "Tool outcomes", ["backend", "outcome"], registry=self.registry)
        self.latency = Histogram("mcp_gateway_tool_seconds", "Tool latency", ["backend"], registry=self.registry)
        self.discovery_errors = Counter("mcp_gateway_discovery_failures", "Failed discovery", ["backend"], registry=self.registry)
        self.http_requests = Counter("mcp_gateway_http_requests", "HTTP outcomes", ["status"], registry=self.registry)
        self.code_runs = Counter("mcp_gateway_code_runs", "Code Mode execution outcomes", ["mode", "outcome"], registry=self.registry)
        self.code_surface = None
        self.workflows = None
        if settings.surface == "code":
            from .code_mode import CodeSurface
            self.code_surface = CodeSurface(self)
            if settings.durable.database:
                from .durable import DurableWorkflows
                self.workflows = DurableWorkflows(self.code_surface)
        self.server = Server("mcp_gateway", version="0.1.0", instructions="Tools are named backend__tool. Use gateway_search_tools to find authorized tools. Failed calls are never automatically retried.")
        self.server.list_tools()(self.advertised_tools)
        # Authorization and input validation run together below; SDK's shared tool cache is not a policy boundary.
        self.server.call_tool(validate_input=False)(self.call_code_tool if self.code_surface else self.call_tool)

    async def advertised_tools(self):
        return self.code_surface.tools() if self.code_surface else await self.list_tools()

    async def call_code_tool(self, name, arguments):
        from .code_mode import CodeModeError
        try:
            result = await self.code_surface.run(name, arguments)
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))], structuredContent=result)
        except CodeModeError as exc:
            return self.error(str(exc))

    async def list_tools(self):
        user = principal.get()
        if user is None:
            return []

        async def discover(pool):
            if not user.roles.intersection(pool.config.roles):
                return []
            try:
                credential, cache_key = None, "service"
                if getattr(pool, "discovery_requires_credential", True):
                    credential, cache_key = await self.exchange.credential(pool.config, subject_token.get())
                tools = await pool.list_tools(self.settings.discovery_ttl_seconds, credential=credential, cache_key=cache_key)
                return [tool.model_copy(update={"name": f"{pool.config.name}__{tool.name}"})
                        for tool in tools if authorized(user, pool.config, tool.name)]
            except BackendUnavailable:
                self.discovery_errors.labels(pool.config.name).inc()
                return []

        results = await asyncio.gather(*(discover(pool) for pool in self.pools.values()))
        return [SEARCH_TOOL] + [tool for group in results for tool in group]

    async def call_tool(self, name: str, arguments: dict, *, durable_approved: bool = False):
        started = time.monotonic()
        user = principal.get()
        backend_name, sep, tool_name = name.partition("__")
        pool = self.pools.get(backend_name) if sep else None
        backend_label = backend_name if pool else "gateway"
        outcome = "error"
        try:
            if user is None:
                outcome = "denied"
                raise PermissionError("Authentication required")
            if name == SEARCH_TOOL.name:
                jsonschema.validate(arguments, SEARCH_TOOL.inputSchema, registry=SCHEMA_REGISTRY)
                query = arguments["query"].casefold()
                matches = [t.model_dump(mode="json", exclude_none=True) for t in await self.list_tools()
                           if t.name != SEARCH_TOOL.name and query in (t.name + " " + (t.description or "")).casefold()]
                outcome = "success"
                result = {"tools": matches[:arguments.get("limit", 10)], "matched": len(matches)}
                return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))], structuredContent=result)
            if pool is None or not authorized(user, pool.config, tool_name):
                outcome = "denied"
                raise PermissionError("Tool unavailable or access denied")
            if tool_name in pool.config.durable_only_tools and durable_approved is not True:
                outcome = "denied"
                raise PermissionError("Tool requires exact durable workflow approval")
            credential, cache_key = None, "service"
            if getattr(pool, "discovery_requires_credential", True):
                credential, cache_key = await self.exchange.credential(pool.config, subject_token.get())
            tools = await pool.list_tools(self.settings.discovery_ttl_seconds, credential=credential, cache_key=cache_key)
            definition = next((t for t in tools if t.name == tool_name), None)
            if definition is None:
                raise ValueError("Tool is not advertised by the configured backend")
            jsonschema.validate(arguments, definition.inputSchema, registry=SCHEMA_REGISTRY)
            if not getattr(pool, "discovery_requires_credential", True):
                credential, _ = await self.exchange.credential(pool.config, subject_token.get())
            result = await pool.request("call_tool", tool_name, arguments, credential=credential)
            durable_tools = pool.config.durable_only_tools
            if not result.isError and (tool_name in durable_tools or tool_name in durable_tools.values()):
                receipt = result.structuredContent
                if not isinstance(receipt, dict) or not receipt:
                    raise ValueError("Backend did not return a structured receipt")
                if definition.outputSchema is not None:
                    try:
                        jsonschema.validate(receipt, definition.outputSchema, registry=SCHEMA_REGISTRY)
                    except jsonschema.ValidationError:
                        raise ValueError("Backend receipt did not match its response schema") from None
            outcome = "tool_error" if result.isError else "success"
            return result
        except OperationNotApplied:
            outcome = "not_applied"
            if durable_approved is True and pool is not None and tool_name in pool.config.durable_only_tools:
                raise
            return self.error("Backend refused the operation")
        except jsonschema.ValidationError:
            outcome = "invalid_input"
            return self.error("Arguments do not match the tool input schema")
        except (PermissionError, ValueError, BackendUnavailable) as exc:
            return self.error(str(exc))
        except Exception:
            return self.error("Gateway operation failed")
        finally:
            duration = time.monotonic() - started
            self.calls.labels(backend_label, outcome).inc()
            self.latency.labels(backend_label).observe(duration)
            # Never record tokens, arguments, returned documents, or arbitrary untrusted tool names.
            log.info(json.dumps({"event": "tool_call", "request_id": uuid.uuid4().hex,
                "subject": user.subject if user else "unauthenticated", "backend": backend_label,
                "outcome": outcome, "seconds": round(duration, 6)}))

    @staticmethod
    def error(message):
        return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=message)])


class Guard:
    """Pure ASGI middleware: buffers bounded requests and propagates identity to SDK tasks."""
    def __init__(self, app, gateway: Gateway, authenticator: Authenticator):
        self.app, self.gateway, self.auth = app, gateway, authenticator
        self.buckets = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        settings = self.gateway.settings

        async def reject(status, message):
            self.gateway.http_requests.labels(str(status)).inc()
            await JSONResponse({"error": message}, status_code=status)(scope, receive, send)

        authorities = request.headers.getlist("host")
        try:
            if len(authorities) != 1 or any(ord(c) <= 32 for c in authorities[0]):
                raise ValueError("Invalid Host")
            authority = urlsplit("//" + authorities[0])
            host, port = authority.hostname, authority.port
            if (not host or not host.isascii() or authority.username is not None or authority.password is not None
                    or authority.path or authority.query or authority.fragment):
                raise ValueError("Invalid Host")
        except ValueError:
            return await reject(403, "Invalid Host")
        if host not in {h.strip("[]").lower() for h in settings.allowed_hosts}:
            return await reject(403, "Invalid Host")
        # Canonicalize only after validation, including brackets required by IPv6 authority syntax.
        # The SDK receives the same validated host even if the original used mixed case.
        canonical = f"[{host}]" if ":" in host else host
        if port is not None:
            canonical += f":{port}"
        scope = dict(scope, headers=[(key, canonical.encode("ascii") if key.lower() == b"host" else value)
                                     for key, value in scope["headers"]])
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            return await reject(403, "Invalid Origin")
        if scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        user = await self.auth.authenticate(request.headers.get("authorization", ""))
        if user is None:
            return await reject(401, "Valid bearer token required")
        now = time.monotonic()
        count, window = self.buckets.pop(user.subject, (0, now))
        if now - window >= 60:
            count, window = 0, now
        self.buckets[user.subject] = (count + 1, window)
        # Prevent identities from causing unbounded in-memory cardinality.
        while len(self.buckets) > 10000:
            self.buckets.popitem(last=False)
        if count >= settings.requests_per_minute:
            return await reject(429, "Rate limit exceeded")
        if scope["path"] in {"/metrics", "/readyz"} and "operator" not in user.roles:
            return await reject(403, "Operator role required")
        if scope["path"] == "/mcp" and scope["method"] == "GET":
            return await reject(405, "This stateless gateway does not offer a listening SSE stream")
        chunks, size = [], 0
        if scope["method"] == "POST":
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        size += len(chunk)
                        if size > settings.max_body_bytes:
                            return await reject(413, "Request body too large")
                        chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await reject(408, "Request body deadline exceeded")
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def observed_send(message):
            if message["type"] == "http.response.start":
                self.gateway.http_requests.labels(str(message["status"])).inc()
            await send(message)

        marker = principal.set(user)
        token_marker = subject_token.set(request.headers.get("authorization", "").partition(" ")[2])
        try:
            await self.app(scope, replay if scope["method"] == "POST" else receive, observed_send)
        finally:
            principal.reset(marker)
            subject_token.reset(token_marker)


def create_app(settings: Settings):
    gateway = Gateway(settings)
    auth = Authenticator(settings.auth)
    hosts = [h.strip("[]").lower() for h in settings.allowed_hosts]
    hosts = [f"[{h}]" if ":" in h else h for h in hosts]
    manager = StreamableHTTPSessionManager(gateway.server, json_response=True, stateless=True,
        max_request_body_size=settings.max_body_bytes,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{host}:*" for host in hosts] + hosts,
            allowed_origins=settings.allowed_origins))

    @asynccontextmanager
    async def lifespan(app):
        if gateway.code_surface:
            gateway.code_surface.executor.preflight()
        if gateway.workflows:
            gateway.workflows.journal.open()
        try:
            for pool in gateway.pools.values():
                pool.start()
            async with manager.run():
                yield
        finally:
            await asyncio.gather(*(pool.close() for pool in gateway.pools.values()))
            if gateway.workflows:
                gateway.workflows.journal.close()

    async def health(request):
        return JSONResponse({"status": "alive", "service": "mcp_gateway"})

    async def ready(request):
        async def check(name, pool):
            try:
                if not getattr(pool, "discovery_requires_credential", True):
                    await pool.list_tools(settings.discovery_ttl_seconds)
                    return name, "configured"
                credential, cache_key = await gateway.exchange.credential(pool.config, subject_token.get())
                await pool.list_tools(settings.discovery_ttl_seconds, credential=credential, cache_key=cache_key)
                return name, "ready"
            except BackendUnavailable:
                return name, "unavailable"
        statuses = dict(await asyncio.gather(*(check(n, p) for n, p in gateway.pools.items())))
        healthy = all(s in {"ready", "configured"} for s in statuses.values())
        return JSONResponse({"status": "ready" if healthy else "degraded", "backends": statuses}, status_code=200 if healthy else 503)

    async def metrics(request):
        return Response(generate_latest(gateway.registry), media_type="text/plain; version=0.0.4")

    async def code_request(request):
        from .code_mode import CodeModeError
        try:
            arguments = await request.json()
            result = await gateway.code_surface.run(request.path_params["operation"], arguments)
            return JSONResponse(result)
        except (ValueError, CodeModeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def workflow_request(request):
        from .code_mode import CodeModeError
        workflows = gateway.workflows
        workflow_id = request.path_params.get("workflow_id")
        try:
            if request.method == "GET":
                return JSONResponse(workflows.snapshot(workflow_id))
            arguments = await request.json()
            action = request.path_params.get("action")
            if workflow_id is None:
                result = await workflows.start(arguments)
            elif action == "resume":
                if arguments != {}:
                    raise CodeModeError("Resume accepts an empty object")
                result = await workflows.resume(workflow_id)
            elif action in {"approve", "reject", "resolve"}:
                result = await workflows.decide(workflow_id, action, arguments)
            else:
                raise CodeModeError("Unknown workflow action")
            return JSONResponse(result)
        except (ValueError, CodeModeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # An ASGI callable mounted through Route avoids redirecting /mcp to /mcp/.
    class MCPRoute:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    routes = [Route("/healthz", health), Route("/readyz", ready), Route("/metrics", metrics),
              Route("/mcp", MCPRoute(), methods=["POST", "GET", "DELETE"])]
    if gateway.code_surface:
        if gateway.workflows:
            routes += [Route("/code/workflows", workflow_request, methods=["POST"]),
                       Route("/code/workflows/{workflow_id:str}", workflow_request, methods=["GET"]),
                       Route("/code/workflows/{workflow_id:str}/{action:str}", workflow_request, methods=["POST"])]
        routes += [Route("/code/{operation:str}", code_request, methods=["POST"])]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.gateway = gateway
    return Guard(app, gateway, auth)
