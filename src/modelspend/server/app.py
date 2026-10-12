"""Local FastAPI server.

Endpoints
  POST /v1/chat/completions   OpenAI-compatible subset, model="auto"; routing metadata in `modelspend`
  POST /v1/route              native request/response (full RouteRequest)
  POST /v1/route/preview      estimate candidates, no generation call, no spend
  GET  /v1/catalog /v1/policy /v1/usage     inspection
  GET  /dashboard, /dashboard/data          minimal local dashboard
  GET  /healthz               unauthenticated liveness
Binds to 127.0.0.1 by default; a non-loopback bind requires MODELSPEND_API_TOKEN.
"""
from __future__ import annotations

import hmac
import json
import time
from importlib import resources
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError

from ..config import Config, is_loopback
from ..engine import Engine
from ..errors import InvalidRequest, ModelSpendError
from ..policy import POLICY_VERSION, PROFILES
from ..types import RouteRequest, RouteResult, ToolSpec

UNSUPPORTED_PARAMS = {
    "n": "only n=1 is supported", "logprobs": "logprobs are not supported", "top_logprobs": "not supported",
    "logit_bias": "not supported", "audio": "audio is not supported (text only)", "modalities": "text only",
    "functions": "use `tools`", "function_call": "use `tool_choice`", "parallel_tool_calls": "not supported",
    "top_p": "use modelspend.provider_options.<provider>.top_p", "stop": "use modelspend.provider_options.<provider>",
    "presence_penalty": "use modelspend.provider_options.<provider>", "frequency_penalty": "use modelspend.provider_options.<provider>",
    "seed": "use modelspend.provider_options.<provider>.seed", "user": "not forwarded",
    "prediction": "not supported", "store": "ModelSpend does not store content", "metadata": "not supported",
    "reasoning_effort": "use modelspend.provider_options.<provider>.reasoning_effort", "service_tier": "not supported",
}
KNOWN = {"model", "messages", "max_tokens", "max_completion_tokens", "temperature", "stream", "stream_options",
         "response_format", "tools", "tool_choice", "modelspend"}


def check_bind(host: str, token: str | None) -> None:
    if not is_loopback(host) and not token:
        raise SystemExit(
            f"refusing to bind to {host} without authentication: set MODELSPEND_API_TOKEN "
            "(the server is localhost-only by default)")


def _err(status: int, message: str, code: str, param: str | None = None, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": "invalid_request_error" if status < 500 else "server_error",
                                                              "param": param, "code": code, **extra}})


def to_route_request(body: dict[str, Any], cfg: Config) -> tuple[RouteRequest, bool]:
    """Translate an OpenAI-style body (+ `modelspend` extension) to a RouteRequest."""
    unsup = [k for k in body if k in UNSUPPORTED_PARAMS and body[k] not in (None, 1 if k == "n" else None)]
    if unsup:
        raise InvalidRequest(f"unsupported parameter '{unsup[0]}': {UNSUPPORTED_PARAMS[unsup[0]]}", param=unsup[0])
    unknown = [k for k in body if k not in KNOWN and k not in UNSUPPORTED_PARAMS]
    if unknown:
        raise InvalidRequest(f"unknown parameter '{unknown[0]}'", param=unknown[0])
    if body.get("model") not in ("auto", "modelspend/auto"):
        raise InvalidRequest("only model='auto' is supported; ModelSpend chooses the model. "
                             "Pin providers/models via the `modelspend` extension (providers, allow_models).", param="model")
    if not isinstance(body.get("messages"), list):
        raise InvalidRequest("messages is required", param="messages")
    msgs = []
    for m in body["messages"]:
        role = "system" if m.get("role") == "developer" else m.get("role")
        if role == "function":
            raise InvalidRequest("role 'function' is not supported; use tools", param="messages")
        msgs.append({"role": role, "content": m.get("content"), "name": m.get("name"), "tool_call_id": m.get("tool_call_id"),
                     "tool_calls": [{"id": t["id"], "name": t["function"]["name"], "arguments": t["function"]["arguments"]}
                                    for t in m.get("tool_calls") or []] or None})
    ext = dict(body.get("modelspend") or {})
    kw: dict[str, Any] = {"messages": msgs, "providers": ext.pop("providers", None) or list(cfg.default_providers)}
    mt = body.get("max_completion_tokens") or body.get("max_tokens")
    if mt:
        kw["max_output_tokens"] = mt
    if body.get("temperature") is not None:
        kw["temperature"] = body["temperature"]
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema":
        kw["response_schema"] = (rf.get("json_schema") or {}).get("schema")
    elif rf.get("type") == "json_object":
        ext.setdefault("validators", []).append({"type": "valid_json"})
    elif rf.get("type") not in (None, "text"):
        raise InvalidRequest(f"response_format type '{rf.get('type')}' is not supported", param="response_format")
    if body.get("tools"):
        kw["tools"] = [ToolSpec(**t["function"]) for t in body["tools"] if t.get("type") == "function"]
        if len(kw["tools"]) != len(body["tools"]):
            raise InvalidRequest("only function tools are supported", param="tools")
    tc = body.get("tool_choice")
    if tc:
        kw["tool_choice"] = tc if isinstance(tc, str) else tc["function"]["name"]
    stream = bool(body.get("stream"))
    kw.update(ext)
    return RouteRequest(**kw), stream


def _usage_block(r: RouteResult) -> dict[str, Any]:
    u = r.usage
    prompt = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
    return {"prompt_tokens": prompt, "completion_tokens": u.output_tokens, "total_tokens": prompt + u.output_tokens,
            "prompt_tokens_details": {"cached_tokens": u.cache_read_tokens},
            "completion_tokens_details": {"reasoning_tokens": u.reasoning_tokens}}


