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

from . import POLICY_VERSION, registry
from .catalog import Catalog, shipped_catalog_path, write_evidence
from .config import (
    Config, keys_path, load_settings, parse_env_file, read_keys_file, save_settings, settings_path, write_keys_file,
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
thresholds_app = typer.Typer(no_args_is_help=True, help="Your own evidence bars: how much measured proof a model needs, per task category, before ModelSpend picks it.")
app.add_typer(thresholds_app, name="thresholds")
alias_app = typer.Typer(no_args_is_help=True, help="Name the model you want for each kind of work (e.g. coding, classify) and use the name as --model / model=.")
app.add_typer(alias_app, name="alias")
budget_app = typer.Typer(no_args_is_help=True, help="Spending limits per project and/or agent (daily or monthly, in USD and/or tokens).")
app.add_typer(budget_app, name="budget")
shadow_app = typer.Typer(no_args_is_help=True, help="Learn from your own traffic which cheaper model is safe to use (opt-in, capped, off by default).")
app.add_typer(shadow_app, name="shadow")
reconcile_app = typer.Typer(no_args_is_help=True, help="Compare ModelSpend's usage with what the provider reports (import an export you download yourself).")
app.add_typer(reconcile_app, name="reconcile")
app.add_typer(catalog_app, name="catalog")
app.add_typer(keys_app, name="keys")
app.add_typer(providers_app, name="providers")
app.add_typer(models_app, name="models")
pricing_app = typer.Typer(no_args_is_help=True, help="Refresh prices from the providers' pricing pages: fetch, extract, verify, then review and apply (cron-friendly).")
app.add_typer(sources_app, name="sources")
app.add_typer(pricing_app, name="pricing")
app.add_typer(eval_app, name="eval")

SYNTH_BANNER = "[SYNTHETIC CATALOG: fictional models and prices; never use for real routing]"


def _version(value: bool) -> None:
    if value:
        from . import __version__
        typer.echo(f"modelspend {__version__}")
        raise typer.Exit()


@app.callback()
def _root(env_file: Optional[Path] = typer.Option(None, "--env-file", help="KEY=VALUE file with API keys (never stored or logged)."),
          version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version and exit.")):
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


PROJECT = typer.Option(None, "--project", envvar="MODELSPEND_PROJECT", help="Attribute this call to a project (see `modelspend report`).")
AGENT = typer.Option(None, "--agent", envvar="MODELSPEND_AGENT", help="Attribute this call to an agent.")
RUN_ID = typer.Option(None, "--run-id", help="Group several calls into one agent run.")
LABEL = typer.Option(None, "--label", help="Extra tag as key=value (repeatable, max 8), e.g. --label env=prod.")
PIN = typer.Option(None, "--model", help="Observe-only: run exactly this 'provider/model-id' with no routing, failover or escalation; tokens and cost are still recorded.")


def _labels(items: Optional[list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        if "=" not in it:
            raise typer.BadParameter(f"--label expects key=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _request(prompt: Optional[str], providers: Optional[str], profile: str, budget: Optional[float], max_out: Optional[int],
             task_type: Optional[str], schema: Optional[Path], system: Optional[str], hard: bool, cfg: Config,
             budget_tokens: Optional[int] = None, allow_unpriced: bool = False,
             project: Optional[str] = None, agent: Optional[str] = None, run_id: Optional[str] = None,
             label: Optional[list[str]] = None, pin: Optional[str] = None) -> RouteRequest:
    if prompt is None or prompt == "-":
        prompt = sys.stdin.read()
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    if pin:
        from .aliases import resolve as resolve_alias
        pin = resolve_alias(pin, cfg.aliases)  # --model coding -> the model that alias points to
    if pin and not providers:
        providers = pin.split("/", 1)[0] if "/" in pin else None  # a pinned model implies its provider
    return RouteRequest(
        messages=msgs, providers=_providers(providers, cfg), profile=profile, max_budget_usd=budget, max_output_tokens=max_out,
        max_budget_tokens=budget_tokens, allow_unpriced=allow_unpriced or cfg.allow_unpriced,
        task_type=TaskCategory(task_type) if task_type else None, hard=hard,
        response_schema=json.loads(schema.read_text()) if schema else None,
        project=project, agent=agent, run_id=run_id, labels=_labels(label), pin=pin)


@app.command()
def preview(prompt: Optional[str] = typer.Argument(None, help="Prompt text, or '-' for stdin."),
            providers: Optional[str] = typer.Option(None, help="Comma-separated allowlist, e.g. openai,anthropic."),
            profile: str = "balanced", budget: Optional[float] = typer.Option(None, help="Max total USD for the request."),
            max_output_tokens: Optional[int] = None, task_type: Optional[str] = None, schema: Optional[Path] = None,
            system: Optional[str] = None, hard: bool = False, budget_tokens: Optional[int] = typer.Option(None, help="Max TOTAL tokens (input+output) across all attempts."),
            allow_unpriced: bool = typer.Option(False, help="Include models with no catalog pricing; cost is shown as unknown, tokens still tracked."),
            assume_keys: bool = typer.Option(False, help="Plan as if keys exist for the allowlisted providers (no calls are made either way)."),
            model: Optional[str] = PIN,
            catalog: Optional[str] = CAT, as_json: bool = typer.Option(False, "--json")):
    """Estimate candidates and costs without making any generation call."""
    cfg = Config.from_env()
    if assume_keys:
        from .aliases import resolve as resolve_alias
        pinned = resolve_alias(model, cfg.aliases) if model else None
        for p in _providers(providers or (pinned.split("/", 1)[0] if pinned and "/" in pinned else None), cfg):  # a pinned model implies its provider
            cfg.api_keys.setdefault(p, "preview-only")
    cat = _catalog(cfg, catalog)
    eng = Engine(cat, cfg, {}, Store(cfg.db_path) if cfg.shadow.use_as_evidence else None, shadow=False)  # a store only so `shadow apply` evidence is seen
    eng.router.available = set(cfg.api_keys)
    try:
        plan = asyncio.run(eng.plan(_request(prompt, providers, profile, budget, max_output_tokens, task_type, schema, system, hard, cfg, budget_tokens, allow_unpriced,
                                             pin=model)))
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
          project: Optional[str] = PROJECT, agent: Optional[str] = AGENT, run_id: Optional[str] = RUN_ID,
          label: Optional[list[str]] = LABEL, model: Optional[str] = PIN,
          as_json: bool = typer.Option(False, "--json")):
    """Route a task to a model and run it (spends money with your API keys)."""
    cfg = Config.from_env()
    eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), Store(cfg.db_path), shadow=False)  # type: ignore[arg-type]
    try:
        res = asyncio.run(eng.run(_request(prompt, providers, profile, budget, max_output_tokens, task_type, schema, system, hard, cfg, budget_tokens, allow_unpriced,
                                                 project, agent, run_id, label, model)))
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
    for w in res.budget_warnings:
        typer.echo(f"--- BUDGET: {w}", err=True)
    typer.echo(f"--- {res.explanation}", err=True)


@app.command()
def serve(host: Optional[str] = None, port: Optional[int] = None, catalog: Optional[str] = CAT,
          background: bool = typer.Option(False, "--background", "-d", help="Run detached (log: ~/.modelspend/server.log). Stop it with `modelspend stop`.")):
    """Run the local API server (127.0.0.1 by default; non-loopback requires MODELSPEND_API_TOKEN)."""
    import uvicorn

    from .server import runner
    from .server.app import check_bind, create_app
    cfg = Config.from_env()
    cfg.host, cfg.port = host or cfg.host, port or cfg.port
    check_bind(cfg.host, cfg.api_token)
    if background:
        ok, msg = runner.start_background(cfg, host, port, catalog)
        typer.echo(msg, err=not ok)
        raise typer.Exit(0 if ok else 1)
    st = runner.server_state(cfg)
    if st.running:
        typer.echo(f"a ModelSpend server is already running (pid {st.pid}) on port {st.port}. Stop it with `modelspend stop`, or use `modelspend restart`.", err=True)
        raise typer.Exit(1)
    eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), Store(cfg.db_path))  # type: ignore[arg-type]
    if not eng.adapters:
        typer.echo("warning: no provider API keys found in the environment; requests will have no eligible models", err=True)
    runner.write_pidfile(cfg, cfg.host, cfg.port)
    try:
        uvicorn.run(create_app(eng, cfg, reload_catalog=lambda: _catalog(cfg, catalog)), host=cfg.host, port=cfg.port, log_level="info")
    finally:
        runner.remove_pidfile(cfg, only_if_pid=os.getpid())


