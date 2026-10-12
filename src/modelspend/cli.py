"""`modelspend` command line interface."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import typer

from . import POLICY_VERSION
from .catalog import Catalog, shipped_catalog_path, write_evidence
from .config import (
    KEY_ENV, Config, keys_path, load_settings, parse_env_file, read_keys_file, save_settings, settings_path, write_keys_file,
)
from .engine import Engine
from .errors import ModelSpendError
from .policy import BASE_TIER, PROFILES
from .providers import build_adapters
from .sources import Source, check_source, clean_url, load_snapshots, resolve_sources, set_user_source
from .store import Store
from .types import RouteRequest, TaskCategory

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ModelSpend: route each task to the lowest-cost suitable model.")
catalog_app = typer.Typer(no_args_is_help=True, help="Inspect, validate and extend the model catalog.")
eval_app = typer.Typer(no_args_is_help=True, help="Evaluation harness (mock by default; live is opt-in with a budget).")
keys_app = typer.Typer(no_args_is_help=True, help="Set, list, test and remove API keys (stored only in ~/.modelspend/keys.env, chmod 600).")
providers_app = typer.Typer(no_args_is_help=True, help="Choose which providers ModelSpend may use by default.")
models_app = typer.Typer(no_args_is_help=True, help="See which models are routable and turn individual models on or off.")
sources_app = typer.Typer(no_args_is_help=True, help="Where each provider's official pricing page lives (change it if a URL moves).")
app.add_typer(catalog_app, name="catalog")
app.add_typer(keys_app, name="keys")
app.add_typer(providers_app, name="providers")
app.add_typer(models_app, name="models")
app.add_typer(sources_app, name="sources")
app.add_typer(eval_app, name="eval")

SYNTH_BANNER = "[SYNTHETIC CATALOG: fictional models and prices; never use for real routing]"


@app.callback()
def _root(env_file: Optional[Path] = typer.Option(None, "--env-file", help="KEY=VALUE file with API keys (never stored or logged).")):
    if env_file:
        for k, v in parse_env_file(env_file).items():
            os.environ.setdefault(k, v)


def _catalog(cfg: Config, which: Optional[str]) -> Catalog:
    if which == "synthetic":
        typer.echo(SYNTH_BANNER, err=True)
        return Catalog.load(shipped_catalog_path("synthetic_test_catalog.json"))
    if which:
        return Catalog.load(Path(which), cfg.overrides_path if cfg.overrides_path and cfg.overrides_path.exists() else None)
    return Catalog.from_config(cfg)


def _providers(s: Optional[str], cfg: Config) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()] if s else list(cfg.default_providers)


def _fail(e: ModelSpendError) -> None:
    typer.echo(f"error [{e.code}]: {e.message}", err=True)
    raise typer.Exit(2)


CAT = typer.Option(None, "--catalog", help="Catalog JSON path, or 'synthetic' for the fictional test catalog.")


def _usd(x: Optional[float]) -> str:
    return "unknown" if x is None else f"${x:.6f}"


def _tok_line(u, total: int) -> str:
    cached = f" (cached {u.cache_read_tokens})" if u.cache_read_tokens else ""
    reason = f" (reasoning {u.reasoning_tokens})" if u.reasoning_tokens else ""
    return f"in {u.input_tokens + u.cache_read_tokens + u.cache_write_tokens}{cached} | out {u.output_tokens}{reason} | total {total}"


def _request(prompt: Optional[str], providers: Optional[str], profile: str, budget: Optional[float], max_out: Optional[int],
             task_type: Optional[str], schema: Optional[Path], system: Optional[str], hard: bool, cfg: Config,
             budget_tokens: Optional[int] = None, allow_unpriced: bool = False) -> RouteRequest:
    if prompt is None or prompt == "-":
        prompt = sys.stdin.read()
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    return RouteRequest(
        messages=msgs, providers=_providers(providers, cfg), profile=profile, max_budget_usd=budget, max_output_tokens=max_out,
        max_budget_tokens=budget_tokens, allow_unpriced=allow_unpriced or cfg.allow_unpriced,
        task_type=TaskCategory(task_type) if task_type else None, hard=hard,
        response_schema=json.loads(schema.read_text()) if schema else None)


@app.command()
def preview(prompt: Optional[str] = typer.Argument(None, help="Prompt text, or '-' for stdin."),
            providers: Optional[str] = typer.Option(None, help="Comma-separated allowlist, e.g. openai,anthropic."),
            profile: str = "balanced", budget: Optional[float] = typer.Option(None, help="Max total USD for the request."),
            max_output_tokens: Optional[int] = None, task_type: Optional[str] = None, schema: Optional[Path] = None,
            system: Optional[str] = None, hard: bool = False, budget_tokens: Optional[int] = typer.Option(None, help="Max TOTAL tokens (input+output) across all attempts."),
            allow_unpriced: bool = typer.Option(False, help="Include models with no catalog pricing; cost is shown as unknown, tokens still tracked."),
            assume_keys: bool = typer.Option(False, help="Plan as if keys exist for the allowlisted providers (no calls are made either way)."),
            catalog: Optional[str] = CAT, as_json: bool = typer.Option(False, "--json")):
    """Estimate candidates and costs without making any generation call."""
    cfg = Config.from_env()
    if assume_keys:
        for p in _providers(providers, cfg):
            cfg.api_keys.setdefault(p, "preview-only")
    cat = _catalog(cfg, catalog)
    eng = Engine(cat, cfg, {}, None)
    eng.router.available = set(cfg.api_keys)
    try:
        plan = asyncio.run(eng.plan(_request(prompt, providers, profile, budget, max_output_tokens, task_type, schema, system, hard, cfg, budget_tokens, allow_unpriced)))
    except ModelSpendError as e:
        plan = e.details.get("plan")
        if plan is None or as_json is False:
            _fail(e)
        typer.echo(plan.model_dump_json(indent=2))
        raise typer.Exit(2)
    if as_json:
        typer.echo(plan.model_dump_json(indent=2))
        return
    typer.echo(plan.explanation + "\n")
    typer.echo(f"ranked by: {plan.ranking_basis} | input ~{plan.input_tokens} tokens ({plan.token_method})")
    typer.echo(f"{'#':<3}{'model':<40}{'tier':<5}{'basis':<9}{'tokens exp/max':>16}{'est. cost':>12}{'worst case':>12}")
    for c in plan.candidates:
        typer.echo(f"{c.rank:<3}{c.provider + '/' + c.model:<40}{c.tier or '-':<5}{c.suitability_basis:<9}"
                   f"{str(c.expected_total_tokens) + '/' + str(c.worst_case_total_tokens):>16}{_usd(c.expected_cost_usd):>12}{_usd(c.worst_case_cost_usd):>12}")
    for c in plan.unsuitable:
        typer.echo(f"-  {c.provider + '/' + c.model:<44}{c.tier or '-':<5}{'unsuitable':<9}  {c.suitability_detail}")
    for e in plan.excluded:
        typer.echo(f"x  {e.provider + '/' + e.model:<44}[{e.stage}] {e.reason}")
    typer.echo("\nAssumptions:\n  - " + "\n  - ".join(plan.assumptions))
    if plan.uncertainty:
        typer.echo("Uncertainty:\n  - " + "\n  - ".join(plan.uncertainty))
    typer.echo(f"\ncatalog {plan.catalog_version} | policy {plan.policy_version} | escalation limit {plan.escalation_limit}")
    typer.echo("Estimates only: usage is finalized by the provider after the call.")


@app.command()
def route(prompt: Optional[str] = typer.Argument(None, help="Prompt text, or '-' for stdin."),
          providers: Optional[str] = None, profile: str = "balanced", budget: Optional[float] = None,
          max_output_tokens: Optional[int] = None, task_type: Optional[str] = None, schema: Optional[Path] = None,
          system: Optional[str] = None, hard: bool = False, catalog: Optional[str] = CAT,
          budget_tokens: Optional[int] = typer.Option(None, help="Max TOTAL tokens (input+output) across all attempts."),
          allow_unpriced: bool = typer.Option(False, help="Allow models with no catalog pricing; cost is shown as unknown."),
          as_json: bool = typer.Option(False, "--json")):
    """Route a task to a model and run it (spends money with your API keys)."""
    cfg = Config.from_env()
    eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), Store(cfg.db_path))  # type: ignore[arg-type]
    try:
        res = asyncio.run(eng.run(_request(prompt, providers, profile, budget, max_output_tokens, task_type, schema, system, hard, cfg, budget_tokens, allow_unpriced)))
    except ModelSpendError as e:
        _fail(e)
    if as_json:
        typer.echo(res.model_dump_json(indent=2))
        return
    typer.echo(res.text)
    typer.echo(f"\n--- {res.provider}/{res.model} | {res.status} | validation: {res.validation.status}", err=True)
    first = next((a for a in res.attempts if a.kind == "generation" and a.outcome == "ok"), None)
    if first:
        got = first.usage.input_tokens + first.usage.cache_read_tokens + first.usage.cache_write_tokens
        typer.echo(f"--- input tokens: estimated ~{res.estimated_input_tokens} vs provider-reported {got} (first attempt)", err=True)
    typer.echo(f"--- TOKENS: {_tok_line(res.usage, res.total_tokens)} (provider-reported, all {len(res.attempts)} attempt(s))"
               + (f" | token budget left {res.budget_tokens_remaining}/{res.budget_tokens}" if res.budget_tokens else ""), err=True)
    typer.echo(f"--- cost: est {_usd(res.estimated_cost_usd)} -> actual {_usd(res.actual_cost_usd)}"
               + (f" (+{_usd(res.uncertain_cost_usd)} uncertain)" if res.uncertain_cost_usd else "")
               + f" | retries {res.retries} failovers {res.failovers} escalations {res.escalations}", err=True)
    typer.echo(f"--- {res.explanation}", err=True)


@app.command()
def serve(host: Optional[str] = None, port: Optional[int] = None, catalog: Optional[str] = CAT):
    """Run the local API server (127.0.0.1 by default; non-loopback requires MODELSPEND_API_TOKEN)."""
    import uvicorn

    from .server.app import check_bind, create_app
    cfg = Config.from_env()
    cfg.host, cfg.port = host or cfg.host, port or cfg.port
    check_bind(cfg.host, cfg.api_token)
    eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), Store(cfg.db_path))  # type: ignore[arg-type]
    if not eng.adapters:
        typer.echo("warning: no provider API keys found in the environment; requests will have no eligible models", err=True)
    uvicorn.run(create_app(eng, cfg), host=cfg.host, port=cfg.port, log_level="info")


@app.command()
def usage(limit: int = 10):
    """Summarize recorded usage (metadata only; no prompts are stored)."""
    cfg = Config.from_env()
    s = Store(cfg.db_path).summary(limit)  # type: ignore[arg-type]
    t = s["totals"]
    typer.echo(f"requests {t['n']} | TOKENS total {t['total_tokens']} = in {t['input_tokens']} (cached {t['cache_read_tokens']}) + out {t['output_tokens']} (reasoning {t['reasoning_tokens']})")
    typer.echo(f"cost: actual {_usd(t['act'])} | estimated {_usd(t['est'])} | uncertain {_usd(t['unc'])} | {t['unpriced_requests']} request(s) with unknown cost | escalation rate {s['escalation_rate']*100:.1f}%")
    for title, rows, key in (("by provider", s["by_provider"], "provider"), ("by task", s["by_task"], "task"), ("by model", s["by_model"], "model")):
        typer.echo(f"\n{title}:  (requests, input, output tokens | cost)")
        for r in rows:
            cost = _usd(r["cost"]) if r["cost"] is not None else "unknown"
            typer.echo(f"  {r[key]:<34}{r['n']:>5} {r['input_tokens']:>10} {r['output_tokens']:>9} tok | {cost}")


@app.command()
def policy():
    """Print the routing policy (profiles and per-category tier requirements)."""
    typer.echo(f"policy {POLICY_VERSION}\n")
    for p in PROFILES.values():
        typer.echo(f"{p.name.value}: {p.description}\n  tier_delta={p.tier_delta} allow_prior={p.allow_prior} min_n={p.min_n} "
                   f"min_lower_bound={p.min_lower_bound} escalation_default={p.escalation_default} require_validation={p.require_validation}")
    typer.echo("\nbase tier per category: " + ", ".join(f"{c.value}={t}" for c, t in BASE_TIER.items()))


# ---------------------------------------------------------------- catalog
@catalog_app.command("list")
def catalog_list(catalog: Optional[str] = CAT, provider: Optional[str] = None):
    cfg = Config.from_env()
    cat = _catalog(cfg, catalog)
    typer.echo(f"catalog {cat.version}")
    typer.echo(f"{'model':<46}{'avail':<12}{'tier':<5}{'in $/M':>9}{'out $/M':>9}  priced-on")
    for m in cat.models:
        if provider and m.provider != provider:
            continue
        p = m.pricing
        typer.echo(f"{m.ref:<46}{m.availability:<12}{m.tier or '-':<5}{(p.input_per_mtok if p and p.known else '?'):>9}"
                   f"{(p.output_per_mtok if p and p.known else '?'):>9}  {p.verified_on if p and p.verified_on else '-'}")


@catalog_app.command("show")
def catalog_show(ref: str, catalog: Optional[str] = CAT):
    cat = _catalog(Config.from_env(), catalog)
    typer.echo(cat.get(ref).model_dump_json(indent=2))


@catalog_app.command("validate")
def catalog_validate(catalog: Optional[str] = CAT, max_age_days: int = 30):
    """Exit 1 on errors; warnings (stale/unknown pricing) are listed."""
    cat = _catalog(Config.from_env(), catalog)
    issues = cat.validate(max_age_days=max_age_days)
    for i in issues:
        typer.echo(str(i))
    errs = [i for i in issues if i.severity == "error"]
    typer.echo(f"{len(errs)} errors, {len(issues) - len(errs)} warnings (catalog {cat.version})")
    raise typer.Exit(1 if errs else 0)


@catalog_app.command("stale")
def catalog_stale(catalog: Optional[str] = CAT, max_age_days: int = 30):
    cat = _catalog(Config.from_env(), catalog)
    rows = cat.stale(max_age_days=max_age_days)
    for m in rows:
        typer.echo(f"{m.ref}: pricing verified_on={m.pricing.verified_on if m.pricing else None}")
    typer.echo(f"{len(rows)} model(s) with stale or undated pricing (threshold {max_age_days} days)")


@catalog_app.command("discover")
def catalog_discover(catalog: Optional[str] = CAT):
    """Compare each provider's live model list with the catalog. Never edits the catalog:
    discovery gives IDs only, not prices or capabilities, so new IDs must be verified by hand."""
    cfg = Config.from_env()
    cat = _catalog(cfg, catalog)
    adapters = build_adapters(cfg)
    if not adapters:
        typer.echo("no API keys configured", err=True)
        raise typer.Exit(1)

    async def go():
        for name, a in adapters.items():
            try:
                live = set(await a.list_models())
            except Exception as e:  # noqa: BLE001
                typer.echo(f"{name}: discovery failed: {e}")
                continue
            known = {m.id for m in cat.models if m.provider == name}
            typer.echo(f"{name}: {len(live)} models returned by API")
            for i in sorted(known - live):
                typer.echo(f"  in catalog, NOT in API list (maybe retired/renamed): {i}")
            for i in sorted(live - known)[:50]:
                typer.echo(f"  in API, not in catalog (needs verified pricing/capabilities): {i}")
    asyncio.run(go())


def _load_overrides(cfg: Config) -> dict:
    p = cfg.overrides_path
    return json.loads(p.read_text()) if p and p.exists() else {}


def _save_overrides(cfg: Config, data: dict) -> None:
    p = cfg.overrides_path
    p.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    p.write_text(json.dumps(data, indent=2, sort_keys=True))  # type: ignore[union-attr]


LINE_TIER = {"haiku": 1, "sonnet": 2, "opus": 3, "fable": 3, "mythos": 3}


def sync_patch(ref_known: bool, mid: str, provider: str, info: dict, assume: list[str], today: str, has_tier: bool) -> dict | None:
    """Build an overrides patch from a provider's list endpoint. Never touches prices."""
    life = info.get("lifecycle")
    if life == "retired":
        return None
    caps: dict = {"text": True}
    if info.get("structured_output") is not None:
        caps["structured_output"] = bool(info["structured_output"])
    for a in assume:  # explicit user assertions, recorded as such
        caps[{"streaming": "streaming", "tools": "tool_calling", "tool_calling": "tool_calling", "temperature": "temperature"}[a]] = True
    patch: dict = {"availability": "deprecated" if life == "deprecated" else "available", "verified_on": today,
                   "availability_source_url": f"{provider} models API (live, key-scoped) on {today}", "capabilities": caps}
    lim = {k: v for k, v in (("context_tokens", info.get("context_tokens")), ("max_output_tokens", info.get("max_output_tokens"))) if v}
    if lim:
        patch["limits"] = lim
    if not has_tier and info.get("line") in LINE_TIER:
        patch["tier"] = LINE_TIER[info["line"]]
        patch["tier_note"] = f"suggested from the provider's own '{info['line']}' model line; editorial prior, not a measurement"
    return patch