def _tool_calls(r: RouteResult) -> list[dict[str, Any]]:
    return [{"id": t.id, "type": "function", "function": {"name": t.name, "arguments": t.arguments}} for t in r.tool_calls]


def _finish(r: RouteResult) -> str:
    return "content_filter" if r.status == "refused" else (r.finish_reason or "stop")


def completion_body(r: RouteResult) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": r.text or None}
    if r.tool_calls:
        msg["tool_calls"] = _tool_calls(r)
    return {"id": f"chatcmpl-{r.request_id}", "object": "chat.completion", "created": int(time.time()), "model": r.model,
            "choices": [{"index": 0, "message": msg, "finish_reason": _finish(r)}],
            "usage": _usage_block(r), "modelspend": r.json_metadata()}


def create_app(engine: Engine, cfg: Config | None = None) -> FastAPI:
    cfg = cfg or engine.cfg
    app = FastAPI(title="ModelSpend", version="0.1.0")

    async def auth(request: Request) -> None:
        if not cfg.api_token:
            return
        got = request.headers.get("authorization", "")
        if not got.startswith("Bearer ") or not hmac.compare_digest(got[7:], cfg.api_token):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return _err(e.status_code, str(e.detail), "unauthorized" if e.status_code == 401 else "http_error")

    @app.exception_handler(ModelSpendError)
    async def _ms(_: Request, e: ModelSpendError):
        extra = {}
        if "plan" in e.details:
            extra["modelspend"] = {"plan": json.loads(e.details["plan"].model_dump_json())}
        if "partial_text" in e.details:
            extra["partial_text"] = e.details["partial_text"]
        return _err(e.http_status, e.message, e.code, e.details.get("param"), **extra)

    @app.exception_handler(ValidationError)
    async def _val(_: Request, e: ValidationError):
        msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        code = "unsupported_input" if "not supported" in msg else "invalid_request"
        return _err(400, msg, code)

    async def _json(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise InvalidRequest("request body must be valid JSON")
        if not isinstance(body, dict):
            raise InvalidRequest("request body must be a JSON object")
        return body

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "policy_version": POLICY_VERSION, "catalog_version": engine.catalog.version,
                "providers_configured": sorted(engine.adapters)}

    @app.post("/v1/chat/completions", dependencies=[Depends(auth)])
    async def chat(request: Request):
        req, stream = to_route_request(await _json(request), cfg)
        if not stream:
            return JSONResponse(completion_body(await engine.run(req)))
        agen = engine.stream(req)
        first = await agen.__anext__()  # plan errors surface as proper HTTP errors before streaming starts
        return StreamingResponse(_sse(agen, first, req), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def _sse(agen: AsyncIterator, first, req: RouteRequest) -> AsyncIterator[str]:
        cid, created = f"chatcmpl-{first.plan.request_id}", int(time.time())
        sel = first.plan.candidates[0]

        def chunk(delta: dict, finish: str | None = None, **extra: Any) -> str:
            return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": sel.model,
                                          "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}) + "\n\n"
        yield chunk({"role": "assistant", "content": ""}, modelspend={"routing": {"provider": sel.provider, "model": sel.model,
                    "task": first.plan.task.model_dump(mode="json"), "explanation": first.plan.explanation,
                    "catalog_version": first.plan.catalog_version, "policy_version": first.plan.policy_version}})
        try:
            async for ev in agen:
                if ev.type == "delta":
                    yield chunk({"content": ev.text})
                elif ev.type == "done":
                    r = ev.result
                    delta = {"tool_calls": [{"index": i, **t} for i, t in enumerate(_tool_calls(r))]} if r.tool_calls else {}
                    yield chunk(delta, _finish(r), usage=_usage_block(r), modelspend=r.json_metadata())
        except ModelSpendError as e:
            yield "data: " + json.dumps({"error": {"message": e.message, "type": "server_error", "code": e.code,
                                                    "partial_text": e.details.get("partial_text")}}) + "\n\n"
        yield "data: [DONE]\n\n"

    @app.post("/v1/route", dependencies=[Depends(auth)])
    async def route(request: Request):
        req = RouteRequest(**_with_default_providers(await _json(request), cfg))
        r = await engine.run(req)
        return JSONResponse(json.loads(r.model_dump_json()))

    @app.post("/v1/route/preview", dependencies=[Depends(auth)])
    async def preview(request: Request):
        req = RouteRequest(**_with_default_providers(await _json(request), cfg))
        plan = await engine.plan(req)
        return JSONResponse(json.loads(plan.model_dump_json()))

    @app.get("/v1/catalog", dependencies=[Depends(auth)])
    async def catalog():
        c = engine.catalog
        return {"catalog_version": c.version, "models": [json.loads(m.model_dump_json()) for m in c.models],
                "issues": [str(i) for i in c.validate(max_age_days=cfg.max_pricing_age_days)]}

    @app.get("/v1/policy", dependencies=[Depends(auth)])
    async def policy():
        return {"policy_version": POLICY_VERSION, "profiles": {k.value: {**v.__dict__, "name": k.value} for k, v in PROFILES.items()}}

    @app.get("/v1/usage", dependencies=[Depends(auth)])
    @app.get("/dashboard/data", dependencies=[Depends(auth)])
    async def usage():
        return engine.store.summary() if engine.store else {"totals": {"n": 0}, "recent": []}

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return resources.files("modelspend.server").joinpath("dashboard.html").read_text()

    return app


def _with_default_providers(body: dict[str, Any], cfg: Config) -> dict[str, Any]:
    if not body.get("providers") and cfg.default_providers:
        body = {**body, "providers": list(cfg.default_providers)}
    return body
