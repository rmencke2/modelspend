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
from typing import Any, AsyncIterator, Callable

import asyncio
import os
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError

from ..config import Config, is_loopback
from ..engine import Engine
from ..errors import InvalidRequest, ModelSpendError
from ..pricing_refresh import apply_proposal, get_proposal, list_proposals, refresh_provider, reject_proposal
from ..sources import check_source, load_snapshots, resolve_sources, set_user_source
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


def _tags_from_headers(headers: Any) -> dict[str, Any]:
    """X-ModelSpend-Project / -Agent / -Run / -Labels ("k=v,k=v"). Invalid values are rejected by RouteRequest validation."""
    out: dict[str, Any] = {}
    for hdr, key in (("x-modelspend-project", "project"), ("x-modelspend-agent", "agent"), ("x-modelspend-run", "run_id")):
        v = headers.get(hdr)
        if v:
            out[key] = v
    raw = headers.get("x-modelspend-labels")
    if raw:
        labels = {}
        for part in raw.split(","):
            if "=" not in part:
                raise InvalidRequest("X-ModelSpend-Labels must look like key=value,key=value", param="x-modelspend-labels")
            k, v = part.split("=", 1)
            labels[k.strip()] = v.strip()
        out["labels"] = labels
    return out


def _resolve_pin(model: str, catalog: Any, aliases: dict[str, str] | None = None) -> str:
    """model='provider/id', an alias, or a bare catalog id -> 'provider/id' (observe-only: no routing)."""
    if "/" in model:
        return model
    if aliases and model.strip().lower() in aliases:
        return aliases[model.strip().lower()]
    hits = [m.ref for m in (catalog.models if catalog is not None else []) if m.id == model]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise InvalidRequest(f"model '{model}' exists under several providers ({', '.join(hits)}); use 'provider/model-id'", param="model")
    raise InvalidRequest(f"unknown model '{model}'. Use model='auto' to let ModelSpend choose, an alias you defined (`modelspend alias set`), or "
                         "'provider/model-id' for a catalog model (observe-only: no routing, tokens and cost are still recorded).", param="model")


def to_route_request(body: dict[str, Any], cfg: Config, headers: Any = None, catalog: Any = None) -> tuple[RouteRequest, bool]:
    """Translate an OpenAI-style body (+ `modelspend` extension) to a RouteRequest."""
    unsup = [k for k in body if k in UNSUPPORTED_PARAMS and body[k] not in (None, 1 if k == "n" else None)]
    if unsup:
        raise InvalidRequest(f"unsupported parameter '{unsup[0]}': {UNSUPPORTED_PARAMS[unsup[0]]}", param=unsup[0])
    unknown = [k for k in body if k not in KNOWN and k not in UNSUPPORTED_PARAMS]
    if unknown:
        raise InvalidRequest(f"unknown parameter '{unknown[0]}'", param=unknown[0])
    model = body.get("model")
    pin = None
    if model not in ("auto", "modelspend/auto"):
        if not isinstance(model, str) or not model:
            raise InvalidRequest("model is required: 'auto' or a catalog model ('provider/model-id')", param="model")
        pin = _resolve_pin(model, catalog, cfg.aliases)
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
    kw: dict[str, Any] = {"messages": msgs, "providers": ext.pop("providers", None) or ([pin.split("/", 1)[0]] if pin else list(cfg.default_providers))}
    if pin:
        kw["pin"] = pin
    # attribution precedence: `modelspend` body extension > X-ModelSpend-* headers > MODELSPEND_PROJECT / MODELSPEND_AGENT
    kw.update({k: v for k, v in (("project", cfg.default_project), ("agent", cfg.default_agent)) if v})
    kw.update(_tags_from_headers(headers) if headers is not None else {})
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