@catalog_app.command("sync")
def catalog_sync(provider: Optional[str] = typer.Option(None, help="Only this provider (default: every provider with a key)."),
                 assume: str = typer.Option("", help="Capabilities YOU assert after reading the docs, comma-separated: streaming,tools,temperature."),
                 add_new: bool = typer.Option(False, help="Also add models that are not in the catalog yet (pricing stays unknown). Requires --only."),
                 only: str = typer.Option("", help="Comma-separated model-ID substrings to include, e.g. --only gpt-5,o4-mini. Needed with --add-new."),
                 write: bool = typer.Option(False, help="Write to overrides.json. Without it this is a dry run."),
                 catalog: Optional[str] = CAT):
    """Fill availability, token limits, structured-output support and a suggested tier from the provider's live model list.
    Prices are never touched (no provider list endpoint exposes them): set them from the official pricing page."""
    cfg = Config.from_env()
    cat = _catalog(cfg, catalog)
    adapters = {k: v for k, v in build_adapters(cfg).items() if not provider or k == provider}
    if not adapters:
        typer.echo("no API keys configured for that provider", err=True)
        raise typer.Exit(1)
    only_l = [o.strip().lower() for o in only.split(",") if o.strip()]
    if add_new and not only_l:
        typer.echo("--add-new needs --only <id substrings> so you don't import every model the API lists "
                   "(embeddings, audio, image, legacy...). Dry-run `modelspend catalog discover` first to see the IDs.", err=True)
        raise typer.Exit(2)
    asm = [a.strip() for a in assume.split(",") if a.strip()]
    bad = [a for a in asm if a not in ("streaming", "tools", "tool_calling", "temperature")]
    if bad:
        typer.echo(f"--assume accepts streaming,tools,temperature (got {bad})", err=True)
        raise typer.Exit(2)
    today = str(date.today())
    ov = _load_overrides(cfg)

    async def go():
        n = 0
        for name, a in adapters.items():
            try:
                info = await a.list_model_info()
            except Exception as e:  # noqa: BLE001
                typer.echo(f"{name}: sync failed: {e}")
                continue
            for mid, i in sorted(info.items()):
                ref = f"{name}/{mid}"
                known = ref in {m.ref for m in cat.models}
                if not known and not add_new:
                    continue
                if only_l and not any(o in mid.lower() for o in only_l):
                    continue
                has_tier = known and cat.get(ref).tier is not None
                patch = sync_patch(known, mid, name, i, asm, today, has_tier)
                if patch is None:
                    continue
                n += 1
                lim = patch.get("limits", {})
                typer.echo(f"{'update' if known else 'add   '} {ref:<44} ctx {lim.get('context_tokens', '?')} out {lim.get('max_output_tokens', '?')} "
                           f"tier {patch.get('tier', 'kept' if has_tier else '?')} {patch['availability']}")
                if write:
                    if known:
                        ov.setdefault("models", {}).setdefault(ref, {})
                        cur = ov["models"][ref]
                        cur.update({k: v for k, v in patch.items() if k != "capabilities"})
                        cur.setdefault("capabilities", {}).update(patch["capabilities"])
                    else:
                        ov.setdefault("add_models", [])
                        ov["add_models"] = [m for m in ov["add_models"] if f"{m['provider']}/{m['id']}" != ref]
                        ov["add_models"].append({"provider": name, "id": mid, "display_name": i.get("display_name"), **patch})
        return n
    n = asyncio.run(go())
    if write and n:
        _save_overrides(cfg, ov)
        typer.echo(f"wrote {n} model(s) to {cfg.overrides_path}")
    elif n:
        typer.echo("dry run: add --write to save. Then set what the API cannot tell you (see `modelspend catalog set`).")
    else:
        typer.echo("nothing to sync: no catalog models matched the API's list (use --add-new to add unknown ones)")


