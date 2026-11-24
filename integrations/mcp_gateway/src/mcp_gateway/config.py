from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Token(StrictModel):
    env: str
    subject: str
    roles: list[str] = Field(min_length=1)


class Auth(StrictModel):
    mode: str = "local_tokens"
    tokens: list[Token] = []
    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "local_tokens" and not self.tokens:
            raise ValueError("local_tokens requires at least one configured token")
        if self.mode == "jwt":
            if not all((self.issuer, self.audience, self.jwks_url)):
                raise ValueError("JWT mode requires issuer, audience and jwks_url")
            if urlparse(self.jwks_url).scheme != "https":
                raise ValueError("JWKS must use HTTPS")
        elif self.mode != "local_tokens":
            raise ValueError("Unsupported auth mode")
        return self


class Backend(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    kind: Literal["mcp", "openapi"] = "mcp"
    url: str
    spec_file: str | None = None
    max_response_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    roles: list[str] = Field(min_length=1)
    allowed_tools: list[str] = Field(min_length=1)
    read_only_tools: list[str] = []
    durable_only_tools: dict[str, str] = Field(default_factory=dict)
    tool_roles: dict[str, list[str]] = {}
    token_env: str | None = None
    exchange: "Exchange | None" = None
    pool_size: int = Field(default=2, ge=1, le=32)
    queue_size: int = Field(default=32, ge=1, le=10000)
    timeout_seconds: float = Field(default=10, gt=0, le=300)
    failure_threshold: int = Field(default=3, ge=1)
    recovery_seconds: float = Field(default=15, gt=0)

    @model_validator(mode="after")
    def validate_url(self):
        parsed = urlparse(self.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Backend URLs cannot contain credentials, queries or fragments")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError("Use HTTPS for non-loopback backends")
        if not parsed.hostname:
            raise ValueError("Backend URL requires a hostname")
        try:
            parsed.port
        except ValueError:
            raise ValueError("Backend URL contains an invalid port") from None
        if self.kind == "openapi" and not self.spec_file:
            raise ValueError("OpenAPI connectors require a local spec_file")
        if self.kind == "mcp" and self.spec_file:
            raise ValueError("spec_file is only valid for OpenAPI connectors")
        if self.token_env and self.exchange:
            raise ValueError("Choose a service credential or token exchange, not both")
        read_only = set(self.read_only_tools)
        if not read_only.issubset(self.allowed_tools):
            raise ValueError("read_only_tools must be a subset of allowed_tools")
        if (not set(self.durable_only_tools).issubset(set(self.allowed_tools) - read_only)
                or not set(self.durable_only_tools.values()).issubset(read_only)):
            raise ValueError("durable_only_tools must map allowed writes to declared read-only receipt tools")
        return self


class Exchange(StrictModel):
    token_url: str
    audience: str
    scopes: list[str] = []
    client_id: str
    client_secret_env: str

    @model_validator(mode="after")
    def secure_endpoint(self):
        parsed = urlparse(self.token_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Token exchange endpoint must use HTTPS and contain no credentials")
        return self


Backend.model_rebuild()


class CodeLimits(StrictModel):
    timeout_seconds: float = Field(default=10, ge=0.1, le=60)
    memory_mb: int = Field(default=32, ge=8, le=128)
    max_calls: int = Field(default=32, ge=1, le=256)
    max_parallel_calls: int = Field(default=8, ge=1, le=32)
    max_executions: int = Field(default=4, ge=1, le=16)
    max_code_bytes: int = Field(default=32768, ge=1024, le=32768)
    max_result_bytes: int = Field(default=65536, ge=1024, le=65536)
    max_frame_bytes: int = Field(default=131072, ge=131072, le=1048576)
    max_catalog_bytes: int = Field(default=2097152, ge=65536, le=4194304)


class DurableSettings(StrictModel):
    database: str | None = None
    approval_seconds: int = Field(default=300, ge=10, le=86400)
    max_workflows: int = Field(default=1000, ge=1, le=100000)


class Settings(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    allowed_hosts: list[str] = ["127.0.0.1", "localhost"]
    allowed_origins: list[str] = []
    max_body_bytes: int = Field(default=1048576, ge=1024, le=4194304)
    requests_per_minute: int = Field(default=600, ge=1)
    discovery_ttl_seconds: float = Field(default=30, gt=0)
    surface: Literal["classic", "code"] = "classic"
    code_mode: CodeLimits = Field(default_factory=CodeLimits)
    durable: DurableSettings = Field(default_factory=DurableSettings)
    auth: Auth
    backends: list[Backend] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_names(self):
        names = [b.name for b in self.backends]
        if len(set(names)) != len(names):
            raise ValueError("Backend names must be unique")
        return self


def load_config(path: str | Path) -> Settings:
    path = Path(path).resolve()
    settings = Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    for backend in settings.backends:
        if backend.spec_file:
            spec = Path(backend.spec_file)
            backend.spec_file = str(spec if spec.is_absolute() else (path.parent / spec).resolve())
    if settings.durable.database:
        database = Path(settings.durable.database)
        settings.durable.database = str(database if database.is_absolute() else (path.parent / database).resolve())
    return settings
