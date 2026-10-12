"""Python SDK: sync and async entry points over the Engine."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterator

from .catalog import Catalog
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
        return RouteRequest(messages=messages, **{**self._defaults, **kw})

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
        loop = asyncio.new_event_loop()
        agen = self.astream(messages, **kw)
        try:
            while True:
                try:
                    yield loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            loop.run_until_complete(agen.aclose())
            loop.close()


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("called a sync ModelSpend method inside a running event loop; use the async variant (aroute/apreview/astream)")