@catalog_app.command("set")
def catalog_set(ref: str, assignments: list[str] = typer.Argument(..., help="dotted.key=json-value, e.g. tier=2 capabilities.streaming=true pricing.input_per_mtok=3")):
    """Patch one model in overrides.json (validated by reloading the catalog; reverted if invalid)."""
    cfg = Config.from_env()
    cat = Catalog.from_config(cfg)
    if ref not in {m.ref for m in cat.models}:
        typer.echo(f"unknown model '{ref}' (see `modelspend catalog list`)", err=True)
        raise typer.Exit(2)
    ov = _load_overrides(cfg)
    before = json.dumps(ov, sort_keys=True)
    patch = ov.setdefault("models", {}).setdefault(ref, {})
    for a in assignments:
        k, _, v = a.partition("=")
        try:
            val = json.loads(v)
        except json.JSONDecodeError:
            val = v
        d = patch
        *parts, last = k.split(".")
        for p in parts:
            d = d.setdefault(p, {})
        d[last] = val
    _save_overrides(cfg, ov)
    try:
        Catalog.from_config(cfg)
    except Exception as e:  # noqa: BLE001
        _save_overrides(cfg, json.loads(before))
        typer.echo(f"invalid change, reverted: {str(e)[:300]}", err=True)
        raise typer.Exit(2)
    typer.echo(f"updated {ref} in {cfg.overrides_path}")


