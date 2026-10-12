"""SIMULATED providers for mock evaluations and CI.

A simulated model answers a fixture correctly iff its catalog tier >= the task's `difficulty`,
otherwise it returns the fixture's `wrong` answer. This exercises the harness and the router's
mechanics deterministically. It says NOTHING about real model quality or real savings.
"""
from __future__ import annotations

import asyncio

from ..catalog import Catalog
from ..config import Config
from ..engine import Engine
from ..providers.base import ProviderRequest, ProviderResponse
from ..providers.mock import ScriptedAdapter
from ..store import Store
from ..types import Usage
from .harness import EvalTask

LATENCY_S = {1: 0.002, 2: 0.004, 3: 0.008}  # simulated, tier-based


def build_sim_engine(catalog: Catalog, tasks: list[EvalTask], store: Store | None = None, providers: list[str] | None = None) -> Engine:
    by_prompt = {t.prompt: t for t in tasks}
    provs = providers or ["openai", "anthropic", "google", "xai"]

    def make(provider: str):
        def script(req: ProviderRequest, n: int) -> ProviderResponse:
            last = next((m.content or "" for m in reversed(req.messages) if m.role == "user"), "")
            task = by_prompt.get(last)
            tier = catalog.get(f"{provider}/{req.model}").tier or 1
            if task is None:  # judge / classifier probes
                text = "ok"
            else:
                text = task.reference if tier >= task.difficulty else task.wrong
            n_in = sum(len(m.content or "") for m in req.messages) // 4 + 4
            return ProviderResponse(text=text, usage=Usage(input_tokens=n_in, output_tokens=max(1, len(text) // 4)))

        class Sim(ScriptedAdapter):
            async def generate(self, req):  # simulated latency
                await asyncio.sleep(LATENCY_S[catalog.get(f"{provider}/{req.model}").tier or 1])
                return await super().generate(req)
        return Sim(provider, script)

    cfg = Config(api_keys={p: "sim" for p in provs}, retry_backoff_s=0.0)
    return Engine(catalog, cfg, {p: make(p) for p in provs}, store=store, sandbox_runner=None)