@app.command()
def stop(timeout: float = typer.Option(10.0, help="Seconds to wait for a graceful shutdown."),
         force: bool = typer.Option(False, "--force", help="Kill it if it has not exited after the timeout (or if the PID file looks stale).")):
    """Stop the local ModelSpend server (started with `serve`, `serve --background`, or by an older version)."""
    from .server import runner
    ok, msg = runner.stop_server(Config.from_env(), timeout=timeout, force=force)
    typer.echo(msg, err=not ok)
    raise typer.Exit(0 if ok else 1)


@app.command()
def restart(host: Optional[str] = None, port: Optional[int] = None, catalog: Optional[str] = CAT):
    """Stop the server (if running) and start it again in the background, e.g. after `git pull`."""
    from .server import runner
    cfg = Config.from_env()
    cfg.host, cfg.port = host or cfg.host, port or cfg.port
    ok, msg = runner.stop_server(cfg)
    if not ok:
        typer.echo(msg, err=True)
        raise typer.Exit(1)
    typer.echo(msg)
    ok, msg = runner.start_background(cfg, host, port, catalog)
    typer.echo(msg, err=not ok)
    raise typer.Exit(0 if ok else 1)


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
def report(by: str = typer.Option("project,agent,model", "--by", help="Group by up to 4 of: project, agent, run, provider, model, task, profile, day, purpose, label.<name>."),
           since: str = typer.Option("30d", help="Window: 7d, 24h, 2w, a date like 2026-10-01, or 'all'."),
           project: Optional[str] = typer.Option(None, help="Only this project ('(untagged)' for calls without one)."),
           agent: Optional[str] = typer.Option(None, help="Only this agent."), run: Optional[str] = typer.Option(None, help="Only this run id."),
           provider: Optional[str] = None, model: Optional[str] = typer.Option(None, help="Only this model, as provider/model-id."),
           task: Optional[str] = None, top: int = typer.Option(50, help="Maximum number of groups to show."),
           fmt: str = typer.Option("table", "--format", help="table, csv or json.")):
    """Tokens and cost per project, agent and model (metadata only; no prompts are stored).

    Only calls made through ModelSpend are counted. Tokens are provider-reported; cost uses the prices in effect when each call ran.
    """
    from .report import ReportError, build_report, parse_since, to_csv, to_table
    if fmt not in ("table", "csv", "json"):
        raise typer.BadParameter("--format must be table, csv or json")
    cfg = Config.from_env()
    store = Store(cfg.db_path)  # type: ignore[arg-type]
    filters = {k: v for k, v in (("project", project), ("agent", agent), ("run", run), ("provider", provider), ("model", model), ("task", task)) if v}
    try:
        rep = build_report(store, [d.strip() for d in by.split(",") if d.strip()], filters=filters, since=parse_since(since), limit=top)
    except ReportError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(rep, indent=2) if fmt == "json" else to_csv(rep) if fmt == "csv" else to_table(rep))


# ---------------------------------------------------------------- thresholds and evidence
@thresholds_app.command("set")
def thresholds_set(category: str = typer.Argument(..., help="A task category (classification, extraction, rewriting, summarization, translation, qa_context, code, analysis) or '*' for all."),
                   min_n: Optional[int] = typer.Option(None, help="Eval cases a model needs for its results to count."),
                   min_pass_rate: Optional[float] = typer.Option(None, help="Pass rate it must show, e.g. 0.95."),
                   basis: Optional[str] = typer.Option(None, help="lower_bound (default: the 95% Wilson lower bound must clear the rate; conservative) or observed (passes / n)."),
                   require_evidence: Optional[bool] = typer.Option(None, "--require-evidence/--allow-prior", help="--require-evidence: never choose a model for this category on its tier prior alone.")):
    """Set (or change fields of) your evidence bar for a category. Unset fields fall back to '*', then to the profile's built-in bar."""
    from .policy import min_samples_for, parse_threshold
    cfg = Config.from_env()
    s = load_settings(cfg.home)
    cur = dict((s.get("thresholds") or {}).get(category.strip().lower(), {}))
    for k, v in (("min_n", min_n), ("min_pass_rate", min_pass_rate), ("basis", basis), ("require_evidence", require_evidence)):
        if v is not None:
            cur[k] = v
    try:
        cat, t = parse_threshold(category, cur)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    th = dict(s.get("thresholds") or {})
    th[cat] = t.as_dict()
    s["thresholds"] = th
    save_settings(cfg.home, s)
    typer.echo(f"threshold for {cat}: " + ", ".join(f"{k}={v}" for k, v in t.as_dict().items()) + f"   (saved in {settings_path(cfg.home)}; restart a running server to apply)")
    if t.min_pass_rate is not None and (t.basis or "lower_bound") == "lower_bound":
        typer.echo(f"note: with the lower_bound basis, a bar of {t.min_pass_rate:g} needs at least {min_samples_for(t.min_pass_rate)} cases even if every one passes. "
                   "Use --basis observed if you want to judge by the plain pass rate.")


@thresholds_app.command("list")
def thresholds_list(profile: str = typer.Option("balanced", help="Show the bars as they apply under this profile.")):
    """Your settings, and the bar that actually applies to each category."""
    from .policy import PROFILES, bar_for
    from .types import Profile, TaskCategory
    cfg = Config.from_env()
    try:
        pol = PROFILES[Profile(profile)]
    except ValueError:
        typer.echo("profile must be economy, balanced or high_assurance", err=True)
        raise typer.Exit(2)
    typer.echo(f"your settings: {', '.join(f'{k}: {v.as_dict()}' for k, v in cfg.thresholds.items()) or 'none (profile defaults apply)'}\n")
    typer.echo(f"effective bars under the {profile} profile:")
    for c in TaskCategory:
        if c != TaskCategory.unknown:
            b = bar_for(pol, c, cfg.thresholds)
            typer.echo(f"  {c.value:<15}{b.describe()}" + ("" if b.source == "profile" else f"   [{b.source}]"))


@thresholds_app.command("remove")
def thresholds_remove(category: str):
    """Delete your bar for a category (the profile's built-in bar applies again)."""
    cfg = Config.from_env()
    s = load_settings(cfg.home)
    th = dict(s.get("thresholds") or {})
    if category.strip().lower() not in th:
        typer.echo(f"no threshold for '{category}'", err=True)
        raise typer.Exit(2)
    del th[category.strip().lower()]
    s["thresholds"] = th
    save_settings(cfg.home, s)
    typer.echo(f"removed threshold for {category}")


@app.command()
def evidence(profile: str = typer.Option("balanced", help="Judge against this profile's bars (plus your own thresholds)."),
             category: Optional[str] = None, model: Optional[str] = typer.Option(None, help="Only this model, as provider/model-id."),
             catalog: Optional[str] = CAT, as_json: bool = typer.Option(False, "--json")):
    """Which models clear your evidence bar for which task category, from measured results (`eval calibrate --write`)."""
    from . import evidence as ev
    cfg = Config.from_env()
    try:
        rep = ev.build(_catalog(cfg, catalog), cfg, profile, category, model)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(rep, indent=2) if as_json else ev.to_table(rep))


