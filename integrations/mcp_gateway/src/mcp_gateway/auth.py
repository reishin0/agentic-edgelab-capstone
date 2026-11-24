import asyncio
import hmac
import os
from contextvars import ContextVar
from dataclasses import dataclass

import jwt

from .config import Auth, Backend


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]


principal: ContextVar[Principal | None] = ContextVar("principal", default=None)
subject_token: ContextVar[str | None] = ContextVar("subject_token", default=None)


def authorized(user: Principal, backend: Backend, tool: str) -> bool:
    return (bool(user.roles.intersection(backend.roles))
            and tool in backend.allowed_tools
            and bool(user.roles.intersection(backend.tool_roles.get(tool, backend.roles))))


class Authenticator:
    def __init__(self, config: Auth):
        self.config = config
        self.tokens = []
        for item in config.tokens if config.mode == "local_tokens" else []:
            value = os.environ.get(item.env, "")
            if len(value) < 24:
                raise ValueError(f"Set {item.env} to a random token of at least 24 characters")
            if any(hmac.compare_digest(value, secret) for secret, _ in self.tokens):
                raise ValueError("Each principal requires a distinct token")
            self.tokens.append((value, Principal(item.subject, frozenset(item.roles))))
        # The optional per-key LRU has no expiry and can retain removed signing keys.
        # Use only the bounded JWKS-set cache so rotations/removals take effect on refresh.
        self.jwks = jwt.PyJWKClient(config.jwks_url, cache_keys=False, lifespan=300, timeout=5) if config.mode == "jwt" else None

    async def authenticate(self, authorization: str) -> Principal | None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 16384:
            return None
        if self.config.mode == "local_tokens":
            for secret, user in self.tokens:
                if hmac.compare_digest(token.encode(), secret.encode()):
                    return user
            return None
        try:
            claims = await asyncio.to_thread(self._decode, token)
            roles = claims.get("roles", [])
            if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
                return None
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                return None
            return Principal(claims["sub"], frozenset(roles))
        except (jwt.PyJWTError, ValueError, KeyError):
            return None

    def _decode(self, token):
        key = self.jwks.get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=self.config.audience,
                          issuer=self.config.issuer, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