@catalog_app.command("price")
def catalog_price(ref: str,
                  input: float = typer.Option(..., "--input", help="USD per 1M input tokens, from the provider's official pricing page."),
                  output: float = typer.Option(..., "--output", help="USD per 1M output tokens."),
                  cached: Optional[float] = typer.Option(None, "--cached", help="USD per 1M cached-input (cache read) tokens, if the page lists it."),
                  cache_write: Optional[float] = typer.Option(None, "--cache-write", help="USD per 1M cache-write tokens, if the page lists it."),
                  effective_date: Optional[str] = typer.Option(None, "--effective-date", help="yyyy-mm-dd, if the page states one."),
                  reasoning_separate: bool = typer.Option(False, "--reasoning-separate", help="Provider reports reasoning tokens SEPARATELY from output tokens (billed as output).")):
    """Set a model's prices in one command. Stamps today as the verification date; the source URL is the provider's configured pricing page."""
    parts = [f"pricing.input_per_mtok={input}", f"pricing.output_per_mtok={output}", f'pricing.verified_on="{date.today()}"']
    if cached is not None:
        parts.append(f"pricing.cached_input_per_mtok={cached}")
    if cache_write is not None:
        parts.append(f"pricing.cache_write_per_mtok={cache_write}")
    if effective_date:
        parts.append(f'pricing.effective_date="{effective_date}"')
    if reasoning_separate:
        parts.append("pricing.reasoning_in_output=false")
    catalog_set(ref, parts)
    typer.echo(f"priced {ref}: in ${input}/M, out ${output}/M (verified {date.today()}). Make sure you copied these from the official page.")