# ---------------------------------------------------------------- budgets and retention
def _store() -> Store:
    return Store(Config.from_env().db_path)  # type: ignore[arg-type]


def _pct(p: Optional[float]) -> str:
    return "-" if p is None else f"{p:.0%}"


@alias_app.command("set")
def alias_set(name: str, model: str = typer.Argument(..., help="provider/model-id, e.g. anthropic/claude-sonnet-5-5"),
              force: bool = typer.Option(False, "--force", help="Save even if the model is not in your catalog yet.")):
    """Create or change an alias."""
    from .aliases import set_alias
    cfg = Config.from_env()
    try:
        n, r = set_alias(cfg, name, model, None if force else _catalog(cfg, None))
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(f"alias '{n}' -> {r}   use it as: modelspend route \"...\" --model {n}   or model=\"{n}\" on the local server")


@alias_app.command("list")
def alias_list(as_json: bool = typer.Option(False, "--json")):
    """Show aliases and whether each target model is in the catalog, priced and has a key."""
    from .aliases import describe
    cfg = Config.from_env()
    rows = describe(cfg, _catalog(cfg, None))
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("no aliases. Example: modelspend alias set coding anthropic/claude-sonnet-5-5")
        return
    for r in rows:
        notes = [x for x, ok in (("not in catalog", r["in_catalog"]), ("no price", r["priced"]), ("no API key", r["has_key"])) if not ok]
        typer.echo(f"{r['alias']:<20} -> {r['model']:<42} {'ok' if not notes else '; '.join(notes)}")


@alias_app.command("remove")
def alias_remove(name: str):
    """Delete an alias."""
    from .aliases import remove_alias
    if not remove_alias(Config.from_env(), name):
        typer.echo(f"no alias '{name}'", err=True)
        raise typer.Exit(2)
    typer.echo(f"removed alias '{name}'")


@budget_app.command("set")
def budget_set(project: Optional[str] = typer.Option(None, help="Limit this project (omit for any project)."),
               agent: Optional[str] = typer.Option(None, help="Limit this agent (omit for any agent)."),
               period: str = typer.Option("monthly", help="daily or monthly (UTC calendar day / month)."),
               usd: Optional[float] = typer.Option(None, help="Limit in US dollars (counts only calls on models with a price)."),
               tokens: Optional[int] = typer.Option(None, help="Limit in total tokens (input + output, all calls)."),
               mode: str = typer.Option("hard", help="hard: refuse calls that would exceed it. soft: only warn.")):
    """Create or replace a budget. Needs --usd and/or --tokens. With neither --project nor --agent it covers everything."""
    from .budgets import set_budget
    try:
        b = set_budget(_store(), project=project, agent=agent, period=period, limit_usd=usd, limit_tokens=tokens, mode=mode)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    lim = " and ".join(x for x in (f"${b.limit_usd:,.2f}" if b.limit_usd is not None else "", f"{b.limit_tokens:,} tokens" if b.limit_tokens is not None else "") if x)
    typer.echo(f"budget #{b.id}: {b.period} {b.mode} limit {lim} for {b.label}")


@budget_app.command("list")
def budget_list(as_json: bool = typer.Option(False, "--json")):
    """Show every budget with what has been spent in its current window."""
    from .budgets import status
    rows = status(_store())
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("no budgets set. Example: modelspend budget set --project shop --period monthly --usd 50")
        return
    for r in rows:
        spent = []
        if r["limit_usd"] is not None:
            spent.append(f"${r['spent_usd']:,.4f} of ${r['limit_usd']:,.2f} ({_pct(r['pct_usd'])}{'' if r['usd_complete'] else ', partial: some calls unpriced'})")
        if r["limit_tokens"] is not None:
            spent.append(f"{r['spent_tokens']:,} of {r['limit_tokens']:,} tokens ({_pct(r['pct_tokens'])})")
        flag = {"ok": "ok     ", "warning": "WARNING", "over": "OVER   "}[r["state"]]
        typer.echo(f"#{r['id']:<3} {flag} {r['period']:<8} {r['mode']:<5} {r['scope']:<42} {' | '.join(spent)} | resets {r['resets_at_text']}")


@budget_app.command("remove")
def budget_remove(budget_id: int):
    """Delete a budget by its number (see `budget list`)."""
    from .budgets import remove_budget
    if not remove_budget(_store(), budget_id):
        typer.echo(f"no budget #{budget_id}", err=True)
        raise typer.Exit(2)
    typer.echo(f"removed budget #{budget_id}")


def _write_shadow(cfg: Config, **changes) -> "ShadowSettings":
    from .config import update_shadow_settings
    try:
        return update_shadow_settings(cfg.home, changes)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)


def _shadow_summary(s) -> str:
    return (f"{'ON' if s.enabled else 'OFF'}: {s.sample_rate:.0%} of requests sampled, up to {s.max_candidates} cheaper model(s) each, "
            f"cap ${s.monthly_budget_usd:g}/month, judge {'on' if s.judge else 'off'}, sensitive requests {'included' if s.include_sensitive else 'never shadowed'}, "
            f"routing uses shadow evidence: {'yes' if s.use_as_evidence else 'no'}")


@shadow_app.command("status")
def shadow_status():
    """Settings, this month's shadow spend, and what has been measured so far."""
    from .shadow import aggregate, spent_this_month
    cfg = Config.from_env()
    store = _store()
    s = cfg.shadow
    typer.echo(_shadow_summary(s))
    typer.echo(f"spent this month (UTC): ${spent_this_month(store):.4f} of ${s.monthly_budget_usd:g}")
    rows = aggregate(store)
    if not rows:
        typer.echo("no comparisons yet" + ("" if s.enabled else ": turn it on with `modelspend shadow on`, then restart the server (`modelspend restart`)"))
        return
    typer.echo(f"\n{'task':<16}{'cheaper model':<30}{'scored':>7}{'passed':>7}{'unscored':>9}")
    for r in rows:
        typer.echo(f"{r['category']:<16}{r['provider'] + '/' + r['model']:<30}{r['n']:>7}{r['passes']:>7}{r['unscored']:>9}")
    from .shadow import REASON_TEXT, reason_counts
    typer.echo("\nwhy:")
    for r in reason_counts(store):
        typer.echo(f"  {r['category']:<16}{r['model']:<30}{r['verdict']:<9}{r['n']:>4}  {REASON_TEXT.get(r['reason'], r['reason'])}")
    typer.echo("\nPassed = the cheaper model gave an answer as good as the one you used. Run `modelspend savings` for what that means in dollars.")


@shadow_app.command("on")
def shadow_on():
    """Start comparing cheaper models on a sample of your real requests (restart the server afterwards)."""
    s = _write_shadow(Config.from_env(), enabled=True)
    typer.echo(_shadow_summary(s))
    typer.echo("Shadow calls cost real money, capped as above, and are recorded under project 'modelspend-shadow'. Prompts are held in memory only; "
               "nothing but verdicts and counts is stored. Restart a running server to apply: modelspend restart")


@shadow_app.command("off")
def shadow_off():
    """Stop shadow evaluation (what was measured is kept)."""
    typer.echo(_shadow_summary(_write_shadow(Config.from_env(), enabled=False)))