def create_app(engine: Engine, cfg: Config | None = None, reload_catalog: Callable[[], Any] | None = None) -> FastAPI:
    cfg = cfg or engine.cfg
    app = FastAPI(title="ModelSpend", version="0.7.0b1", docs_url=None, redoc_url=None, openapi_url=None)  # endpoints are documented in docs/openai-compat.md

    async def auth(request: Request) -> None:
        if not cfg.api_token:
            return
        got = request.headers.get("authorization", "")
        if not got.startswith("Bearer ") or not hmac.compare_digest(got[7:].encode("utf-8"), cfg.api_token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    loopbacks = {"127.0.0.1", "localhost", "::1"} | {cfg.host} | {h.strip() for h in os.environ.get("MODELSPEND_ALLOWED_HOSTS", "").split(",") if h.strip()}

    async def host_guard(request: Request) -> None:
        """DNS-rebinding protection for every data endpoint (reads too). Without a bearer token the server is local-only, so the
        Host header must name this machine; a hostile web page that re-points its own DNS name at 127.0.0.1 carries its own name."""
        if cfg.api_token:
            return
        host = request.headers.get("host", "")
        if (urlparse("//" + host).hostname or "") not in loopbacks:
            raise HTTPException(status_code=403, detail="unexpected Host header (DNS-rebinding protection); add it to MODELSPEND_ALLOWED_HOSTS if intended")

    async def guard(request: Request) -> None:
        """Browser-safety for anything that spends money or changes settings. With a bearer token configured this is already
        covered (browsers never attach it cross-site). Without one: a loopback Host, a JSON content type (forces a CORS
        preflight) and a same-origin Origin."""
        if cfg.api_token:
            return
        await host_guard(request)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="Content-Type must be application/json")
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != host:
            raise HTTPException(status_code=403, detail="cross-origin request refused")

    max_body = int(os.environ.get("MODELSPEND_MAX_BODY_BYTES", 2_000_000))

    @app.middleware("http")
    async def _hardening(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > max_body:
            return _err(413, f"request body larger than {max_body} bytes (MODELSPEND_MAX_BODY_BYTES)", "payload_too_large")
        resp = await call_next(request)
        # a hostile page must not be able to frame the dashboard and trick a click on Apply / Refresh / Save
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
        return resp

    def _reload() -> None:
        if reload_catalog is not None:
            engine.set_catalog(reload_catalog())

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

    @app.get("/healthz", dependencies=[Depends(host_guard)])
    async def healthz():
        return {"ok": True, "policy_version": POLICY_VERSION, "catalog_version": engine.catalog.version,
                "providers_configured": sorted(engine.adapters)}

    @app.post("/v1/chat/completions", dependencies=[Depends(auth), Depends(guard)])
    async def chat(request: Request):
        req, stream = to_route_request(await _json(request), cfg, request.headers, engine.catalog)
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

    @app.post("/v1/route", dependencies=[Depends(auth), Depends(guard)])
    async def route(request: Request):
        body = {**_tags_from_headers(request.headers), **(await _json(request))}  # body wins over headers
        req = RouteRequest(**_with_default_providers(body, cfg))
        r = await engine.run(req)
        return JSONResponse(json.loads(r.model_dump_json()))

    @app.post("/v1/route/preview", dependencies=[Depends(auth), Depends(guard)])
    async def preview(request: Request):
        req = RouteRequest(**_with_default_providers(await _json(request), cfg))
        plan = await engine.plan(req)
        return JSONResponse(json.loads(plan.model_dump_json()))

    @app.get("/v1/catalog", dependencies=[Depends(auth), Depends(host_guard)])
    async def catalog():
        c = engine.catalog
        return {"catalog_version": c.version, "models": [json.loads(m.model_dump_json()) for m in c.models],
                "issues": [str(i) for i in c.validate(max_age_days=cfg.max_pricing_age_days)]}

    @app.get("/v1/policy", dependencies=[Depends(auth), Depends(host_guard)])
    async def policy():
        return {"policy_version": POLICY_VERSION, "profiles": {k.value: {**v.__dict__, "name": k.value} for k, v in PROFILES.items()},
                "thresholds": {k: v.as_dict() for k, v in cfg.thresholds.items()}}

    @app.get("/v1/evidence", dependencies=[Depends(auth), Depends(host_guard)])
    async def evidence_view(request: Request):
        """Which models clear the evidence bar (the profile's, or your own thresholds) for which task category (`?profile=&category=&model=`)."""
        from .. import evidence as ev
        q = request.query_params
        extra = [k for k in q if k not in ("profile", "category", "model")]
        if extra:
            raise InvalidRequest(f"unknown query parameter '{extra[0]}'", param=extra[0])
        try:
            return ev.build(engine.catalog, cfg, q.get("profile", "balanced"), q.get("category"), q.get("model"))
        except ValueError as e:
            raise InvalidRequest(str(e))

    @app.get("/v1/usage", dependencies=[Depends(auth), Depends(host_guard)])
    @app.get("/dashboard/data", dependencies=[Depends(auth), Depends(host_guard)])
    async def usage():
        return engine.store.summary() if engine.store else {"totals": {"n": 0}, "recent": []}

    @app.get("/v1/usage/report", dependencies=[Depends(auth), Depends(host_guard)])
    async def usage_report(request: Request):
        """Tokens and cost grouped by project, agent, model, ... (`?by=project,agent&since=7d&project=billing`)."""
        from ..report import FILTERS, ReportError, build_report, parse_since
        q = request.query_params
        known = {"by", "since", "until", "top", *FILTERS}
        extra = [k for k in q if k not in known]
        if extra:
            raise InvalidRequest(f"unknown query parameter '{extra[0]}'", param=extra[0])
        if engine.store is None:
            raise InvalidRequest("usage storage is disabled")
        try:
            top = int(q.get("top", "200"))
            if not 1 <= top <= 1000:
                raise ValueError
        except ValueError:
            raise InvalidRequest("top must be an integer between 1 and 1000", param="top")
        try:
            return build_report(engine.store, [d.strip() for d in q.get("by", "project,agent,model").split(",") if d.strip()],
                                filters={k: q[k] for k in FILTERS if q.get(k)}, since=parse_since(q.get("since", "30d")),
                                until=parse_since(q.get("until")), limit=top)
        except ReportError as e:
            raise InvalidRequest(str(e))

    # ---------------- savings and shadow evaluation ----------------
    @app.get("/v1/savings", dependencies=[Depends(auth), Depends(host_guard)])
    async def savings_view(request: Request):
        """What each kind of work could save on a cheaper model, with the evidence (`?since=30d&profile=balanced&project=`). A projection, not a measurement."""
        from ..report import ReportError, parse_since
        from ..savings import build_savings
        q = request.query_params
        extra = [k for k in q if k not in ("since", "profile", "project")]
        if extra:
            raise InvalidRequest(f"unknown query parameter '{extra[0]}'", param=extra[0])
        if engine.store is None:
            raise InvalidRequest("usage storage is disabled")
        if q.get("profile", "balanced") not in ("economy", "balanced", "high_assurance"):
            raise InvalidRequest("profile must be economy, balanced or high_assurance", param="profile")
        try:
            return build_savings(engine.store, cfg, profile=q.get("profile", "balanced"), since=parse_since(q.get("since", "30d")), project=q.get("project") or None)
        except ReportError as e:
            raise InvalidRequest(str(e), param="since")

    def _shadow_state() -> dict:
        from dataclasses import asdict
        from ..shadow import aggregate, spent_this_month
        return {"settings": asdict(cfg.shadow), "active": engine.shadow is not None and cfg.shadow.enabled,
                "spent_this_month": spent_this_month(engine.store) if engine.store else 0.0,
                "comparisons": aggregate(engine.store) if engine.store else []}

    @app.get("/v1/shadow", dependencies=[Depends(auth), Depends(host_guard)])
    async def shadow_view():
        return _shadow_state()

    @app.put("/v1/shadow", dependencies=[Depends(auth), Depends(guard)])
    async def shadow_put(request: Request):
        """Change shadow settings on the running server and save them (validated first; an invalid change changes nothing)."""
        from ..config import update_shadow_settings
        body = await _json(request)
        try:
            new = update_shadow_settings(cfg.home, body)
        except ValueError as e:
            raise InvalidRequest(str(e))
        engine.set_shadow(new)
        return _shadow_state()

    # ---------------- aliases (pick the model by name) ----------------
    @app.get("/v1/aliases", dependencies=[Depends(auth), Depends(host_guard)])
    async def aliases_list():
        from ..aliases import describe
        return {"aliases": describe(cfg, engine.catalog)}

    @app.put("/v1/aliases/{name}", dependencies=[Depends(auth), Depends(guard)])
    async def aliases_put(name: str, request: Request):
        from ..aliases import describe, set_alias
        body = await _json(request)
        extra = [k for k in body if k != "model"]
        if extra or not isinstance(body.get("model"), str):
            raise InvalidRequest('body must be {"model": "provider/model-id"}')
        try:
            n, _ = set_alias(cfg, name, body["model"], engine.catalog)
        except ValueError as e:
            raise InvalidRequest(str(e))
        return {"alias": next(a for a in describe(cfg, engine.catalog) if a["alias"] == n)}

    @app.delete("/v1/aliases/{name}", dependencies=[Depends(auth), Depends(guard)])
    async def aliases_delete(name: str):
        from ..aliases import remove_alias
        if not remove_alias(cfg, name):
            raise InvalidRequest(f"no alias '{name}'")
        return {"removed": name}

    # ---------------- budgets (per project / agent) ----------------
    @app.get("/v1/budgets", dependencies=[Depends(auth), Depends(host_guard)])
    async def budgets_status():
        from ..budgets import status
        return {"budgets": status(engine.store) if engine.store else []}

    @app.put("/v1/budgets", dependencies=[Depends(auth), Depends(guard)])
    async def budgets_put(request: Request):
        from ..budgets import set_budget, status
        if engine.store is None:
            raise InvalidRequest("usage storage is disabled")
        body = await _json(request)
        extra = [k for k in body if k not in ("project", "agent", "period", "limit_usd", "limit_tokens", "mode")]
        if extra:
            raise InvalidRequest(f"unknown field '{extra[0]}'", param=extra[0])
        try:
            b = set_budget(engine.store, project=body.get("project"), agent=body.get("agent"), period=body.get("period", "monthly"),
                           limit_usd=body.get("limit_usd"), limit_tokens=body.get("limit_tokens"), mode=body.get("mode", "hard"))
        except ValueError as e:
            raise InvalidRequest(str(e))
        return {"budget": next((s for s in status(engine.store) if s["id"] == b.id), None)}

    @app.delete("/v1/budgets/{budget_id}", dependencies=[Depends(auth), Depends(guard)])
    async def budgets_delete(budget_id: int):
        from ..budgets import remove_budget
        if engine.store is None or not remove_budget(engine.store, budget_id):
            raise InvalidRequest(f"no budget #{budget_id}")
        return {"removed": budget_id}

    # ---------------- pricing sources and refresh (dashboard "Pricing" tab) ----------------
    def _sources_view() -> dict:
        snaps = load_snapshots(cfg)
        out = []
        for p, s in resolve_sources(cfg).items():
            snap = snaps.get(p)
            out.append({"provider": p, "pricing_url": s.pricing_url, "origin": s.origin,
                        "baseline_accepted_on": snap["accepted_on"] if snap and snap.get("url") == s.pricing_url else None})
        return {"providers": sorted(out, key=lambda x: __import__("modelspend.registry", fromlist=["x"]).all_providers().index(x["provider"]))}

    def _provider(name: str) -> str:
        from ..types import normalize_provider
        try:
            return normalize_provider(name)
        except ValueError as e:
            raise InvalidRequest(str(e))

    @app.get("/v1/sources", dependencies=[Depends(auth), Depends(host_guard)])
    async def sources_get():
        return _sources_view()

    @app.put("/v1/sources/{provider}", dependencies=[Depends(auth), Depends(guard)])
    async def sources_put(provider: str, request: Request):
        body = await _json(request)
        try:
            set_user_source(cfg, _provider(provider), body.get("url") or None)
        except ValueError as e:
            raise InvalidRequest(str(e))
        return _sources_view()

    @app.post("/v1/sources/{provider}/check", dependencies=[Depends(auth), Depends(guard)])
    async def sources_check(provider: str, request: Request):
        body = await _json(request)
        src = resolve_sources(cfg)[_provider(provider)]
        return await asyncio.to_thread(check_source, cfg, src, bool(body.get("accept")))

    @app.post("/v1/pricing/refresh", dependencies=[Depends(auth), Depends(guard)])
    async def pricing_refresh(request: Request):
        body = await _json(request)
        if body.get("auto_apply") or "max_change_pct" in body:
            raise InvalidRequest("unattended auto-apply is only available from the CLI (`modelspend pricing refresh --auto-apply`); "
                                 "over HTTP every price change is a proposal a person applies")
        r = await refresh_provider(engine, cfg, _provider(body.get("provider", "")), text=body.get("text") or None,
                                   force=bool(body.get("force")))
        if r["state"] == "applied":
            _reload()
        return r

    @app.get("/v1/pricing/proposals", dependencies=[Depends(auth), Depends(host_guard)])
    async def proposals(status: str = "pending"):
        return {"proposals": list_proposals(cfg, None if status == "all" else status)}

    @app.post("/v1/pricing/proposals/{pid}/{action}", dependencies=[Depends(auth), Depends(guard)])
    async def proposal_action(pid: str, action: str):
        if action not in ("apply", "reject"):
            raise HTTPException(status_code=404, detail="unknown action")
        try:
            p = (apply_proposal if action == "apply" else reject_proposal)(cfg, pid)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if action == "apply":
            _reload()
        return p

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/dashboard")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return resources.files("modelspend.server").joinpath("dashboard.html").read_text()

    return app


def _with_default_providers(body: dict[str, Any], cfg: Config) -> dict[str, Any]:
    if not body.get("providers") and cfg.default_providers:
        body = {**body, "providers": list(cfg.default_providers)}
    return body