@catalog_app.command("template")
def catalog_template(ref: str):
    """Print an `add_models` skeleton for overrides.json. Fill every field from the provider's official docs."""
    prov, _, mid = ref.partition("/")
    price_url = (resolve_sources(Config.from_env()).get(prov) or Source(prov, "<official pricing page>", "")).pricing_url
    typer.echo(json.dumps({"add_models": [{
        "provider": prov, "id": mid, "availability": "available", "verified_on": str(date.today()),
        "availability_source_url": "<official models page URL>",
        "limits": {"context_tokens": None, "max_output_tokens": None},
        "capabilities": {"text": True, "streaming": None, "structured_output": None, "tool_calling": None, "temperature": None,
                         "reasoning": None},
        "pricing": {"currency": "USD", "input_per_mtok": None, "output_per_mtok": None, "cached_input_per_mtok": None,
                    "cache_write_per_mtok": None, "effective_date": "<yyyy-mm-dd>", "verified_on": str(date.today()),
                    "source_url": price_url, "reasoning_in_output": True, "conditions": []},
        "tier": None, "tier_note": "editorial prior: 1 small/fast, 2 mid, 3 frontier"}]}, indent=2))


# ---------------------------------------------------------------- eval
@eval_app.command("run")
def eval_run(mode: str = typer.Option("mock", help="mock (simulated, free) or live (spends money)."),
             providers: Optional[str] = None, premium: Optional[str] = typer.Option(None, help="provider/model for the premium baseline."),
             budget: Optional[float] = typer.Option(None, help="REQUIRED for live: hard USD ceiling across all arms."),
             split: str = "heldout", fixtures: Optional[Path] = None, sandbox: bool = typer.Option(False, help="Also run sandbox_validators in Docker."),
             out: Optional[Path] = None, catalog: Optional[str] = None):
    """Compare premium baseline vs cheapest-eligible vs ModelSpend on identical tasks."""
    from .eval import evaluate, load_tasks, render_markdown
    from .eval.sim import build_sim_engine
    cfg = Config.from_env()
    tasks = load_tasks(split, fixtures)
    provs = _providers(providers, cfg) or ["openai", "anthropic", "google", "xai"]
    if mode == "mock":
        eng = build_sim_engine(Catalog.load(shipped_catalog_path("synthetic_test_catalog.json")), tasks, providers=provs)
    elif mode == "live":
        if not budget or budget <= 0:
            typer.echo("live evaluation spends real money: pass --budget <USD> (hard ceiling).", err=True)
            raise typer.Exit(2)
        eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), None)
        missing = [p for p in provs if p not in eng.adapters]
        if missing:
            typer.echo(f"no API key for: {', '.join(missing)}", err=True)
            raise typer.Exit(2)
        if not premium:
            typer.echo("live evaluation needs an explicit --premium provider/model (your fixed premium baseline).", err=True)
            raise typer.Exit(2)
    else:
        typer.echo("mode must be mock or live", err=True)
        raise typer.Exit(2)
    try:
        rep = asyncio.run(evaluate(eng, tasks, provs, premium, budget if mode == "live" else None, "simulated" if mode == "mock" else "live", sandbox))
    except (ModelSpendError, ValueError) as e:
        typer.echo(f"error: {getattr(e, 'message', e)}", err=True)
        raise typer.Exit(2)
    md = render_markdown(rep)
    typer.echo(md)
    if out:
        out.write_text(md)
        out.with_suffix(".json").write_text(json.dumps(rep, indent=2, default=str))
        typer.echo(f"wrote {out} and {out.with_suffix('.json')}", err=True)