@shadow_app.command("set")
def shadow_set(rate: Optional[float] = typer.Option(None, help="Fraction of requests to sample, 0 to 1 (0.05 = 5%)."),
               budget: Optional[float] = typer.Option(None, help="Hard ceiling in USD per UTC month."),
               max_candidates: Optional[int] = typer.Option(None, help="Cheaper models tried per sampled request (1-5)."),
               judge: Optional[bool] = typer.Option(None, "--judge/--no-judge", help="Use a judge model when answers differ and there are no validators (costs money)."),
               include_sensitive: Optional[bool] = typer.Option(None, "--include-sensitive/--no-include-sensitive", help="Also shadow requests marked sensitive."),
               target_n: Optional[int] = typer.Option(None, help="Stop sampling a model/task pair at this many scored comparisons.")):
    """Change shadow settings (validated before saving)."""
    changes = {k: v for k, v in (("sample_rate", rate), ("monthly_budget_usd", budget), ("max_candidates", max_candidates), ("judge", judge),
                                 ("include_sensitive", include_sensitive), ("target_n", target_n)) if v is not None}
    if not changes:
        raise typer.BadParameter("nothing to change: give at least one option, e.g. --rate 0.1 --budget 10")
    typer.echo(_shadow_summary(_write_shadow(Config.from_env(), **changes)))


@shadow_app.command("reset")
def shadow_reset(model: Optional[str] = typer.Option(None, help="Only comparisons for this candidate model, as provider/model-id."),
                 category: Optional[str] = typer.Option(None, help="Only this task type."),
                 yes: bool = typer.Option(False, "--yes", help="Actually delete. Without it, only report what would be deleted.")):
    """Forget measured comparisons (all, or one model and/or task type), e.g. after changing your prompts or output limits. Spend records are kept."""
    from .shadow import reset
    n = reset(_store(), model, category, dry_run=not yes)
    typer.echo(f"{'deleted' if yes else 'would delete'} {n} shadow comparison(s)" + ("" if yes else "  (add --yes to delete)"))


@shadow_app.command("apply")
def shadow_apply(profile: str = typer.Option("balanced", help="Judge against this profile's bars (plus your own thresholds)."),
                 yes: bool = typer.Option(False, "--yes", help="Do it. Without this, only show what would change.")):
    """Let routing use shadow results as evidence where you have none of your own. Shows what would change first; undo with `shadow revert`."""
    from .savings import build_savings
    cfg = Config.from_env()
    rep = build_savings(_store(), cfg, profile=profile)
    ready = [r for r in rep["rows"] if r["state"] == "ready"]
    if not ready:
        typer.echo("No task type has enough shadow evidence to clear its bar yet, so applying would not change any routing decision. "
                   "Keep collecting (`modelspend shadow status`).")
    for r in ready:
        b = r["recommended"]
        typer.echo(f"  {r['category']}: {b['model']} cleared the bar ({b['detail']}); routing could choose it instead of {r['top_model']}, "
                   f"projected saving ${b['projected_saving_usd']:.4f} on the traffic in the window")
    if cfg.shadow.use_as_evidence:
        typer.echo("Already applied.")
        return
    if not yes:
        typer.echo("\nNothing changed. Run again with --yes to apply. Your own eval evidence always wins where you have it; undo any time with `modelspend shadow revert`.")
        return
    _write_shadow(cfg, use_as_evidence=True)
    typer.echo("\nApplied: routing now treats shadow results as evidence where you have no eval evidence for that model and task type. "
               "Restart a running server to pick it up: modelspend restart. Undo: modelspend shadow revert")


@shadow_app.command("revert")
def shadow_revert():
    """Routing stops using shadow results (they are kept, and still shown by `savings`)."""
    _write_shadow(Config.from_env(), use_as_evidence=False)
    typer.echo("Routing no longer uses shadow evidence. Restart a running server to pick it up: modelspend restart")


@app.command()
def savings(since: str = typer.Option("30d", help="Window for your spend: 7d, 24h, 2w, a date like 2026-10-01, or 'all'."),
            profile: str = typer.Option("balanced", help="Judge against this profile's bars (plus your own thresholds)."),
            project: Optional[str] = typer.Option(None, help="Only this project's traffic."),
            fmt: str = typer.Option("table", "--format", help="table or json.")):
    """What you could save by moving each kind of work to a cheaper model, and how sure ModelSpend is (a projection, with its evidence)."""
    from .report import parse_since
    from .savings import build_savings, to_table
    if fmt not in ("table", "json"):
        raise typer.BadParameter("--format must be table or json")
    if profile not in ("economy", "balanced", "high_assurance"):
        raise typer.BadParameter("--profile must be economy, balanced or high_assurance")
    cfg = Config.from_env()
    try:
        rep = build_savings(_store(), cfg, profile=profile, since=parse_since(since), project=project)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(rep, indent=2) if fmt == "json" else to_table(rep))


@app.command()
def whatif(since: str = typer.Option("30d", help="Window for your traffic: 7d, 24h, 2w, a date like 2026-10-01, or 'all'."),
           profile: str = typer.Option("balanced", help="Judge the evidence against this profile's bars."),
           project: Optional[str] = typer.Option(None, help="Only this project's traffic."),
           only: Optional[list[str]] = typer.Option(None, help="Only models whose name contains this (repeatable), e.g. --only gpt-4.1 --only claude."),
           provider: Optional[list[str]] = typer.Option(None, help="Only this provider (repeatable)."),
           show_all: bool = typer.Option(False, "--all", help="Show every model, not the cheapest 15."),
           fmt: str = typer.Option("table", "--format", help="table or json.")):
    """What the tokens you already used would have cost on each model in your catalog (same tokens, catalog prices; not a quality claim)."""
    from .catalog import Catalog
    from .report import parse_since
    from .whatif import build_whatif, to_table
    if fmt not in ("table", "json"):
        raise typer.BadParameter("--format must be table or json")
    if profile not in ("economy", "balanced", "high_assurance"):
        raise typer.BadParameter("--profile must be economy, balanced or high_assurance")
    cfg = Config.from_env()
    try:
        rep = build_whatif(_store(), Catalog.from_config(cfg), cfg, profile=profile, since=parse_since(since), project=project,
                           only=[o.lower() for o in only or []], providers=[registry.normalize(p) for p in provider or []])
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(rep, indent=2) if fmt == "json" else to_table(rep, top=10_000 if show_all else 15))


@reconcile_app.command("import")
def reconcile_import(file: Path = typer.Argument(..., help="Usage export from the provider: CSV, JSON or JSON lines."),
                     provider: Optional[str] = typer.Option(None, help="openai, anthropic, google, xai, deepseek or one you defined (or put a 'provider' column in the file)."),
                     source: Optional[str] = typer.Option(None, help="Name for this import (default: the file name). Importing the same name again replaces it."),
                     map_: Optional[list[str]] = typer.Option(None, "--map", help="Column mapping field=column, e.g. --map day=Date --map cost=Amount. Fields: day, model, input_tokens, output_tokens, cache_read, cache_write, requests, cost.")):
    """Read a provider usage export into ModelSpend's database (numbers only; no text from the file is stored)."""
    from .reconcile import ReconcileError, import_file
    mapping = {}
    for m in map_ or []:
        k, _, v = m.partition("=")
        if not v:
            raise typer.BadParameter(f"--map needs field=column, got {m!r}")
        mapping[k.strip()] = v.strip()
    try:
        r = import_file(_store(), file, provider.lower() if provider else None, source, mapping)
    except ReconcileError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(f"imported {r['rows']} rows as '{r['source']}': {', '.join(r['providers'])}, {r['models']} model(s), {r['first_day']}..{r['last_day']}"
               + ("" if r["has_cost"] else "  (no cost column: only tokens can be compared)"))
    if r["empty_days"]:
        typer.echo(f"note: the provider reports no usage on {', '.join(r['empty_days'])}; ModelSpend calls on those days will be flagged.")
    if r["overlap"]:
        days = ", ".join(f"{o['provider']} {o['day']}" for o in r["overlap"][:5])
        typer.echo(f"warning: another import already covers some of these days ({days}{'...' if len(r['overlap']) > 5 else ''}); "
                   "comparing adds imports together and would double count. Use --source on `reconcile run`, or `reconcile remove` one.", err=True)
    typer.echo("next: modelspend reconcile run")


