"""Deterministic fake providers for tests and mock evaluations. Never contacts a network."""
from __future__ import annotations

from typing import AsyncIterator, Callable

from ..errors import ProviderError
from ..types import Usage
from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StreamChunk, StreamEnd

Script = Callable[[ProviderRequest, int], "ProviderResponse | ProviderError | str"]


class ScriptedAdapter(ProviderAdapter):
    """Adapter whose behaviour comes from `script(request, call_number)`.

    The script returns a ProviderResponse, a plain string (wrapped with token-ish usage), or a
    ProviderError instance (raised). For streaming, `fail_stream_after` raises after N chunks.
    """

    def __init__(self, name: str, script: Script, chunk_size: int = 5, fail_stream_after: int | None = None,
                 models: list[str] | None = None, count_fn=None):
        self.name = name
        self.script = script
        self.calls: list[ProviderRequest] = []
        self.chunk_size = chunk_size
        self.fail_stream_after = fail_stream_after
        self._models = models or []
        self._count_fn = count_fn

    def _resolve(self, req: ProviderRequest) -> ProviderResponse:
        self.calls.append(req)
        out = self.script(req, len(self.calls))
        if isinstance(out, ProviderError):
            raise out
        if isinstance(out, str):
            n_in = max(1, sum(len(m.content or "") for m in req.messages) // 4)
            return ProviderResponse(text=out, usage=Usage(input_tokens=n_in, output_tokens=max(1, len(out) // 4)))
        return out

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        return self._resolve(req)

    async def stream(self, req: ProviderRequest) -> AsyncIterator[StreamChunk | StreamEnd]:
        """Emit chunks; if `fail_stream_after=N`, raise after N chunks (0 = before any output)."""
        resp = self._resolve(req)
        if self.fail_stream_after == 0:
            raise ProviderError(self.name, "connection", "simulated disconnect before output")
        sent = 0
        for i in range(0, len(resp.text), self.chunk_size):
            yield StreamChunk(text=resp.text[i:i + self.chunk_size])
            sent += 1
            if self.fail_stream_after is not None and sent >= self.fail_stream_after:
                raise ProviderError(self.name, "connection", "simulated mid-stream disconnect")
        yield StreamEnd(response=resp)

    async def list_models(self) -> list[str]:
        return list(self._models)

    async def count_tokens(self, req: ProviderRequest) -> int | None:
        return self._count_fn(req) if self._count_fn else None