@eval_app.command("calibrate")
def eval_calibrate(mode: str = "mock", providers: Optional[str] = None, budget: Optional[float] = None,
                   fixtures: Optional[Path] = None, sandbox: bool = False, catalog: Optional[str] = None,
                   write: bool = typer.Option(False, help="Merge results into the evidence overlay file (live mode only).")):
    """Measure per-model, per-category pass counts on the CALIBRATION split and (optionally) store them as routing evidence."""
    from .eval import calibrate, load_tasks
    from .eval.sim import build_sim_engine
    cfg = Config.from_env()
    tasks = load_tasks("calibration", fixtures)
    provs = _providers(providers, cfg) or ["openai", "anthropic", "google", "xai"]
    if mode == "mock":
        eng = build_sim_engine(Catalog.load(shipped_catalog_path("synthetic_test_catalog.json")), tasks, providers=provs)
    else:
        if not budget or budget <= 0:
            typer.echo("live calibration spends real money: pass --budget <USD>.", err=True)
            raise typer.Exit(2)
        eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), None)
    ev, spent, stopped = asyncio.run(calibrate(eng, tasks, provs, budget if mode == "live" else None, sandbox))
    typer.echo(("SIMULATED " if mode == "mock" else "") + f"calibration spend ${spent:.6f}" + (f" | {stopped}" if stopped else ""))
    for ref, cats in sorted(ev.items()):
        typer.echo(f"  {ref}: " + ", ".join(f"{c} {e.passes}/{e.n}" for c, e in sorted(cats.items())))
    if write and mode == "live":
        write_evidence(cfg.evidence_path, ev)  # type: ignore[arg-type]
        typer.echo(f"evidence merged into {cfg.evidence_path}")
    elif write:
        typer.echo("not writing simulated evidence", err=True)
    typer.echo("Note: evidence counts only influence routing once n meets the profile minimum (see `modelspend policy`).")


# ---------------------------------------------------------------- keys
def _prov(p: str) -> str:
    from .types import normalize_provider
    try:
        return normalize_provider(p)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)


@keys_app.command("set")
def keys_set(provider: str, key: Optional[str] = typer.Option(None, "--key", help="Avoid: it lands in your shell history. Omit to be prompted (input hidden)."),
             stdin: bool = typer.Option(False, "--stdin", help="Read the key from standard input (for scripts).")):
    """Save a provider's API key to ~/.modelspend/keys.env (chmod 600). A real environment variable still overrides it."""
    prov = _prov(provider)
    cfg = Config.from_env()
    if stdin:
        key = sys.stdin.readline().strip()
    elif key is None:
        key = typer.prompt(f"{prov} API key (input hidden)", hide_input=True).strip()
    if not key:
        typer.echo("empty key; nothing saved", err=True)
        raise typer.Exit(2)
    data = read_keys_file(cfg.home)
    data[KEY_ENV[prov][0]] = key
    p = write_keys_file(cfg.home, data)
    typer.echo(f"saved {prov} key to {p} (readable only by you). Next: modelspend keys test {prov}")
    if cfg.key_sources.get(prov) == "environment":
        typer.echo(f"note: {KEY_ENV[prov][0]} is set in your environment and takes precedence over the saved key.")


