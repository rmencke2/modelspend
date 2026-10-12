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
from .config import Config, parse_env_file
from .engine import Engine
from .errors import ModelSpendError
from .policy import BASE_TIER, PROFILES
from .providers import build_adapters
from .store import Store
from .types import RouteRequest, TaskCategory

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ModelSpend: route each task to the lowest-cost suitable model.")
catalog_app = typer.Typer(no_args_is_help=True, help="Inspect, validate and extend the model catalog.")
eval_app = typer.Typer(no_args_is_help=True, help="Evaluation harness (mock by default; live is opt-in with a budget).")
app.add_typer(catalog_app, name="catalog")
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


def _request(prompt: Optional[str], providers: Optional[str], profile: str, budget: Optional[float], max_out: Optional[int],
             task_type: Optional[str], schema: Optional[Path], system: Optional[str], hard: bool, cfg: Config) -> RouteRequest:
    if prompt is None or prompt == "-":
        prompt = sys.stdin.read()
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    return RouteRequest(
        messages=msgs, providers=_providers(providers, cfg), profile=profile, max_budget_usd=budget, max_output_tokens=max_out,
        task_type=TaskCategory(task_type) if task_type else None, hard=hard,
        response_schema=json.loads(schema.read_text()) if schema else None)


@app.command()
def preview(prompt: Optional[str] = typer.Argument(None, help="Prompt text, or '-' for stdin."),
            providers: Optional[str] = typer.Option(None, help="Comma-separated allowlist, e.g. openai,anthropic."),
            profile: str = "balanced", budget: Optional[float] = typer.Option(None, help="Max total USD for the request."),
            max_output_tokens: Optional[int] = None, task_type: Optional[str] = None, schema: Optional[Path] = None,
            system: Optional[str] = None, hard: bool = False, assume_keys: bool = typer.Option(False, help="Plan as if keys exist for the allowlisted providers (no calls are made either way)."),
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
        plan = asyncio.run(eng.plan(_request(prompt, providers, profile, budget, max_output_tokens, task_type, schema, system, hard, cfg)))
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
    typer.echo(f"{'#':<3}{'model':<44}{'tier':<5}{'basis':<9}{'est. cost':>12}{'worst case':>13}")
    for c in plan.candidates:
        typer.echo(f"{c.rank:<3}{c.provider + '/' + c.model:<44}{c.tier or '-':<5}{c.suitability_basis:<9}${c.expected_cost_usd:>10.6f} ${c.worst_case_cost_usd:>10.6f}")
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
          as_json: bool = typer.Option(False, "--json")):
    """Route a task to a model and run it (spends money with your API keys)."""
    cfg = Config.from_env()
    eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), Store(cfg.db_path))  # type: ignore[arg-type]
    try:
        res = asyncio.run(eng.run(_request(prompt, providers, profile, budget, max_output_tokens, task_type, schema, system, hard, cfg)))
    except ModelSpendError as e:
        _fail(e)
    if as_json:
        typer.echo(res.model_dump_json(indent=2))
        return
    typer.echo(res.text)
    typer.echo(f"\n--- {res.provider}/{res.model} | {res.status} | validation: {res.validation.status}", err=True)
    typer.echo(f"--- est ${res.estimated_cost_usd:.6f} -> actual ${res.actual_cost_usd:.6f} (+${res.uncertain_cost_usd:.6f} uncertain) | "
               f"in {res.usage.input_tokens + res.usage.cache_read_tokens + res.usage.cache_write_tokens} out {res.usage.output_tokens} | "
               f"retries {res.retries} failovers {res.failovers} escalations {res.escalations}", err=True)
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
    typer.echo(f"requests {t['n']} | actual ${t['act']:.6f} | estimated ${t['est']:.6f} | uncertain ${t['unc']:.6f} | escalation rate {s['escalation_rate']*100:.1f}%")
    for title, rows, key in (("by provider", s["by_provider"], "provider"), ("by task", s["by_task"], "task")):
        typer.echo(f"\n{title}:")
        for r in rows:
            typer.echo(f"  {r[key]:<14}{r['n']:>6}  ${r['cost'] or 0:.6f}")


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


@catalog_app.command("template")
def catalog_template(ref: str):
    """Print an `add_models` skeleton for overrides.json. Fill every field from the provider's official docs."""
    prov, _, mid = ref.partition("/")
    typer.echo(json.dumps({"add_models": [{
        "provider": prov, "id": mid, "availability": "available", "verified_on": str(date.today()),
        "availability_source_url": "<official models page URL>",
        "limits": {"context_tokens": None, "max_output_tokens": None},
        "capabilities": {"text": True, "streaming": None, "structured_output": None, "tool_calling": None, "temperature": None,
                         "reasoning": None},
        "pricing": {"currency": "USD", "input_per_mtok": None, "output_per_mtok": None, "cached_input_per_mtok": None,
                    "cache_write_per_mtok": None, "effective_date": "<yyyy-mm-dd>", "verified_on": str(date.today()),
                    "source_url": "<official pricing page URL>", "reasoning_in_output": True, "conditions": []},
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
