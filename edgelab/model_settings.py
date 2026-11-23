"""Discover adviser choices and run one explicit, synthetic compatibility probe."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from time import monotonic
from types import MappingProxyType
from typing import NotRequired, TypedDict

from .providers import LMStudio, ProviderError, ROLES, Transport, _parse_json, get_json, lm_url, post_json


OPENROUTER_BASE = "https://openrouter.ai/api/v1"
LM_STUDIO_DEFAULT = "http://127.0.0.1:1234/v1"
CHAT_PATH = "/api/v1/chat/completions"
PRICE_MULTIPLIERS = {"prompt": 1_000_000, "completion": 1_000_000, "request": 1, "image": 1}
ROUTE_ID = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?")
GET_TIMEOUT = 5
GetTransport = Callable[[str, dict, float], dict]


class ModelCatalog(TypedDict):
    provider: str
    status: str
    models: list[dict]
    loaded_ids: NotRequired[list[str]]
    downloaded: NotRequired[list[dict]]
    models_directory: NotRequired[str | None]
    free_only: NotRequired[bool]


def _local_path(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw or not raw.isprintable():
        return None
    try:
        path = Path(raw).expanduser()
        if path.is_absolute() and not str(path).startswith(("\\\\", "//")):
            return path
    except RuntimeError:
        pass
    return None


def _models_directory(settings: Mapping[str, str], home: Path) -> Path | None:
    configured = settings.get("LM_STUDIO_MODELS_DIR")
    if configured:
        path = _local_path(configured)
        if path is None:
            raise ValueError("LM Studio model directory must be an absolute local path")
        return path
    try:
        with (home / ".lmstudio/settings.json").open("rb") as source:
            raw = source.read(256 * 1024 + 1)
        if len(raw) <= 256 * 1024:
            desktop = _parse_json(raw)
            path = _local_path(desktop.get("downloadsFolder")) if isinstance(desktop, dict) else None
            if path is not None and path.is_dir():
                return path
    except (OSError, RecursionError, ValueError):
        pass
    return next((path for path in (home / ".lmstudio/models", home / ".cache/lm-studio/models")
                 if path.is_dir()), None)


def _downloaded_models(root: Path | None) -> list[dict]:
    if root is None:
        return []
    rows = []
    # Windows GGUF stores use publisher/model/file. File metadata suffices.
    for file in sorted(root.glob("*/*/*.gguf")):
        try:
            if not file.is_file():
                continue
            size = file.stat().st_size
        except OSError:
            continue
        if size > 0:
            rows.append({"file": file.relative_to(root).as_posix(), "model_key": file.parent.relative_to(root).as_posix(),
                         "size_bytes": size})
    return rows


def _bearer(key: str) -> dict[str, str]:
    if key and not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,2}", key):
        raise ValueError("Invalid provider API token format")
    return {"Authorization": "Bearer " + key} if key else {}


def _usable_text(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable() and bool(value.strip())


def _local_models(reply: dict) -> list[dict]:
    records = reply.get("models")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ProviderError("Invalid LM Studio model list")
    rows = []
    for row in records:
        if row.get("type") != "llm":
            continue
        model_key = row.get("key")
        instances = row.get("loaded_instances")
        if not _usable_text(model_key) or not isinstance(instances, list) or not all(
            isinstance(instance, dict) for instance in instances
        ):
            raise ProviderError("Invalid LM Studio model metadata")
        name = row.get("display_name")
        name = name if _usable_text(name) else model_key
        for model_id in [instance.get("id") for instance in instances] or [model_key]:
            if not _usable_text(model_id):
                raise ProviderError("Invalid LM Studio model instance")
            rows.append({"id": model_id, "name": name, "model_key": model_key, "loaded": bool(instances)})
    if len({row["id"] for row in rows if row["loaded"]}) != sum(row["loaded"] for row in rows):
        raise ProviderError("Ambiguous LM Studio loaded model ID")
    return sorted(rows, key=lambda row: (not row["loaded"], row["id"]))


def local_catalog(settings: Mapping[str, str], *, allow_provider_io: bool = False,
                  transport: GetTransport = get_json, home: Path | None = None) -> ModelCatalog:
    """Keep on-disk downloads distinct from service-authoritative loaded IDs."""
    root = _models_directory(settings, Path.home() if home is None else home)
    catalog: ModelCatalog = {"provider": "lm_studio", "status": "disabled", "models": [], "loaded_ids": [],
                             "downloaded": _downloaded_models(root), "models_directory": str(root) if root else None}
    if allow_provider_io is True:
        base = lm_url(settings.get("LM_STUDIO_BASE_URL") or LM_STUDIO_DEFAULT)
        headers = _bearer(settings.get("LM_STUDIO_API_KEY", ""))
        try:
            models = _local_models(transport(base.removesuffix("/v1") + "/api/v1/models", headers, GET_TIMEOUT))
        except ProviderError:
            catalog["status"] = "unavailable"
        else:
            catalog["status"] = "available"
            catalog["models"] = models
            catalog["loaded_ids"] = [row["id"] for row in models if row["loaded"]]
    return catalog


def _pricing(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict) or not {"prompt", "completion"} <= raw.keys():
        return None
    prices = {}
    try:
        for key, value in raw.items():
            if type(value) not in {str, int, float} or len(str(value)) > 64:
                return None
            price = Decimal(str(value))
            if not price.is_finite() or not 0 <= price <= 10**9:
                return None
            prices[key] = str(price)
    except InvalidOperation:
        return None
    return prices


def _openrouter_models(reply: dict, free_only: bool) -> list[dict]:
    records = reply.get("data")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ProviderError("Invalid OpenRouter account catalog")
    models = {}
    for row in records:
        model_id = row.get("id")
        if not _usable_text(model_id) or not ROUTE_ID.fullmatch(model_id):
            continue
        if model_id.split("/", 1)[0] == "openrouter":
            continue
        architecture = row.get("architecture")
        if not isinstance(architecture, dict) or any(
            not isinstance(architecture.get(field), list) or "text" not in architecture[field]
            for field in ("input_modalities", "output_modalities")
        ):
            continue
        endpoints = row.get("endpoints")
        if endpoints is not None and (not isinstance(endpoints, list) or CHAT_PATH not in endpoints):
            continue
        params = row.get("supported_parameters")
        if not isinstance(params, list) or "structured_outputs" not in params or "response_format" not in params:
            continue
        prices = _pricing(row.get("pricing"))
        if prices is None:
            continue
        zero_priced = all(Decimal(value) == 0 for value in prices.values())
        free = model_id.endswith(":free")
        if free != zero_priced or (free_only and not free):
            continue
        if model_id in models:
            raise ProviderError("Duplicate OpenRouter model ID")
        name, context = row.get("name"), row.get("context_length")
        models[model_id] = {"id": model_id, "name": name if _usable_text(name) else model_id, "free": free,
                            "pricing": prices, "context_length": context if type(context) is int and context > 0 else None}
    return sorted(models.values(), key=lambda row: (not row["free"], row["id"]))


def openrouter_catalog(settings: Mapping[str, str], *, allow_provider_io: bool, free_only: bool = True,
                       transport: GetTransport = get_json) -> ModelCatalog:
    """Project the account-filtered catalog. Never substitute the public list."""
    catalog: ModelCatalog = {"provider": "openrouter", "status": "disabled", "models": [], "free_only": free_only}
    if allow_provider_io is not True:
        return catalog
    key = settings.get("OPENROUTER_API_KEY", "")
    if not key:
        catalog["status"] = "not_configured"
        return catalog
    headers = _bearer(key)
    try:
        models = _openrouter_models(transport(OPENROUTER_BASE + "/models/user", headers, GET_TIMEOUT), free_only)
    except ProviderError:
        catalog["status"] = "unavailable"
    else:
        catalog["status"] = "available"
        catalog["models"] = models
    return catalog


@dataclass(frozen=True, slots=True)
class OpenRouter:
    models: Mapping[str, str]
    max_price: Mapping[str, float]
    api_key: str = field(repr=False)
    transport: Transport = field(repr=False)

    def choose(self, role: str, prompt: str, options: tuple[str, ...], timeout: float = 20) -> str:
        if role not in ROLES or role not in self.models:
            raise ValueError("No permitted OpenRouter role model")
        if not options or len(options) > 8 or len(set(options)) != len(options) or len(prompt) > 6000:
            raise ValueError("Invalid structured decision request")
        model_id = self.models[role]
        reply = self.transport(OPENROUTER_BASE + "/chat/completions", _bearer(self.api_key), {
            "model": model_id, "stream": False, "temperature": 0, "max_tokens": 128,
            "provider": {"require_parameters": True, "allow_fallbacks": False, "data_collection": "deny",
                         "zdr": True, "max_price": dict(self.max_price)},
            "messages": [
                {"role": "system", "content": "Choose one supplied label. You cannot call tools or grant permissions."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "edgelab_choice", "strict": True,
                "schema": {"type": "object", "properties": {"choice": {"type": "string", "enum": list(options)}},
                           "required": ["choice"], "additionalProperties": False},
            }},
        }, timeout)
        try:
            if reply.get("model", model_id) not in {model_id, model_id.removesuffix(":free")}:
                raise ValueError
            choice_record, = reply["choices"]
            if choice_record.get("finish_reason") != "stop":
                raise ValueError
            message = choice_record["message"]
            result = _parse_json(message["content"])
            if (message.get("tool_calls") or message.get("function_call") or not isinstance(result, dict)
                    or set(result) != {"choice"} or result["choice"] not in options):
                raise ValueError
            return result["choice"]
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ProviderError("OpenRouter did not return an allowed structured choice") from None


def openrouter_adviser(settings: Mapping[str, str], model_id: str, *, allow_provider_io: bool,
                       free_only: bool = True, catalog_transport: GetTransport = get_json,
                       transport: Transport = post_json) -> OpenRouter:
    """Check the exact route against a fresh account catalog before inference."""
    if allow_provider_io is not True:
        raise ValueError("OpenRouter access requires explicit provider I/O permission")
    catalog = openrouter_catalog(settings, allow_provider_io=True, free_only=free_only, transport=catalog_transport)
    if catalog["status"] != "available":
        raise ProviderError("OpenRouter account catalog is unavailable")
    model = next((row for row in catalog["models"] if row["id"] == model_id), None)
    if model is None:
        raise ProviderError("OpenRouter model is not eligible in the account catalog")
    # OpenRouter's prompt and completion routing caps are per million tokens.
    max_price = {key: float(Decimal(model["pricing"].get(key, "0")) * multiplier)
                 for key, multiplier in PRICE_MULTIPLIERS.items()}
    return OpenRouter(MappingProxyType(dict.fromkeys(ROLES, model_id)), MappingProxyType(max_price),
                      settings["OPENROUTER_API_KEY"], transport)


def test_model(settings: Mapping[str, str], provider: str, model_id: str, *, allow_provider_io: bool,
               free_only: bool = True, catalog_transport: GetTransport = get_json,
               transport: Transport = post_json, home: Path | None = None) -> dict:
    """Send one fixed synthetic choice. No research, account action, or model loading."""
    if allow_provider_io is not True:
        raise ValueError("A model test requires explicit provider I/O permission")
    if provider == "lm_studio":
        catalog = local_catalog(settings, allow_provider_io=True, transport=catalog_transport, home=home)
        if model_id not in catalog["loaded_ids"]:
            raise ProviderError("LM Studio model is not currently loaded")
        adviser = LMStudio(settings.get("LM_STUDIO_BASE_URL") or LM_STUDIO_DEFAULT,
                           {"planner": model_id}, settings.get("LM_STUDIO_API_KEY", ""), transport)
    elif provider == "openrouter":
        adviser = openrouter_adviser(settings, model_id, allow_provider_io=True, free_only=free_only,
                                     catalog_transport=catalog_transport, transport=transport)
    else:
        raise ValueError("Unknown model test provider")
    started = monotonic()
    choice = adviser.choose("planner", "Synthetic check: both numbers are 7. Choose match if equal, else mismatch.",
                            ("match", "mismatch"), timeout=15)
    return {"provider": provider, "model_id": model_id, "status": "passed" if choice == "match" else "failed",
            "choice": choice, "elapsed_ms": round(1000 * (monotonic() - started)),
            "scope": "synthetic structured-choice check"}