@keys_app.command("list")
def keys_list():
    """Which providers have a key, and where it comes from. Keys are never printed."""
    cfg = Config.from_env()
    for p in PROVIDERS_ORDER:
        src = cfg.key_sources.get(p)
        typer.echo(f"{p:<10}{'set (' + src + ')' if src else 'not set':<26}env var: {' or '.join(KEY_ENV[p])}")
    typer.echo(f"\nkeys file: {keys_path(cfg.home)} ({'exists' if keys_path(cfg.home).exists() else 'not created yet'})")


@keys_app.command("remove")
def keys_remove(provider: str):
    """Delete a provider's key from the keys file (does not touch your environment)."""
    prov = _prov(provider)
    cfg = Config.from_env()
    data = read_keys_file(cfg.home)
    gone = [n for n in KEY_ENV[prov] if data.pop(n, None) is not None]
    write_keys_file(cfg.home, data)
    typer.echo(f"removed {prov} key from the keys file" if gone else f"no {prov} key in the keys file")


@keys_app.command("test")
def keys_test(provider: Optional[str] = typer.Argument(None)):
    """Check that a key works by listing the provider's models (a read-only call; no tokens are generated)."""
    cfg = Config.from_env()
    adapters = build_adapters(cfg)
    targets = [_prov(provider)] if provider else list(adapters)
    if not targets:
        typer.echo("no keys configured; run: modelspend keys set <provider>", err=True)
        raise typer.Exit(1)
    bad = 0

    async def go():
        nonlocal bad
        for p in targets:
            if p not in adapters:
                typer.echo(f"{p:<10}no key configured")
                bad += 1
                continue
            try:
                n = len(await adapters[p].list_models())
                typer.echo(f"{p:<10}OK ({n} models visible to this key)")
            except Exception as e:  # noqa: BLE001
                typer.echo(f"{p:<10}FAILED: {getattr(e, 'kind', type(e).__name__)} ({getattr(e, 'message', str(e))[:100]})")
                bad += 1
    asyncio.run(go())
    raise typer.Exit(1 if bad else 0)


PROVIDERS_ORDER = ("anthropic", "openai", "google", "xai")


# ---------------------------------------------------------------- providers / models / status
def routability(m, cfg: Config) -> list[str]:
    """Why a model would NOT be routed right now ([] = routable). Mirrors the router's hard constraints."""
    why = []
    if m.provider not in cfg.api_keys:
        why.append("no API key")
    if cfg.default_providers and m.provider not in cfg.default_providers:
        why.append("provider not enabled")
    if m.ref in cfg.deny_models or (cfg.allow_models and m.ref not in cfg.allow_models):
        why.append("turned off by you")
    if m.availability != "available":
        why.append(f"availability {m.availability} (run `modelspend catalog sync`)" if m.availability == "unverified" else f"availability {m.availability}")
    if (m.pricing is None or not m.pricing.known) and not cfg.allow_unpriced:
        why.append("no pricing (see `modelspend catalog set`, or MODELSPEND_ALLOW_UNPRICED)")
    if m.limits.context_tokens is None or m.limits.max_output_tokens is None:
        why.append("limits unknown")
    if m.tier is None and not m.evidence:
        why.append("no tier")
    return why


def _save_list(cfg: Config, key: str, values: list[str]) -> None:
    s = load_settings(cfg.home)
    s[key] = sorted(set(values))
    if not s[key]:
        s.pop(key)
    save_settings(cfg.home, s)


@providers_app.command("list")
def providers_list(catalog: Optional[str] = CAT):
    cfg = Config.from_env()
    cat = _catalog(cfg, catalog)
    typer.echo(f"{'provider':<11}{'key':<8}{'enabled':<13}{'models':>7}{'routable':>10}")
    for p in PROVIDERS_ORDER:
        ms = [m for m in cat.models if m.provider == p]
        ok = [m for m in ms if not routability(m, cfg)]
        enabled = ("yes" if p in cfg.default_providers else "no") if cfg.default_providers else "per request"
        typer.echo(f"{p:<11}{'yes' if p in cfg.api_keys else 'no':<8}{enabled:<13}{len(ms):>7}{len(ok):>10}")
    if cfg.default_providers:
        typer.echo(f"\ndefault allowlist: {', '.join(cfg.default_providers)}")
    else:
        typer.echo("\nno default allowlist set: requests must name providers. Set one: modelspend providers enable anthropic openai")


@providers_app.command("enable")
def providers_enable(providers: list[str] = typer.Argument(...)):
    """Add providers to the default allowlist (used when a request does not name providers)."""
    cfg = Config.from_env()
    new = [_prov(p) for p in providers]
    s = load_settings(cfg.home)
    _save_list(cfg, "providers", list(s.get("providers", [])) + new)
    typer.echo(f"default providers: {', '.join(sorted(set(s.get('providers', [])) | set(new)))}  (saved in {settings_path(cfg.home)})")
    if "MODELSPEND_PROVIDERS" in __import__("os").environ:
        typer.echo("note: MODELSPEND_PROVIDERS is set in your environment and overrides this setting.")
    for p in new:
        if p not in cfg.api_keys:
            typer.echo(f"note: no API key for {p} yet: modelspend keys set {p}")


@providers_app.command("disable")
def providers_disable(providers: list[str] = typer.Argument(...)):
    cfg = Config.from_env()
    drop = {_prov(p) for p in providers}
    s = load_settings(cfg.home)
    keep = [p for p in s.get("providers", []) if p not in drop]
    _save_list(cfg, "providers", keep)
    typer.echo(f"default providers: {', '.join(keep) or '(none)'}")


