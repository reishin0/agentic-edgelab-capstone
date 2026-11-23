"""Opt-in provider adapters. No endpoint is contacted at import or construction."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.client import HTTPException
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ProviderError(RuntimeError):
    pass


class ChoiceAdviser(Protocol):
    @property
    def models(self) -> Mapping[str, str]: ...

    def choose(self, role: str, prompt: str, options: tuple[str, ...], timeout: float = 20) -> str: ...


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON member")
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _parse_json(data: str | bytes) -> object:
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


ROLES = ("planner", "critic", "reporter")
LM_STUDIO_PLANNER_PROFILE_KEY = "LM_STUDIO_PLANNER_PROFILE"
ORNITH_PLANNER_PROFILE = "ornith"
ORNITH_PLANNER_MODEL = "ornith-1.5-35b-a3b-q4-k-m"
ORNITH_MAX_OUTPUT_TOKENS = 128
KEYS = {
    "LM_STUDIO_BASE_URL", "LM_STUDIO_API_KEY", "PINECONE_API_KEY", "PINECONE_INDEX_HOST",
    "LM_STUDIO_MODELS_DIR", "LM_STUDIO_AUTO_USE", LM_STUDIO_PLANNER_PROFILE_KEY, "OPENROUTER_API_KEY",
    "EDGELAB_NEWS_KEY", "EDGELAB_NEWS_SECRET",
    *(f"LM_STUDIO_{role.upper()}_MODEL" for role in ROLES),
}


def read_env(path: Path) -> dict[str, str]:
    """Read only the selected project file, with no shell or variable expansion."""
    settings = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in KEYS or key in settings:
            raise ValueError(f"Invalid or duplicate .env setting on line {number}")
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unbalanced .env quotes on line {number}")
            value = value[1:-1]
        if not value.isprintable() and value:
            raise ValueError(f"Invalid .env value on line {number}")
        settings[key] = value
    return settings


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url: str, headers: dict, timeout: float) -> dict:
    return _request_json(Request(url, headers=headers, method="GET"), timeout)


def post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    request = Request(url, data=json.dumps(payload, allow_nan=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", **headers}, method="POST")
    return _request_json(request, timeout)


def _request_json(request: Request, timeout: float) -> dict:
    # Never send keys through an environment proxy or across a redirect.
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ProviderError("Provider response exceeded the byte limit")
        decoded = _parse_json(body)
        if not isinstance(decoded, dict):
            raise ProviderError("Expected a JSON object from provider")
        return decoded
    except HTTPError as error:
        error.close()
        raise ProviderError(f"Provider returned HTTP {error.code}") from None
    except (HTTPException, OSError, RecursionError, ValueError):
        raise ProviderError("Provider connection or response failed") from None


Transport = Callable[[str, dict, dict, float], dict]


def lm_url(value: str) -> str:
    message = "LM Studio requires an explicit loopback HTTP URL ending in /v1"
    try:
        parsed = urlsplit(value.rstrip("/"))
        port = parsed.port
    except ValueError:
        raise ValueError(message) from None
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path != "/v1" or parsed.query or parsed.fragment or not port):
        raise ValueError(message)
    return f"http://{parsed.netloc}/v1"


def pinecone_url(value: str) -> str:
    message = "Pinecone requires the HTTPS data-plane index host"
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value}")
    except ValueError:
        raise ValueError(message) from None
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or not re.fullmatch(r"[a-z0-9.-]+\.svc\.[a-z0-9.-]+\.pinecone\.io", host)
            or parsed.netloc.lower() != host or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError(message)
    return f"https://{host}"


def _allowed_choice(content: str | bytes, options: tuple[str, ...]) -> str:
    result = _parse_json(content)
    if not isinstance(result, dict) or set(result) != {"choice"}:
        raise ValueError
    choice = result["choice"]
    if not isinstance(choice, str) or choice not in options:
        raise ValueError
    return choice


@dataclass(frozen=True, slots=True)
class LMStudio:
    base_url: str
    models: Mapping[str, str]
    api_key: str = field(default="", repr=False)
    transport: Transport = field(default=post_json, repr=False)
    planner_profile: str = field(default="", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", lm_url(self.base_url))
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))

    @classmethod
    def from_settings(cls, settings: Mapping[str, str], *, planner_model: str | None = None,
                      transport: Transport = post_json) -> "LMStudio":
        models = {
            role: settings.get(f"LM_STUDIO_{role.upper()}_MODEL", "") for role in ROLES
        }
        profile = settings.get(LM_STUDIO_PLANNER_PROFILE_KEY, "")
        if profile and (profile != ORNITH_PLANNER_PROFILE or models["planner"] != ORNITH_PLANNER_MODEL):
            raise ValueError("LM Studio Planner profile requires the pinned Ornith model ID")
        if planner_model is not None:
            models["planner"] = planner_model
            if planner_model != ORNITH_PLANNER_MODEL:
                profile = ""
        adviser = cls(settings.get("LM_STUDIO_BASE_URL") or "http://127.0.0.1:1234/v1", models,
                      settings.get("LM_STUDIO_API_KEY", ""), transport)
        object.__setattr__(adviser, "planner_profile", profile)
        return adviser

    def choose(self, role: str, prompt: str, options: tuple[str, ...], timeout: float = 20) -> str:
        if role not in ROLES or not self.models.get(role):
            raise ProviderError(f"No model ID configured for role {role if role in ROLES else 'unknown'}")
        if not options or len(options) > 8 or len(set(options)) != len(options) or len(prompt) > 6000:
            raise ValueError("Invalid structured decision request")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if role == "planner" and self.planner_profile == ORNITH_PLANNER_PROFILE:
            return self._choose_ornith(prompt, options, headers, timeout)
        reply = self.transport(self.base_url + "/chat/completions", headers, {
            "model": self.models[role], "stream": False, "temperature": 0, "max_tokens": 128,
            "messages": [
                {"role": "system", "content": "Choose one of the supplied labels. You cannot call tools or grant permissions."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "edgelab_choice", "strict": True,
                "schema": {"type": "object", "properties": {"choice": {"type": "string", "enum": list(options)}},
                           "required": ["choice"], "additionalProperties": False},
            }},
        }, timeout)
        try:
            choice_record, = reply["choices"]
            if choice_record.get("finish_reason", "stop") != "stop":
                raise ValueError
            message = choice_record["message"]
            if message.get("tool_calls"):
                raise ValueError
            return _allowed_choice(message["content"], options)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ProviderError("LM Studio did not return an allowed structured choice") from None

    def _choose_ornith(self, prompt: str, options: tuple[str, ...], headers: dict, timeout: float) -> str:
        instructions = (
            'Return exactly one JSON object with a single string property named "choice". '
            'Omit explanations, Markdown, and tool calls. You cannot grant permissions. '
            f'Choose exactly one label from this JSON array: {json.dumps(options)}.'
        )
        reply = self.transport(self.base_url.removesuffix("/v1") + "/api/v1/chat", headers, {
            "model": ORNITH_PLANNER_MODEL,
            "system_prompt": instructions,
            "input": prompt,
            "reasoning": "off",
            "store": False,
            "stream": False,
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "max_output_tokens": ORNITH_MAX_OUTPUT_TOKENS,
        }, timeout)
        try:
            if reply["model_instance_id"] != ORNITH_PLANNER_MODEL or "response_id" in reply:
                raise ValueError
            output = reply["output"]
            if not isinstance(output, list) or len(output) != 1:
                raise ValueError
            message = output[0]
            if (not isinstance(message, dict) or set(message) != {"type", "content"}
                    or message["type"] != "message" or not isinstance(message["content"], str)):
                raise ValueError
            stats = reply["stats"]
            reasoning_tokens = stats["reasoning_output_tokens"]
            total_tokens = stats["total_output_tokens"]
            # Native chat has no finish reason. Treat a full token budget as incomplete.
            if (type(reasoning_tokens) is not int or reasoning_tokens != 0
                    or type(total_tokens) is not int or not 0 < total_tokens < ORNITH_MAX_OUTPUT_TOKENS):
                raise ValueError
            return _allowed_choice(message["content"], options)
        except (KeyError, RecursionError, TypeError, ValueError):
            raise ProviderError("Ornith did not return one allowed Planner choice") from None
