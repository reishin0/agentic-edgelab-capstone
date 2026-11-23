"""RFC 8693 adapter for an administrator-configured identity provider.

No login UI, account linking, consent automation, or refresh-token storage.
Issued downstream tokens are cached only in memory, by subject token and backend.
"""
import asyncio
import hashlib
import math
import os
import time
from collections import OrderedDict

import httpx

from .backend import BackendUnavailable
from .config import Backend


class TokenExchange:
    def __init__(self, transport=None):
        self.transport = transport
        self.cache = OrderedDict()
        self.locks = {}

    @staticmethod
    def cache_key(backend: Backend, token: str):
        return backend.name + ":" + hashlib.sha256(token.encode()).hexdigest()

    async def credential(self, backend: Backend, token: str | None):
        if backend.exchange is None:
            return None, "service"
        if not token:
            raise BackendUnavailable("Token exchange requires an authenticated subject token")
        key = self.cache_key(backend, token)
        async with self.locks.setdefault(backend.name, asyncio.Lock()):
            entry = self.cache.pop(key, None)
            if entry and entry[1] > time.monotonic():
                self.cache[key] = entry
                return entry[0], key
            config = backend.exchange
            secret = os.environ.get(config.client_secret_env)
            if not secret:
                raise BackendUnavailable("Token exchange client credential unavailable")
            try:
                async with httpx.AsyncClient(timeout=5, transport=self.transport, follow_redirects=False) as client:
                    response = await client.post(config.token_url, auth=(config.client_id, secret), data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                        "subject_token": token,
                        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "audience": config.audience,
                        "scope": " ".join(config.scopes)})
                    response.raise_for_status()
                    data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Invalid token response")
                value, ttl = data.get("access_token"), float(data.get("expires_in", 0))
                token_type = data.get("token_type")
                if (not isinstance(value, str) or not value or not isinstance(token_type, str)
                        or token_type.lower() != "bearer" or not math.isfinite(ttl) or ttl <= 0):
                    raise ValueError("Invalid token response")
                granted = data.get("scope")
                if granted is not None and (not isinstance(granted, str) or set(granted.split()) != set(config.scopes)):
                    raise ValueError("Granted scopes differ from the configured scope set")
                if data.get("issued_token_type", "urn:ietf:params:oauth:token-type:access_token") != "urn:ietf:params:oauth:token-type:access_token":
                    raise ValueError("Unexpected issued token type")
            except (httpx.HTTPError, ValueError, TypeError):
                raise BackendUnavailable("Token exchange failed") from None
            self.cache[key] = (value, time.monotonic() + max(0, min(ttl, 3600) - 10))
            while len(self.cache) > 1024:
                self.cache.popitem(last=False)
            return value, key
