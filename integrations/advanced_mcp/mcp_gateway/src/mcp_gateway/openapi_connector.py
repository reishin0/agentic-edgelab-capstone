"""Native JSON REST capabilities derived from a locally reviewed OpenAPI document.

This deliberately supports a bounded OpenAPI 3.0/3.1 subset. No remote refs,
spec-driven origins, arbitrary request URLs, cookies, redirects, or automatic retries.
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
import jsonschema
import yaml
from mcp import types
from referencing import Registry

from .backend import BackendUnavailable, OperationNotApplied
from .config import Backend

REGISTRY = Registry()
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
FORBIDDEN_HEADERS = {"authorization", "proxy-authorization", "host", "cookie", "connection",
    "transfer-encoding", "content-length", "content-type", "forwarded", "x-forwarded-host",
    "x-forwarded-for", "x-forwarded-proto", "upgrade", "te", "trailer"}


class OpenAPIConfigError(ValueError):
    pass


class LocalReferences:
    def __init__(self, document):
        self.document, self.remaining = document, 100000

    def resolve(self, value, chain=(), depth=0):
        self.remaining -= 1
        if self.remaining < 0 or depth > 40:
            raise OpenAPIConfigError("OpenAPI reference expansion limit exceeded")
        if isinstance(value, list):
            return [self.resolve(item, chain, depth + 1) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise OpenAPIConfigError("Only local OpenAPI references are supported")
            if ref in chain:
                raise OpenAPIConfigError("Cyclic OpenAPI references are not supported")
            target = self.document
            try:
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                raise OpenAPIConfigError("Unresolved local OpenAPI reference") from None
            resolved = self.resolve(target, (*chain, ref), depth + 1)
            siblings = self.resolve({k: v for k, v in value.items() if k != "$ref"}, chain, depth + 1)
            if siblings:
                if not isinstance(resolved, dict):
                    raise OpenAPIConfigError("Unsupported reference siblings")
                resolved = {**resolved, **siblings}
            return resolved
        return {key: self.resolve(item, chain, depth + 1) for key, item in value.items()}


def normalize_schema(schema):
    """Convert OAS 3.0 nullable/exclusive bounds without rewriting property-name maps."""
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        raise OpenAPIConfigError("Operation schema must be an object or boolean")
    result = copy.deepcopy(schema)
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        if key in result:
            result[key] = {name: normalize_schema(value) for name, value in result[key].items()}
    for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else", "propertyNames"):
        if key in result:
            result[key] = normalize_schema(result[key])
    for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
        if key in result:
            result[key] = [normalize_schema(value) for value in result[key]]
    for bound, ordinary in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
        if isinstance(result.get(bound), bool):
            exclusive = result.pop(bound)
            if exclusive and ordinary in result:
                result[bound] = result.pop(ordinary)
    if result.pop("nullable", False) is True:
        result = {"anyOf": [result, {"type": "null"}]}
    return result


def validate_path(path):
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise OpenAPIConfigError("Operation paths must be origin-relative")
    if any(c in path for c in ("\\", "?", "#", "%")) or any(ord(c) < 32 for c in path):
        raise OpenAPIConfigError("Unsafe OpenAPI operation path")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise OpenAPIConfigError("OpenAPI paths cannot contain traversal segments")


@dataclass
class Operation:
    name: str
    method: str
    path: str
    parameters: list[dict]
    input_schema: dict
    output_schema: dict | bool | None
    description: str
    has_body: bool
    draft_receipt: bool = False
    not_saved_schema: dict | bool | None = None

    def tool(self):
        output = self.output_schema
        return types.Tool(name=self.name, description=self.description, inputSchema=self.input_schema,
            outputSchema=output if isinstance(output, dict) and output.get("type") == "object" else None)


def compile_operations(config: Backend):
    path = Path(config.spec_file)
    if path.stat().st_size > 2097152:
        raise OpenAPIConfigError("OpenAPI document exceeds 2 MiB")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith(("3.0.", "3.1.")):
        raise OpenAPIConfigError("A local OpenAPI 3.0 or 3.1 document is required")
    resolver, operations = LocalReferences(document), {}
    receipt_tools = set(config.durable_only_tools.values())
    for api_path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, dict):
            raise OpenAPIConfigError("Invalid OpenAPI path item")
        if "$ref" in path_item:
            path_item = resolver.resolve(path_item)
        for method, op in path_item.items():
            if method not in METHODS or not isinstance(op, dict) or op.get("operationId") not in config.allowed_tools:
                continue
            name = op["operationId"]
            if name in operations:
                raise OpenAPIConfigError("Selected operationId values must be unique")
            validate_path(api_path)
            parameters = {}
            for source in (path_item.get("parameters", []), op.get("parameters", [])):
                for raw in source:
                    parameter = resolver.resolve(raw)
                    parameters[(parameter["in"], parameter["name"])] = parameter
            fields, required, names = {}, [], set()
            for parameter in parameters.values():
                location, key = parameter["in"], parameter["name"]
                if location not in {"path", "query", "header"}:
                    raise OpenAPIConfigError("Only path, query and header parameters are supported")
                if key in names or key == "body":
                    raise OpenAPIConfigError("Ambiguous OpenAPI parameter names")
                names.add(key)
                if location == "header" and (key.lower() in FORBIDDEN_HEADERS or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)):
                    raise OpenAPIConfigError("Credential and routing headers must remain host-managed")
                style = parameter.get("style", "form" if location == "query" else "simple")
                if style != ("form" if location == "query" else "simple"):
                    raise OpenAPIConfigError("Unsupported OpenAPI parameter serialization style")
                if "schema" not in parameter:
                    raise OpenAPIConfigError("Parameters must declare a schema")
                schema = normalize_schema(parameter["schema"])
                if isinstance(schema, dict) and schema.get("type") == "object":
                    raise OpenAPIConfigError("Object-valued operation parameters are not supported")
                fields[key] = schema
                if location == "path" or parameter.get("required", False):
                    required.append(key)
            placeholders = re.findall(r"\{([^{}]+)\}", api_path)
            declared = {p["name"] for p in parameters.values() if p["in"] == "path"}
            if set(placeholders) != declared or any(c in re.sub(r"\{[^{}]+\}", "", api_path) for c in "{}"):
                raise OpenAPIConfigError("Path template parameters do not match their declarations")
            has_body = "requestBody" in op
            if has_body:
                body = resolver.resolve(op["requestBody"])
                media = body.get("content", {}).get("application/json")
                if not isinstance(media, dict) or "schema" not in media:
                    raise OpenAPIConfigError("Only application/json request bodies are supported")
                fields["body"] = normalize_schema(media["schema"])
                if body.get("required", False):
                    required.append("body")
            input_schema = {"type": "object", "properties": fields, "required": required, "additionalProperties": False}
            output_schema = None
            for status, response in sorted(op.get("responses", {}).items(), key=lambda item: str(item[0])):
                if not str(status).startswith("2"):
                    continue
                response = resolver.resolve(response)
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                if schema is not None:
                    output_schema = normalize_schema(schema)
                break
            draft_receipt = op.get("x-edgelab-draft-receipt") is True
            not_saved_schema = None
            if draft_receipt:
                if (method != "post" or "body" not in required or not config.token_env
                        or not isinstance(output_schema, dict) or output_schema.get("type") != "object"
                        or (name not in config.durable_only_tools and name not in receipt_tools)):
                    raise OpenAPIConfigError("Draft receipts require an authenticated durable write and receipt pair")
                if name in config.durable_only_tools:
                    conflict = resolver.resolve(op.get("responses", {}).get("409", {}))
                    schema = conflict.get("content", {}).get("application/json", {}).get("schema")
                    if not isinstance(schema, dict) or schema.get("type") != "object":
                        raise OpenAPIConfigError("A bound draft write must declare its 409 refusal schema")
                    not_saved_schema = normalize_schema(schema)
            try:
                jsonschema.Draft202012Validator.check_schema(input_schema)
                if output_schema is not None:
                    jsonschema.Draft202012Validator.check_schema(output_schema)
                if not_saved_schema is not None:
                    jsonschema.Draft202012Validator.check_schema(not_saved_schema)
            except jsonschema.SchemaError:
                raise OpenAPIConfigError("Selected operation contains an invalid JSON Schema") from None
            operations[name] = Operation(name, method.upper(), api_path, list(parameters.values()), input_schema,
                output_schema, str(op.get("description") or op.get("summary") or name), has_body,
                draft_receipt, not_saved_schema)
    if set(operations) != set(config.allowed_tools):
        raise OpenAPIConfigError("Every allowed tool must match a supported, unique operationId")
    for write, receipt in config.durable_only_tools.items():
        if operations[write].draft_receipt != operations[receipt].draft_receipt:
            raise OpenAPIConfigError("Both operations in a draft receipt pair must declare the receipt contract")
    return operations


def unique_members(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON member")
    return value


def verify_draft_response(result, original, schema, *, not_saved):
    try:
        jsonschema.validate(result, schema, registry=REGISTRY)
    except jsonschema.ValidationError:
        raise BackendUnavailable("Draft response did not match its schema") from None
    if not isinstance(original, dict) or not isinstance(result, dict) or any(
        not isinstance(result.get(key), str) or result[key] != original.get(key)
        for key in ("request_id", "preview_sha256")
    ):
        raise BackendUnavailable("Draft response did not match the request binding")
    if not_saved:
        if result.get("status") != "not_saved" or set(result) != {"error", "status", "request_id", "preview_sha256"}:
            raise BackendUnavailable("Draft refusal could not be verified")
        return
    version = result.get("schema_version")
    if type(version) is not int or version != 1 or result.get("status") != "saved":
        raise BackendUnavailable("Draft receipt version or status could not be verified")
    fields = {key: value for key, value in result.items() if key != "receipt_sha256"}
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if result.get("receipt_sha256") != hashlib.sha256(encoded).hexdigest():
        raise BackendUnavailable("Draft receipt checksum did not match")


def scalar(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        raise ValueError("Nested operation parameter serialization is unsupported")
    return str(value)


class OpenAPIConnector:
    discovery_requires_credential = False

    def __init__(self, config: Backend, transport: httpx.AsyncBaseTransport | None = None):
        self.config, self.transport = config, transport
        self.operations = compile_operations(config)
        self.client = None
        self.permits = asyncio.Semaphore(config.pool_size)
        self.admitted, self.failures, self.open_until = 0, 0, 0.0

    def start(self):
        if self.client is not None:
            raise RuntimeError("Connector is already running")
        self.client = httpx.AsyncClient(transport=self.transport, follow_redirects=False, trust_env=False,
            timeout=self.config.timeout_seconds, limits=httpx.Limits(max_connections=self.config.pool_size,
            max_keepalive_connections=self.config.pool_size))

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def list_tools(self, ttl, *, credential=None, cache_key="service"):
        return [operation.tool() for operation in self.operations.values()]

    def describe(self, name):
        operation = self.operations[name]
        return {"transport": "openapi", "method": operation.method, "httpPath": operation.path,
                "outputSchema": operation.output_schema or {}}

    @staticmethod
    def error(message):
        return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=message)])

    async def request(self, operation, *args, credential=None):
        if operation != "call_tool" or len(args) != 2 or args[0] not in self.operations or not isinstance(args[1], dict):
            raise BackendUnavailable("Unknown OpenAPI capability")
        if self.client is None:
            raise BackendUnavailable("OpenAPI connector is not running")
        if time.monotonic() < self.open_until:
            raise BackendUnavailable("Backend circuit is open")
        if self.admitted >= self.config.pool_size + self.config.queue_size:
            raise BackendUnavailable("Backend queue is full")
        method, values = self.operations[args[0]], args[1]
        try:
            jsonschema.validate(values, method.input_schema, registry=REGISTRY)
        except jsonschema.ValidationError:
            return self.error("Arguments do not match the operation input schema")
        self.admitted += 1
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self.permits:
                    if time.monotonic() < self.open_until:
                        raise BackendUnavailable("Backend circuit is open")
                    return await self._request(method, values, credential)
        except (httpx.HTTPError, TimeoutError):
            self.failures += 1
            if self.failures >= self.config.failure_threshold:
                self.open_until = time.monotonic() + self.config.recovery_seconds
            raise BackendUnavailable("REST backend unavailable; execution outcome may be unknown") from None
        except (ValueError, UnicodeError):
            return self.error("Invalid REST parameters or JSON response")
        finally:
            self.admitted -= 1

    async def _request(self, operation, values, credential):
        path, query, headers = operation.path, [], {"Accept": "application/json"}
        for parameter in operation.parameters:
            name, location = parameter["name"], parameter["in"]
            if name not in values:
                continue
            value = values[name]
            if location == "path":
                text = scalar(value)
                if not text or text in {".", ".."} or any(c in text for c in ("/", "\\", "%")) or any(ord(c) < 32 for c in text):
                    raise ValueError("Unsafe path segment")
                path = path.replace("{" + name + "}", quote(text, safe=""))
            elif location == "query":
                if isinstance(value, list):
                    parts = [scalar(item) for item in value]
                    if parameter.get("explode", True):
                        query.extend((name, part) for part in parts)
                    else:
                        query.append((name, ",".join(parts)))
                else:
                    query.append((name, scalar(value)))
            else:
                text = scalar(value)
                if any(ord(c) < 32 for c in text):
                    raise ValueError("Invalid header value")
                headers[name] = text
        if credential is None and self.config.token_env:
            credential = os.environ.get(self.config.token_env)
            if not credential:
                raise BackendUnavailable("Backend credential unavailable")
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        url = self.config.url.rstrip("/") + path
        kwargs = {"params": query, "headers": headers}
        if operation.has_body and "body" in values:
            kwargs["content"] = json.dumps(values["body"], allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = self.client.build_request(operation.method, url, **kwargs)
        request.headers.pop("cookie", None)  # Never reuse a different principal's HTTP cookie session.
        response = await self.client.send(request, stream=True)
        try:
            if response.status_code >= 500:
                raise httpx.HTTPStatusError("REST service error", request=request, response=response)
            not_saved = (response.status_code == 409 and operation.not_saved_schema is not None
                         and credential is not None)
            if not 200 <= response.status_code < 300 and not not_saved:
                self.failures = 0
                return self.error(f"REST operation returned HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                body.extend(chunk)
                if len(body) > self.config.max_response_bytes:
                    raise BackendUnavailable("REST response exceeds the configured size limit")
            media = response.headers.get("content-type", "").split(";")[0].lower()
            if body and media != "application/json" and not media.endswith("+json"):
                return self.error("REST operation returned an unsupported response format")
            if operation.draft_receipt:
                try:
                    result = json.loads(body, object_pairs_hook=unique_members)
                except (UnicodeError, ValueError, RecursionError):
                    raise BackendUnavailable("Draft response was not valid JSON") from None
                verify_draft_response(result, values["body"],
                                      operation.not_saved_schema if not_saved else operation.output_schema,
                                      not_saved=not_saved)
            else:
                result = json.loads(body) if body else None
            self.failures, self.open_until = 0, 0.0
            if not_saved:
                raise OperationNotApplied("Backend confirmed the approved write was not applied")
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, allow_nan=False))],
                structuredContent=result if isinstance(result, dict) else None)
        finally:
            self.client.cookies.clear()
            await response.aclose()