@models_app.command("list")
def models_list(provider: Optional[str] = None, catalog: Optional[str] = CAT):
    """Every catalog model, whether it is routable now, and if not, why."""
    cfg = Config.from_env()
    cat = _catalog(cfg, catalog)
    for m in cat.models:
        if provider and m.provider != _prov(provider):
            continue
        why = routability(m, cfg)
        typer.echo(f"{'ROUTABLE' if not why else 'off     '}  {m.ref:<42} tier {m.tier or '-'}  " + ("" if not why else "(" + "; ".join(why) + ")"))


@models_app.command("disable")
def models_disable(refs: list[str] = typer.Argument(..., help="provider/model-id, e.g. anthropic/claude-opus-5-5")):
    """Never route to these models (stored in settings.json; the catalog is untouched)."""
    cfg = Config.from_env()
    cat = _catalog(cfg, None)
    unknown = [r for r in refs if r not in {m.ref for m in cat.models}]
    if unknown:
        typer.echo(f"unknown model(s): {', '.join(unknown)} (see `modelspend models list`)", err=True)
        raise typer.Exit(2)
    _save_list(cfg, "deny_models", cfg.deny_models + refs)
    typer.echo(f"turned off: {', '.join(refs)}")


@models_app.command("enable")
def models_enable(refs: list[str] = typer.Argument(...)):
    cfg = Config.from_env()
    _save_list(cfg, "deny_models", [r for r in cfg.deny_models if r not in refs])
    typer.echo(f"turned on: {', '.join(refs)}")


@app.command()
def status(catalog: Optional[str] = CAT):
    """One-screen overview: keys, enabled providers, routable models, pricing sources."""
    cfg = Config.from_env()
    cat = _catalog(cfg, catalog)
    typer.echo(f"home: {cfg.home}   catalog {cat.version}   policy {POLICY_VERSION}")
    typer.echo("\nproviders:")
    providers_list(catalog)
    routable = [m for m in cat.models if not routability(m, cfg)]
    typer.echo(f"\nroutable models ({len(routable)}):")
    for m in routable:
        typer.echo(f"  {m.ref}")
    if not routable:
        typer.echo("  none yet. Typical fixes: modelspend keys set <provider>; modelspend catalog sync --provider <provider> --write")
    typer.echo("\npricing sources (change with `modelspend sources set <provider> <url>`):")
    for p, s in sorted(resolve_sources(cfg).items(), key=lambda kv: PROVIDERS_ORDER.index(kv[0])):
        typer.echo(f"  {p:<10}{s.pricing_url}  [{s.origin}]")


# ---------------------------------------------------------------- sources
@sources_app.command("list")
def sources_list():
    cfg = Config.from_env()
    snaps = load_snapshots(cfg)
    for p, s in sorted(resolve_sources(cfg).items(), key=lambda kv: PROVIDERS_ORDER.index(kv[0])):
        snap = snaps.get(p)
        base = f"baseline accepted {snap['accepted_on']}" if snap and snap.get("url") == s.pricing_url else "no baseline"
        typer.echo(f"{p:<10}{s.pricing_url}\n          origin: {s.origin} | {base}")


@sources_app.command("set")
def sources_set(provider: str, url: str):
    """Point a provider at a different pricing page. Tracking parameters (utm_*) are stripped."""
    prov = _prov(provider)
    try:
        stored = set_user_source(Config.from_env(), prov, url)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    typer.echo(f"{prov} pricing URL -> {stored}" + ("  (tracking parameters removed)" if stored != url.strip() else ""))
    typer.echo("Catalog entries without their own source_url now cite this page. Re-verify prices against it, then `modelspend sources check --accept`.")


@sources_app.command("reset")
def sources_reset(provider: str):
    """Go back to the shipped URL for a provider."""
    prov = _prov(provider)
    set_user_source(Config.from_env(), prov, None)
    typer.echo(f"{prov}: using {resolve_sources(Config.from_env(), env={})[prov].pricing_url}")


@sources_app.command("check")
def sources_check(provider: Optional[str] = typer.Argument(None),
                  accept: bool = typer.Option(False, "--accept", help="Record the page's current content as the baseline (after you verified prices against it).")):
    """Fetch each pricing page and say whether it CHANGED since your last accepted baseline. Never edits prices."""
    cfg = Config.from_env()
    srcs = resolve_sources(cfg)
    targets = [srcs[_prov(provider)]] if provider else [srcs[p] for p in PROVIDERS_ORDER if p in srcs]
    changed = 0
    for s in targets:
        r = check_source(cfg, s, accept=accept)
        extra = f" | redirected to {r['final_url']}" if r.get("redirected") else ""
        typer.echo(f"{s.provider:<10}{r['state']:<12}{s.pricing_url}{extra}" + (f" ({r['detail']})" if r.get("detail") else "")
                   + (" | baseline saved" if r.get("accepted") else ""))
        changed += r["state"] == "CHANGED"
    if changed:
        typer.echo("\nA page changed: re-verify its prices, update the catalog (`modelspend catalog set ...`), then run `sources check --accept`.")
    raise typer.Exit(1 if changed else 0)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
