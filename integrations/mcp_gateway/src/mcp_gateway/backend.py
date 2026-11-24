"""Task-owned SDK connections: context-manager entry/exit stay in the same task.

Each actor connects lazily, serializes operations, and never replays a failed call.
The bounded shared queue distributes work across independent connections.
"""
import asyncio
import hashlib
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .config import Backend


class BackendUnavailable(Exception):
    pass


class OperationNotApplied(Exception):
    """An authenticated backend confirmed that a bound write had no effect."""


@dataclass
class Job:
    operation: str
    args: tuple
    future: asyncio.Future
    deadline: float
    credential: str | None = None


class BackendPool:
    def __init__(self, config: Backend, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport
        self.queue = asyncio.Queue(maxsize=config.queue_size)
        self.tasks = []
        self.failures = 0
        self.open_until = 0.0
        self.tools = None
        self.tools_until = 0.0
        self.catalogs = {}
        self.discovery_lock = asyncio.Lock()

    def start(self):
        self.tasks = [asyncio.create_task(self._worker(), name=f"backend-{self.config.name}-{i}")
                      for i in range(self.config.pool_size)]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        while not self.queue.empty():
            job = self.queue.get_nowait()
            if not job.future.done():
                job.future.set_exception(BackendUnavailable("Gateway shutting down"))

    async def request(self, operation, *args, credential=None):
        if time.monotonic() < self.open_until:
            raise BackendUnavailable("Backend circuit is open")
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait(Job(operation, args, future, time.monotonic() + self.config.timeout_seconds, credential))
        except asyncio.QueueFull:
            raise BackendUnavailable("Backend queue is full") from None
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                return await future
        except TimeoutError:
            future.cancel()
            raise BackendUnavailable("Backend deadline exceeded; execution outcome may be unknown") from None

    async def list_tools(self, ttl, *, credential=None, cache_key="service"):
        if credential is not None:
            # A subject can receive a replacement token with different upstream permissions.
            # Never reuse its old catalog across that credential change.
            catalog_key = (cache_key, hashlib.sha256(credential.encode()).hexdigest())
            async with self.discovery_lock:
                entry = self.catalogs.get(catalog_key)
                if entry is None or time.monotonic() >= entry[1]:
                    tools = await self.request("list_tools", credential=credential)
                    self.catalogs[catalog_key] = (tools, time.monotonic() + ttl)
                    if len(self.catalogs) > 128:
                        del self.catalogs[next(iter(self.catalogs))]
                    return tools
                return entry[0]
        if self.tools is not None and time.monotonic() < self.tools_until:
            return self.tools
        async with self.discovery_lock:
            if self.tools is None or time.monotonic() >= self.tools_until:
                self.tools = await self.request("list_tools")
                self.tools_until = time.monotonic() + ttl
            return self.tools

    async def _execute(self, session, job):
        if job.operation == "list_tools":
            tools, cursor, seen = [], None, set()
            for _ in range(100):
                result = await session.list_tools(cursor=cursor)
                tools.extend(result.tools)
                cursor = result.nextCursor
                if not cursor:
                    return tools
                if cursor in seen:
                    raise BackendUnavailable("Backend repeated discovery cursor")
                seen.add(cursor)
            raise BackendUnavailable("Backend discovery page limit exceeded")
        if job.operation == "call_tool":
            return await session.call_tool(*job.args)
        raise ValueError("Unknown backend operation")

    async def _worker(self):
        stack, session, active_credential = AsyncExitStack(), None, None
        try:
            while True:
                job = await self.queue.get()
                try:
                    if job.future.done():
                        continue
                    remaining = job.deadline - time.monotonic()
                    if remaining <= 0 or time.monotonic() < self.open_until:
                        raise BackendUnavailable("Backend deadline or circuit admission refused")
                    if session is not None and job.credential != active_credential:
                        await stack.aclose()
                        stack, session = AsyncExitStack(), None
                    async with asyncio.timeout(remaining):
                        if session is None:
                            headers = {}
                            if job.credential is not None:
                                headers["Authorization"] = f"Bearer {job.credential}"
                            elif self.config.token_env:
                                value = os.environ.get(self.config.token_env)
                                if not value:
                                    raise BackendUnavailable("Backend credential unavailable")
                                headers["Authorization"] = f"Bearer {value}"
                            http = await stack.enter_async_context(httpx.AsyncClient(headers=headers,
                                timeout=httpx.Timeout(self.config.timeout_seconds), follow_redirects=False, transport=self.transport))
                            read, write, _ = await stack.enter_async_context(streamable_http_client(
                                self.config.url, http_client=http))
                            session = await stack.enter_async_context(ClientSession(read, write))
                            await session.initialize()
                            active_credential = job.credential
                        result = await self._execute(session, job)
                    self.failures = 0
                    self.open_until = 0.0
                    if not job.future.done():
                        job.future.set_result(result)
                except asyncio.CancelledError:
                    if not job.future.done():
                        job.future.set_exception(BackendUnavailable("Backend operation cancelled; outcome may be unknown"))
                    raise
                except Exception:
                    self.failures += 1
                    if self.failures >= self.config.failure_threshold:
                        self.open_until = time.monotonic() + self.config.recovery_seconds
                    if not job.future.done():
                        job.future.set_exception(BackendUnavailable("Backend unavailable; execution outcome may be unknown"))
                    await stack.aclose()
                    stack, session = AsyncExitStack(), None
                finally:
                    self.queue.task_done()
        finally:
            await stack.aclose()
