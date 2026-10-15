"""Python SDK: sync and async entry points over the Engine."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncIterator, Iterator

from .catalog import Catalog
from .aliases import resolve as resolve_alias
from .config import Config
from .engine import Engine
from .providers import ProviderAdapter, build_adapters
from .store import Store
from .types import Plan, RouteRequest, RouteResult, StreamEvent


class ModelSpend:
    """
    ms = ModelSpend(providers=["anthropic", "openai"], profile="balanced")
    result = ms.route("Classify this ticket as billing|bug|other: ...", max_budget_usd=0.01)
    print(result.text, result.provider, result.model, result.actual_cost_usd)
    """

    def __init__(
        self,
        providers: list[str] | None = None,
        profile: str = "balanced",
        config: Config | None = None,
        catalog: Catalog | None = None,
        adapters: dict[str, ProviderAdapter] | None = None,
        store: Store | bool | None = True,
        **defaults: Any,
    ):
        self.config = config or Config.from_env()
        self.catalog = catalog or Catalog.from_config(self.config)
        self.adapters = adapters if adapters is not None else build_adapters(self.config)
        if store is True:
            store = Store(self.config.db_path)  # type: ignore[arg-type]
        self.store = store or None
        self.engine = Engine(self.catalog, self.config, self.adapters, self.store)
        self._defaults = {"providers": providers or [], "profile": profile, **defaults}

    def _req(self, messages: str | list[Any], kw: dict[str, Any]) -> RouteRequest:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        merged = {**self._defaults, **kw}
        if merged.get("pin"):
            merged["pin"] = resolve_alias(merged["pin"], self.config.aliases)  # pin="coding" -> the model that alias points to
        return RouteRequest(messages=messages, **merged)

    # ---- async ----
    async def aroute(self, messages: str | list[Any], **kw: Any) -> RouteResult:
        return await self.engine.run(self._req(messages, kw))

    async def apreview(self, messages: str | list[Any], **kw: Any) -> Plan:
        return await self.engine.plan(self._req(messages, kw))

    def astream(self, messages: str | list[Any], **kw: Any) -> AsyncIterator[StreamEvent]:
        return self.engine.stream(self._req(messages, kw))

    # ---- sync ----
    def route(self, messages: str | list[Any], **kw: Any) -> RouteResult:
        return _run(self.aroute(messages, **kw))

    def preview(self, messages: str | list[Any], **kw: Any) -> Plan:
        return _run(self.apreview(messages, **kw))

    def stream(self, messages: str | list[Any], **kw: Any) -> Iterator[StreamEvent]:
        _guard_running_loop()
        agen = self.astream(messages, **kw)
        try:
            while True:
                try:
                    yield _call(agen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            _call(agen.aclose())


# Sync calls all run on ONE long-lived background loop. Provider SDK clients are cached on the adapters and keep connection
# pools bound to the loop they were first used on, so a fresh asyncio.run() per call made the second call fail with
# "Event loop is closed".
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _background_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="modelspend-sync-loop", daemon=True).start()
            _loop = loop
        return _loop


def _guard_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("called a sync ModelSpend method inside a running event loop; use the async variant (aroute/apreview/astream)")


def _call(coro):
    return asyncio.run_coroutine_threadsafe(coro, _background_loop()).result()


def _run(coro):
    try:
        _guard_running_loop()
    except RuntimeError:
        coro.close()
        raise
    return _call(coro)