@reconcile_app.command("run")
def reconcile_run(by_day: bool = typer.Option(False, "--by-day", help="One row per model per day (days are UTC; calls near midnight can land on different days)."),
                  tolerance: float = typer.Option(0.02, help="Allowed difference as a fraction of the provider's number (0.02 = 2%)."),
                  exclusive: bool = typer.Option(False, "--exclusive", help="The exported key/project is used only by ModelSpend: flag provider-higher differences too."),
                  source: Optional[str] = typer.Option(None, help="Only this import."), provider: Optional[str] = None,
                  cached_separate: Optional[list[str]] = typer.Option(None, help="Provider whose input-token column excludes cached tokens (default: anthropic only). Repeatable."),
                  cached_included: Optional[list[str]] = typer.Option(None, help="Provider whose input-token column includes cached tokens (default: every provider except anthropic). Repeatable."),
                  fmt: str = typer.Option("table", "--format", help="table, csv or json."),
                  check: bool = typer.Option(False, "--check", help="Exit with status 1 when any row needs a look (for cron/CI).")):
    """Compare ModelSpend's tokens and cost with the provider's, per model, over the days the export covers."""
    from .reconcile import ReconcileError, compare, to_csv, to_table
    if fmt not in ("table", "csv", "json"):
        raise typer.BadParameter("--format must be table, csv or json")
    over = {**{p.lower(): False for p in cached_separate or []}, **{p.lower(): True for p in cached_included or []}}
    try:
        rep = compare(_store(), by_day=by_day, tolerance=tolerance, exclusive=exclusive, source=source,
                      provider=provider.lower() if provider else None, cached_included=over)
    except ReconcileError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(rep, indent=2) if fmt == "json" else to_csv(rep) if fmt == "csv" else to_table(rep))
    if check and not rep["summary"]["ok"]:
        raise typer.Exit(1)


@reconcile_app.command("list")
def reconcile_list():
    """Show what has been imported."""
    from .reconcile import list_sources
    rows = list_sources(_store())
    if not rows:
        typer.echo("nothing imported yet: modelspend reconcile import <file> --provider <name>")
    for r in rows:
        typer.echo(f"{r['source']}  {r['provider']}  {r['first_day']}..{r['last_day']}  {r['rows']} rows  {r['tokens']:,} tokens")


@reconcile_app.command("remove")
def reconcile_remove(source: str):
    """Delete one import."""
    from .reconcile import remove_source
    n = remove_source(_store(), source)
    if not n:
        typer.echo(f"error: no import named {source!r}", err=True)
        raise typer.Exit(2)
    typer.echo(f"removed {n} rows from '{source}'")


@app.command()
def prune(older_than: str = typer.Argument(..., help="Delete usage recorded before this: 90d, 12w, or a date like 2026-07-01."),
          yes: bool = typer.Option(False, "--yes", help="Actually delete. Without it, only report what would be deleted.")):
    """Delete old usage records (requests and their attempts). Budgets and settings are kept. Reports are computed from what remains."""
    from .report import ReportError, parse_since
    try:
        cutoff = parse_since(older_than)
    except ReportError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    if cutoff is None:
        typer.echo("error: give a cutoff such as 90d or 2026-07-01 ('all' is not accepted here)", err=True)
        raise typer.Exit(2)
    n = _store().prune(cutoff, dry_run=not yes)
    typer.echo(f"{'deleted' if yes else 'would delete'} {n['requests']} request(s), {n['attempts']} attempt(s) and {n['shadow']} shadow comparison(s) older than {older_than}"
               + ("" if yes else "  (add --yes to delete)"))


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
def catalog_discover(catalog: Optional[str] = CAT,
                     provider: Optional[str] = typer.Option(None, help="Only this provider."),
                     only: str = typer.Option("", help="Comma-separated ID substrings to show, e.g. --only gpt-5,o4."),
                     all_: bool = typer.Option(False, "--all", help="Also show dated snapshot IDs (e.g. gpt-4o-2024-08-06) and lift the display limit.")):
    """List the model IDs your keys can use and compare them with the catalog. Never edits the catalog:
    discovery gives IDs only, not prices or capabilities, so new IDs must be verified by hand."""
    import re as _re
    cfg = Config.from_env()
    cat = _catalog(cfg, catalog)
    adapters = {k: v for k, v in build_adapters(cfg).items() if not provider or k == _prov(provider)}
    if not adapters:
        typer.echo("no API keys configured", err=True)
        raise typer.Exit(1)
    only_l = [o.strip().lower() for o in only.split(",") if o.strip()]
    snap = _re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8}|\d{4})$")  # dated snapshot suffixes: -2025-08-07, -20251001, -0613
    LIMIT = 60

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
            new = sorted(live - known)
            if only_l:
                new = [i for i in new if any(o in i.lower() for o in only_l)]
            hidden_snap = [] if all_ else [i for i in new if snap.search(i)]
            new = [i for i in new if i not in set(hidden_snap)]
            shown = new if all_ else new[:LIMIT]
            typer.echo(f"  not in catalog ({len(new)}{' after filter' if only_l else ''}); each needs verified pricing before it can be routed:")
            for i in shown:
                typer.echo(f"    {i}")
            if len(new) > len(shown):
                typer.echo(f"    ... {len(new) - len(shown)} more not shown: narrow with --only <substring> or use --all")
            if hidden_snap:
                typer.echo(f"  ({len(hidden_snap)} dated snapshot IDs hidden; --all shows them. Use the undated alias for routing.)")
    asyncio.run(go())


from .overrides import apply_assignments, load_overrides as _load_overrides, save_overrides as _save_overrides  # noqa: E402


_SNAPSHOT = __import__("re").compile(r"-(\d{4}-\d{2}-\d{2}|\d{8}|\d{4})$")
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
                 only: str = typer.Option("", help="Comma-separated model IDs to include (substring match; with --exact, whole IDs), e.g. --only gpt-5-mini,gpt-4.1. Needed with --add-new."),
                 exact: bool = typer.Option(False, "--exact", help="Match --only values against whole model IDs instead of substrings."),
                 snapshots: bool = typer.Option(False, "--snapshots", help="Also include dated snapshot IDs (e.g. gpt-4o-2024-08-06); hidden by default."),
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
    incomplete: list[str] = []  # models whose limits or tier the API could not tell us: they stay unroutable until the user sets them

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
                if only_l and not any((o == mid.lower()) if exact else (o in mid.lower()) for o in only_l):
                    continue
                if not snapshots and not known and _SNAPSHOT.search(mid):
                    continue  # dated snapshot of a model: route to the undated alias instead
                has_tier = known and cat.get(ref).tier is not None
                patch = sync_patch(known, mid, name, i, asm, today, has_tier)
                if patch is None:
                    continue
                n += 1
                have = cat.get(ref).limits if known else None
                if not ((patch.get("limits", {}).get("context_tokens") or (have and have.context_tokens))
                        and (patch.get("limits", {}).get("max_output_tokens") or (have and have.max_output_tokens))) or not (patch.get("tier") or has_tier):
                    incomplete.append(ref)
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
    if incomplete:
        typer.echo("\nThe API does not say these models' context length, output limit or tier, so they stay unroutable until you set them from the provider's docs:\n  "
                   + "\n  ".join(f"modelspend catalog set {r} tier=1 limits.context_tokens=<n> limits.max_output_tokens=<n>" for r in incomplete[:5])
                   + ("\n  ..." if len(incomplete) > 5 else "") + "\n(tier is your own prior: 1 small and cheap, 3 strongest)")
    if write and n:
        _save_overrides(cfg, ov)
        typer.echo(f"wrote {n} model(s) to {cfg.overrides_path}")
    elif n:
        typer.echo("dry run: add --write to save. Then set what the API cannot tell you (see `modelspend catalog set`).")
    else:
        typer.echo("nothing to sync: no catalog models matched the API's list (use --add-new to add unknown ones)")


@catalog_app.command("import-openrouter")
def catalog_import_openrouter(
        only: str = typer.Option("", help="Comma-separated model id substrings to import, e.g. --only claude-sonnet,gpt-4.1,llama-3.3. Needed unless --refresh: the list has hundreds of models."),
        exact: bool = typer.Option(False, "--exact", help="Match --only against whole model ids."),
        include_free: bool = typer.Option(False, "--include-free", help="Also import free-tier (':free') models: they are rate limited and may be logged by the provider."),
        min_context: int = typer.Option(0, help="Skip models with a shorter context window than this many tokens."),
        default_max_output: Optional[int] = typer.Option(None, help="Output limit to use for models whose listing does not state one (from the provider's docs)."),
        refresh: bool = typer.Option(False, "--refresh", help="Update prices, limits and availability of the OpenRouter models already in your catalog."),
        file: Optional[Path] = typer.Option(None, "--file", help="Read a saved response of OpenRouter's model list instead of fetching it."),
        write: bool = typer.Option(False, help="Write to overrides.json. Without it this is a dry run."),
        catalog: Optional[str] = CAT):
    """Bring OpenRouter models into your catalog with their prices and limits, from OpenRouter's own model list.

    Models arrive with no tier (that is your prior about a model, and OpenRouter does not publish one): routing will not pick them on a prior,
    but shadow evaluation can measure them against the model you use today. Set a tier yourself with `modelspend catalog set`."""
    from . import openrouter as orr
    cfg = Config.from_env()
    cat = _catalog(cfg, catalog)
    only_l = [o.strip().lower() for o in only.split(",") if o.strip()]
    if not only_l and not refresh:
        typer.echo("name the models you want, e.g. --only claude-sonnet,gpt-4.1 (OpenRouter lists hundreds), or use --refresh to update the ones you already imported", err=True)
        raise typer.Exit(2)
    try:
        payload = orr.load_payload(file.read_bytes()) if file else orr.fetch(registry.spec("openrouter").base_url, cfg.api_keys.get("openrouter"))
    except (orr.OpenRouterError, OSError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    entries, skipped = orr.parse(payload)
    have = {m.id: m for m in cat.models if m.provider == "openrouter"}
    if refresh and not only_l:
        picked, left = [e for e in entries if e.id in have], []
        missing = sorted(set(have) - {e.id for e in entries})
        for mid in missing:
            typer.echo(f"note: {mid} is no longer in OpenRouter's usable list (removed, renamed or no longer priced per token); left as it is")
    else:
        picked, left = orr.select(entries, only_l, exact, include_free, min_context)
    if not picked:
        typer.echo("no matching models" + (" (" + "; ".join(f"{s.id}: {s.reason}" for s in left[:3]) + ")" if left else "")
                   + f". {len(entries)} usable models in the list; try a shorter --only.")
        raise typer.Exit(0)
    today = str(date.today())
    ov = _load_overrides(cfg)
    ov.setdefault("add_models", [])
    existing = {m["id"]: m for m in ov["add_models"] if m.get("provider") == "openrouter"}
    incomplete: list[str] = []
    typer.echo(f"{'':7}{'model':<46}{'context':>9}{'max out':>9}{'in $/M':>9}{'out $/M':>9}")
    for e in picked:
        fresh = orr.catalog_entry(e, today, default_max_output)
        old = have.get(e.id)
        status = "update" if old else "add"
        change = ""
        if old and old.pricing is not None and old.pricing.known and (old.pricing.input_per_mtok, old.pricing.output_per_mtok) != (e.input_per_mtok, e.output_per_mtok):
            change = f"  PRICE CHANGED: in ${old.pricing.input_per_mtok:g} -> ${e.input_per_mtok:g}, out ${old.pricing.output_per_mtok:g} -> ${e.output_per_mtok:g}"
        lim = fresh.get("limits", {})
        typer.echo(f"{status:<7}openrouter/{e.id:<35}{lim.get('context_tokens', '?'):>9}{lim.get('max_output_tokens', '?'):>9}{e.input_per_mtok:>9g}{e.output_per_mtok:>9g}{change}")
        for w in e.warnings:
            typer.echo(f"       note: {w}")
        if "max_output_tokens" not in lim and not (old and old.limits.max_output_tokens):
            incomplete.append(f"openrouter/{e.id}")
        if write:
            ov["add_models"] = [m for m in ov["add_models"] if not (m.get("provider") == "openrouter" and m.get("id") == e.id)]
            ov["add_models"].append(orr.merge(existing.get(e.id), fresh))
            stale = ov.get("models", {}).get(f"openrouter/{e.id}")
            if stale and "pricing" in stale:  # a hand-set price from `catalog price` would silently override the fresh one
                del stale["pricing"]
                typer.echo("       note: replaced the price you set by hand with OpenRouter's current one")
    if left:
        typer.echo(f"\nnot imported ({len(left)}): " + "; ".join(f"{s.id} ({s.reason})" for s in left[:3]) + (" ..." if len(left) > 3 else ""))
    if incomplete:
        typer.echo("\nNo output limit is listed for: " + ", ".join(incomplete[:5]) + (" ..." if len(incomplete) > 5 else "")
                   + ". Add --default-max-output <n> (from the provider's docs) or set it with `modelspend catalog set <ref> limits.max_output_tokens=<n>`; "
                     "until then they cannot be routed.")
    if write:
        _save_overrides(cfg, ov)
        typer.echo(f"\nwrote {len(picked)} model(s) to {cfg.overrides_path}. Prices come from OpenRouter's own list (what OpenRouter bills); review them against openrouter.ai.\n"
                   "Next: let shadow evaluation measure them against the model you use (`modelspend shadow on`), or set a tier yourself: modelspend catalog set openrouter/<id> tier=<1-3>")
    else:
        typer.echo("\ndry run: add --write to save.")


@catalog_app.command("set")
def catalog_set(ref: str, assignments: list[str] = typer.Argument(..., help="dotted.key=json-value, e.g. tier=2 capabilities.streaming=true pricing.input_per_mtok=3")):
    """Patch one model in overrides.json (validated by reloading the catalog; reverted if invalid)."""
    cfg = Config.from_env()
    try:
        apply_assignments(cfg, ref, assignments)
    except ValueError as e:
        msg = str(e)
        typer.echo(f"{msg} (see `modelspend catalog list`)" if msg.startswith("unknown model") else msg, err=True)
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
        eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), Store(cfg.db_path), shadow=False)  # type: ignore[arg-type]  # live spend is recorded under project modelspend-eval; evals never shadow
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


@eval_app.command("check")
def eval_check(fixtures: Path = typer.Option(..., "--fixtures", help="Your cases: a .jsonl file, a folder of .jsonl files, or <folder>/calibration + <folder>/heldout."),
               profile: str = typer.Option("balanced", help="Compare case counts with this profile's bar (plus your thresholds).")):
    """Check your eval cases (no model is called, nothing is spent): valid format, working validators, enough cases for your bar."""
    from .eval.harness import lint_tasks, load_tasks
    from .policy import PROFILES, bar_for, min_samples_for
    from .types import Profile, TaskCategory
    cfg = Config.from_env()
    try:
        pol = PROFILES[Profile(profile)]
        layered = fixtures.is_dir() and ((fixtures / "calibration").is_dir() or (fixtures / "heldout").is_dir())
        if layered:
            sets = {sp: load_tasks(sp, fixtures) for sp in ("calibration", "heldout") if (fixtures / sp).is_dir()}
        else:
            sets = {"calibration": load_tasks("calibration", fixtures)}
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    label = {"calibration": "calibration" if layered else "all cases", "heldout": "held-out"}
    errors = 0
    for split, tasks in sets.items():
        typer.echo(f"{label[split]}: {len(tasks)} case(s) from {fixtures}")
        by: dict[str, int] = {}
        for t in tasks:
            by[t.category] = by.get(t.category, 0) + 1
        for c in TaskCategory:
            if c == TaskCategory.unknown or c.value not in by:
                continue
            b = bar_for(pol, c, cfg.thresholds)
            n = by[c.value]
            need = max(b.n, min_samples_for(b.rate) if b.basis == "lower_bound" else 1)
            verdict = "enough to clear the bar if the model passes them all" if n >= need else f"too few to clear the bar (needs {need})"
            typer.echo(f"  {c.value:<15}{n:>4} case(s)   bar: {b.describe()}   -> {verdict}")
        for sev, cid, msg in lint_tasks(tasks):
            typer.echo(f"  {sev.upper():<8}{cid}: {msg}")
            errors += sev == "error"
    if layered and len(sets) == 2:
        both = {t.id for t in sets["calibration"]} & {t.id for t in sets["heldout"]}
        if both:
            typer.echo(f"  WARNING calibration and held-out share case ids: {', '.join(sorted(both)[:5])} (keep the two sets separate, or the held-out result proves nothing)")
    typer.echo("ok" if not errors else f"{errors} error(s): fix them before measuring")
    raise typer.Exit(1 if errors else 0)


@eval_app.command("template")
def eval_template(category: str = typer.Argument("classification", help="Task category to show an example case for.")):
    """Print an example eval case (one JSON line) to copy into your own .jsonl file."""
    from .eval.harness import load_tasks
    t = next((t for t in load_tasks("calibration") if t.category == category), None)
    if t is None:
        typer.echo(f"no example for '{category}'", err=True)
        raise typer.Exit(2)
    case = {"id": f"my-{category}-1", "category": t.category, "messages": t.messages, "validators": t.validators, "reference": t.reference, "wrong": t.wrong}
    typer.echo(json.dumps(case))
    typer.echo("one case per line. `validators` decide pass/fail (see docs/own-evals.md); `reference` should pass and `wrong` should fail: "
               "`modelspend eval check` uses them to test your validators.", err=True)


@eval_app.command("calibrate")
def eval_calibrate(mode: str = "mock", providers: Optional[str] = None, budget: Optional[float] = None,
                   fixtures: Optional[Path] = None, sandbox: bool = False, catalog: Optional[str] = None,
                   dataset: Optional[str] = typer.Option(None, help="Name stored with the evidence (shown in route explanations). Default: your folder or file name."),
                   show_failures: bool = typer.Option(False, "--show-failures", help="List each failed case with the model's (shortened) answer. Printed only: nothing is stored."),
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
        eng = Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), Store(cfg.db_path), shadow=False)  # type: ignore[arg-type]  # live spend is recorded under project modelspend-eval; evals never shadow
    ds = dataset or (fixtures.stem if fixtures is not None and fixtures.is_file() else fixtures.name if fixtures is not None else "modelspend-calibration-v2")
    fails: list[dict] = []
    stats: dict[str, int] = {}
    ev, spent, stopped = asyncio.run(calibrate(eng, tasks, provs, budget if mode == "live" else None, sandbox, ds, fails, stats))
    typer.echo(("SIMULATED " if mode == "mock" else "") + f"calibration spend ${spent:.6f}" + (f" | {stopped}" if stopped else ""))
    for ref, cats in sorted(ev.items()):
        typer.echo(f"  {ref}: " + ", ".join(f"{c} {e.passes}/{e.n}" for c, e in sorted(cats.items())))
    if stats.get("retried") or stats.get("still_truncated"):
        from .eval.harness import TRUNCATION_HEADROOM
        typer.echo(f"note: {stats.get('retried', 0)} call(s) stopped at the output limit and were retried once with {TRUNCATION_HEADROOM} more tokens "
                   f"(reasoning models spend output tokens thinking); {stats.get('still_truncated', 0)} still stopped at the limit and count as failures marked 'truncated'.")
    if show_failures:
        typer.echo("\nfailed cases (the model's answer is shown shortened, on this screen only):" if fails else "\nno failed cases")
        for f in fails:
            typer.echo(f"  {f['model']}  {f['case']}: {f['reason'] or 'failed'}  -> {f['output']!r}")
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
    data[registry.key_env()[prov][0]] = key
    p = write_keys_file(cfg.home, data)
    typer.echo(f"saved {prov} key to {p} (readable only by you). Next: modelspend keys test {prov}")
    if cfg.key_sources.get(prov) == "environment":
        typer.echo(f"note: {registry.key_env()[prov][0]} is set in your environment and takes precedence over the saved key.")


@keys_app.command("list")
def keys_list():
    """Which providers have a key, and where it comes from. Keys are never printed."""
    cfg = Config.from_env()
    for p in registry.all_providers():
        src = cfg.key_sources.get(p)
        hint = f"env var: {' or '.join(registry.key_env()[p])}" if registry.spec(p).needs_key else "no key needed (local server)"
        typer.echo(f"{p:<10}{'set (' + src + ')' if src else 'not set':<26}{hint}")
    typer.echo(f"\nkeys file: {keys_path(cfg.home)} ({'exists' if keys_path(cfg.home).exists() else 'not created yet'})")


@keys_app.command("remove")
def keys_remove(provider: str):
    """Delete a provider's key from the keys file (does not touch your environment)."""
    prov = _prov(provider)
    cfg = Config.from_env()
    data = read_keys_file(cfg.home)
    gone = [n for n in registry.key_env()[prov] if data.pop(n, None) is not None]
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
    for p in registry.all_providers():
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
    from .server import runner
    st = runner.server_state(cfg)
    typer.echo(f"server: running (pid {st.pid or '?'}) at http://{runner.probe_host(st.host or cfg.host)}:{st.port}  [modelspend stop]" if st.running
               else "server: not running  [modelspend serve --background]")
    typer.echo("\nproviders:")
    providers_list(catalog)
    routable = [m for m in cat.models if not routability(m, cfg)]
    typer.echo(f"\nroutable models ({len(routable)}):")
    for m in routable:
        typer.echo(f"  {m.ref}")
    if not routable:
        typer.echo("  none yet. Typical fixes: modelspend keys set <provider>; modelspend catalog sync --provider <provider> --write")
    typer.echo("\npricing sources (change with `modelspend sources set <provider> <url>`):")
    for p, s in sorted(resolve_sources(cfg).items(), key=lambda kv: registry.all_providers().index(kv[0])):
        typer.echo(f"  {p:<10}{s.pricing_url}  [{s.origin}]")


# ---------------------------------------------------------------- sources
@sources_app.command("list")
def sources_list():
    cfg = Config.from_env()
    snaps = load_snapshots(cfg)
    for p, s in sorted(resolve_sources(cfg).items(), key=lambda kv: registry.all_providers().index(kv[0])):
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
    targets = [srcs[_prov(provider)]] if provider else [srcs[p] for p in registry.all_providers() if p in srcs]
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


# ---------------------------------------------------------------- pricing refresh
EXIT_PENDING = 10  # `pricing refresh` exit code: at least one proposal is waiting for review (cron can alert on it)


def _pricing_engine(cfg: Config, catalog: Optional[str]) -> Engine:
    return Engine(_catalog(cfg, catalog), cfg, build_adapters(cfg), Store(cfg.db_path), shadow=False)  # type: ignore[arg-type]


def _print_result(r: dict) -> None:
    ex = r.get("extractor")
    used = f" [extractor {ex['model']}: {ex['total_tokens']} tokens (in {ex['tokens_in']}, out {ex['tokens_out']}), cost {_usd(ex['cost_usd'])}]" if ex else ""
    typer.echo(f"{r['provider']:<10}{r['state']:<15}{r.get('detail', '')}{used}")
    for k, label in (("unmatched", "page rows matching no catalog model"), ("unverified", "rows rejected (numbers/name not found in page)"),
                     ("ambiguous", "ambiguous rows (same name twice, e.g. tiers)")):
        if r.get(k):
            typer.echo(f"          - {r[k]} {label}")
    if r.get("page_truncated"):
        typer.echo("          - page text was truncated before extraction: some models may be missing")
    if r.get("proposal_id"):
        typer.echo(f"          proposal: {r['proposal_id']}   (modelspend pricing show {r['proposal_id']})")


async def _refresh_many(cfg, eng, targets, **kw) -> list[dict]:
    from .pricing_refresh import refresh_provider
    return [await refresh_provider(eng, cfg, p, **kw) for p in targets]


@pricing_app.command("refresh")
def pricing_refresh(provider: Optional[str] = typer.Argument(None, help="One provider, or omit for all four."),
                    auto_apply: bool = typer.Option(False, "--auto-apply", help="Apply without review when every row is verified, only catalog models change, and no change exceeds --max-change-pct."),
                    max_change_pct: float = typer.Option(50.0, help="Largest price change --auto-apply may apply."),
                    force: bool = typer.Option(False, help="Re-extract even if the page is unchanged since your baseline."),
                    providers: Optional[str] = typer.Option(None, help="Which providers' models may run the extraction (default: your enabled providers, else every provider with a key)."),
                    extractor_model: Optional[str] = typer.Option(None, help="Pin the extractor, e.g. anthropic/claude-haiku-5-5."),
                    max_tokens: int = typer.Option(80_000, help="Token budget for each extraction call."),
                    catalog: Optional[str] = CAT):
    """Fetch pricing pages and propose price updates. A page identical to your accepted baseline costs nothing (no model call).
    Exit code 0 = nothing to review, 10 = proposals pending review, 1 = an error occurred. Safe to run from cron."""
    cfg = Config.from_env()
    eng = _pricing_engine(cfg, catalog)
    targets = [_prov(provider)] if provider else list(registry.all_providers())
    results = asyncio.run(_refresh_many(cfg, eng, targets, force=force, auto_apply=auto_apply, max_change_pct=max_change_pct,
                                        providers=[p.strip() for p in providers.split(",")] if providers else None,
                                        max_tokens=max_tokens, extractor_model=extractor_model))
    for r in results:
        _print_result(r)
    from .pricing_refresh import list_proposals
    pending = list_proposals(cfg, "pending")
    if any(r["state"] == "error" for r in results):
        raise typer.Exit(1)
    if pending:
        typer.echo(f"\n{len(pending)} proposal(s) waiting: `modelspend pricing proposals`, then `pricing apply <id>` (or the dashboard's Pricing tab).")
        raise typer.Exit(EXIT_PENDING)


@pricing_app.command("paste")
def pricing_paste(provider: str, file: Optional[Path] = typer.Option(None, "--file", help="Read the pasted table text from a file (default: standard input)."),
                  auto_apply: bool = typer.Option(False, "--auto-apply"), max_change_pct: float = 50.0,
                  providers: Optional[str] = None, extractor_model: Optional[str] = None, catalog: Optional[str] = CAT):
    """Extract prices from TEXT YOU PASTE (copied from the provider's pricing page), for pages that need JavaScript or block scripts.
    Same verification as `refresh`: every number must appear in the text you pasted."""
    cfg = Config.from_env()
    text = file.read_text() if file else sys.stdin.read()
    if not text.strip():
        typer.echo("no text given (paste the table, then press Ctrl-D, or use --file)", err=True)
        raise typer.Exit(2)
    eng = _pricing_engine(cfg, catalog)
    r = asyncio.run(_refresh_many(cfg, eng, [_prov(provider)], text=text, auto_apply=auto_apply, max_change_pct=max_change_pct,
                                  providers=[p.strip() for p in providers.split(",")] if providers else None, extractor_model=extractor_model))[0]
    _print_result(r)
    raise typer.Exit(1 if r["state"] == "error" else (EXIT_PENDING if r["state"] == "proposed" else 0))


@pricing_app.command("proposals")
def pricing_proposals(status: Optional[str] = typer.Option("pending", help="pending | applied | rejected | informational | all")):
    from .pricing_refresh import list_proposals
    items = list_proposals(Config.from_env(), None if status == "all" else status)
    for p in items:
        n = sum(len(c["fields"]) for c in p["changes"])
        typer.echo(f"{p['id']:<26}{p['status']:<12}{len(p['changes'])} model(s), {n} price field(s)   [{p['source']['kind']}, {p['created']}]")
    if not items:
        typer.echo(f"no {status} proposals")


@pricing_app.command("show")
def pricing_show(proposal_id: str):
    """Show exactly what a proposal would change (old -> new), and what was rejected or could not be matched."""
    from .pricing_refresh import get_proposal
    try:
        p = get_proposal(Config.from_env(), proposal_id)
    except KeyError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    typer.echo(f"{p['id']}  [{p['status']}]  {p['provider']}  source: {p['source']['kind']} {p['source']['url']}")
    ex = p["extractor"]
    typer.echo(f"extracted by {ex['model']} using {ex['total_tokens']} tokens\n")
    for c in p["changes"]:
        typer.echo(f"  {c['ref']}")
        for f in c["fields"]:
            old = "unset" if f["old"] is None else f"${f['old']}"
            typer.echo(f"      {f['field']:<24}{old:>10} -> ${f['new']}" + (f"   ({f['pct']:+g}%)" if f["pct"] is not None else ""))
    for k, label in (("unmatched", "No catalog model matches"), ("unverified", "REJECTED (not found in the page text)"), ("ambiguous", "Ambiguous")):
        for x in p[k]:
            typer.echo(f"  {label}: {x['name']}" + (f"  ({'; '.join(x['problems'])})" if x.get("problems") else ""))
    for n in p["notes"]:
        typer.echo(f"  note: {n}")


@pricing_app.command("apply")
def pricing_apply(proposal_id: Optional[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all", help="Apply every pending proposal.")):
    """Apply a reviewed proposal: writes the prices to overrides.json (stamped verified today) and accepts the page as your new baseline."""
    from .pricing_refresh import apply_proposal, list_proposals
    cfg = Config.from_env()
    ids = [p["id"] for p in list_proposals(cfg, "pending")] if all_ else ([proposal_id] if proposal_id else [])
    if not ids:
        typer.echo("give a proposal id, or --all", err=True)
        raise typer.Exit(2)
    for i in ids:
        try:
            p = apply_proposal(cfg, i)
            typer.echo(f"applied {i}: {len(p['changes'])} model(s) updated")
        except (KeyError, ValueError) as e:
            typer.echo(f"{i}: {e}", err=True)
            raise typer.Exit(2)


@pricing_app.command("reject")
def pricing_reject(proposal_id: str):
    from .pricing_refresh import reject_proposal
    try:
        reject_proposal(Config.from_env(), proposal_id)
    except (KeyError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    typer.echo(f"rejected {proposal_id} (this version of the page will not be proposed again)")


def main() -> None:
    try:
        app()
    except ValueError as e:
        if not str(e).startswith("settings.json"):
            raise  # anything else is a bug: keep the traceback
        typer.echo(f"error: {e}", err=True)  # a mistake in the user's settings file: one clear line, not a traceback
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
