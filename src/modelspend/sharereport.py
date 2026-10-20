"""A report you can hand to someone: one static file with your spend, the projected saving, the evidence behind it and the what-if price table.

Safe to share by construction, not by promise:
- it contains token counts, dollar amounts, model names and task types, and nothing else: no prompts, no responses, no keys, no file paths;
- project names (which can name clients or products) appear only if you ask with `include_projects`;
- the HTML has no scripts and makes no network request (a Content-Security-Policy forbids both), and every value is escaped;
- the projection is labelled as a projection, with the same caveats the numbers carry in the terminal.
Built from the same functions as `modelspend savings` and `modelspend whatif`, so the page cannot say something they don't.
"""
from __future__ import annotations

import html
import time
from typing import Any

from . import __version__
from .catalog import Catalog
from .config import Config
from .savings import _traffic, build_savings
from .shadow import TOOLING_PROJECTS, aggregate
from .store import Store
from .whatif import build_whatif

REPO_URL = "https://github.com/rmencke2/modelspend"


def _by_project(store: Store, since: float | None) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in TOOLING_PROJECTS)
    where, params = [f"coalesce(project, '') not in ({marks})", "status in ('ok','validation_failed','truncated','refused')"], [*TOOLING_PROJECTS]
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    q = f"select coalesce(project, '(none)') project, count(*) n, sum(actual_cost) cost from requests where {' and '.join(where)} group by 1 order by 3 desc"
    return [{"project": r["project"], "requests": r["n"], "cost": r["cost"] or 0.0} for r in store.db.execute(q, params)]


def build_share(store: Store, catalog: Catalog, cfg: Config, *, profile: str = "balanced", since: float | None = None, project: str | None = None,
                include_projects: bool = False, title: str = "ModelSpend report", window: str = "") -> dict[str, Any]:
    sv = build_savings(store, cfg, profile=profile, since=since, project=project)
    wi = build_whatif(store, catalog, cfg, profile=profile, since=since, project=project)
    models: dict[str, dict[str, Any]] = {}
    for t in _traffic(store, since, project).values():
        for ref, m in t["models"].items():
            x = models.setdefault(ref, {"model": ref, "requests": 0, "cost": 0.0})
            x["requests"] += m["requests"]
            x["cost"] += m["cost"]
    return {
        "title": title[:120], "window": window, "profile": profile, "version": __version__, "generated": time.time(), "savings": sv, "whatif": wi,
        "models": sorted(models.values(), key=lambda m: -m["cost"]),
        "projects": _by_project(store, since) if include_projects and not project else None,
        "comparisons": sum(a["n"] for a in aggregate(store)),
        "calls": sum(r["requests"] for r in sv["rows"]),
        "notes": sv["notes"] + wi["notes"][:-1],  # the last what-if note repeats the eval/shadow exclusion the savings notes already carry
    }


def window_label(since: str | None) -> str:
    """'30d' -> 'Last 30 days'; a date -> 'Since 2026-10-01'; 'all' -> 'All recorded usage'. Only ever echoes a short, validated token."""
    import re
    x = (since or "all").strip().lower()
    m = re.fullmatch(r"(\d{1,4})([hdw])", x)
    if m:
        n, unit = int(m.group(1)), {"h": "hour", "d": "day", "w": "week"}[m.group(2)]
        return f"Last {n} {unit}{'' if n == 1 else 's'}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
        return f"Since {x}"
    return "All recorded usage"


def _usd(v: float | None) -> str:
    return "-" if v is None else f"${v:,.2f}" if abs(v) >= 0.10 else f"${v:,.4f}"


def _pct(r: float | None) -> str:
    return "-" if r is None else f"{(r - 1) * 100:+.0f}%"


def _short(ref: str | None) -> str:
    return (ref or "-").split("/", 1)[-1]


def _passed(p: dict[str, Any] | None) -> str:
    return f"{p['passes']}/{p['n']}" if p else "-"


def _lb(p: dict[str, Any] | None) -> str:
    return "-" if not p or p["lower_bound"] is None else "%.2f" % p["lower_bound"]


def _evidence(r: dict[str, Any]) -> str:
    return f"{r['evidence_passes']}/{r['evidence_n']}" if r["evidence_n"] else "unmeasured"


def _pick(r: dict[str, Any]) -> dict[str, Any] | None:
    return r["recommended"] or (r["candidates"][0] if r["candidates"] else None)


STATE = {"ready": "Ready", "collecting": "Collecting evidence", "no_safe_cheaper_model": "No safe cheaper model yet"}


def to_markdown(d: dict[str, Any]) -> str:
    sv, wi = d["savings"], d["whatif"]
    t = sv["totals"]
    out = [f"# {d['title']}", "", f"_{d['window'] or 'All recorded usage'} - generated {time.strftime('%Y-%m-%d', time.gmtime(d['generated']))} by ModelSpend {d['version']}. "
           "Numbers only: no prompts, responses or keys._", ""]
    out.append(f"**Spend:** {_usd(t['cost'])} over {d['calls']:,} calls.  " + (
        f"**Projected saving:** {_usd(t['projected_saving_usd'])} ({t['projected_saving_pct']:.0%}), in {t['categories_ready']} of {t['categories']} task types where the evidence clears the bar."
        if t["projected_saving_usd"] > 0 else "No task type has enough evidence yet for a saving estimate."))
    out += ["", "## Evidence by task type", "", "| task | spend | state | best cheaper model | passed | lower bound | cost vs now | projected saving |", "|---|---:|---|---|---:|---:|---:|---:|"]
    for r in sv["rows"]:
        b, p = r["recommended"], _pick(r)
        cells = [r["category"], _usd(r["cost"]), STATE[r["state"]], _short(p["model"]) if p else "-", _passed(p), _lb(p),
                 _pct(p["cost_ratio"]) if p else "-", _usd(b["projected_saving_usd"]) if b else "-"]
        out.append("| " + " | ".join(cells) + " |")
    out += ["", "## Spend by model", "", "| model | requests | spend |", "|---|---:|---:|"] + [f"| {_short(m['model'])} | {m['requests']:,} | {_usd(m['cost'])} |" for m in d["models"][:15]]
    if d["projects"]:
        out += ["", "## Spend by project", "", "| project | requests | spend |", "|---|---:|---:|"] + [f"| {p['project']} | {p['requests']:,} | {_usd(p['cost'])} |" for p in d["projects"][:15]]
    out += ["", "## The same tokens on other models", "", "| model | would cost | vs actual | evidence | clears the bar for |", "|---|---:|---:|---|---|"]
    for r in wi["rows"][:10]:
        out.append("| " + " | ".join([_short(r["model"]), _usd(r["cost"]), _pct(r["ratio"]), _evidence(r), ", ".join(r["clears"]) or "-"]) + " |")
    out += ["", "## Read this before you rely on it", ""] + [f"- {n}" for n in d["notes"]] + ["", f"Made with [ModelSpend]({REPO_URL}), open source, runs on your machine."]
    return "\n".join(out) + "\n"


CSS = """
:root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#6f6d68;--grid:#e1e0d9;--hair:rgba(11,11,11,.10);--card:#fff;--s1:#2a78d6;--s3:#1baf7a;--track:#e9f1fb}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#a09e97;--grid:#2c2c2a;--hair:rgba(255,255,255,.10);--card:#222221;--s1:#3987e5;--s3:#199e70;--track:#0d366b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:900px;margin:0 auto;padding:28px 16px 48px}h1{font-size:22px;margin:0 0 2px}h2{font-size:15px;margin:28px 0 8px}
.sub{color:var(--muted);font-size:13px;margin:0 0 18px}.hero{font-size:clamp(40px,9vw,60px);line-height:1.05;font-weight:650;letter-spacing:-.02em;margin:10px 0 2px}
.hero small{display:block;line-height:1.4;font-size:14px;font-weight:400;color:var(--ink2);letter-spacing:0;margin-top:6px;max-width:60ch}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}.tile{background:var(--card);border:1px solid var(--hair);border-radius:8px;padding:10px 12px}
.tile b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.tile span{font-size:12px;color:var(--muted)}
.chart{background:var(--card);border:1px solid var(--hair);border-radius:8px;padding:14px 16px}.legend{display:flex;gap:14px;font-size:12px;color:var(--ink2);margin:0 0 10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.row{display:grid;grid-template-columns:minmax(80px,170px) 1fr 70px;gap:10px;align-items:center;min-height:28px;font-size:13px}
.track{display:flex;gap:2px;height:14px}.track b{display:block;height:100%;border-radius:0 4px 4px 0;min-width:2px}.track b:first-child{border-radius:0 4px 4px 0}
.n{text-align:right;font-variant-numeric:tabular-nums}.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:6px 10px 6px 0;border-bottom:1px solid var(--grid);text-align:left;white-space:nowrap}th{color:var(--muted);font-weight:500;font-size:12px}th.n,td.n{text-align:right}
ul{padding-left:18px;color:var(--ink2);font-size:13px}.foot{margin-top:28px;color:var(--muted);font-size:12px}a{color:var(--s1)}
"""


def to_html(d: dict[str, Any]) -> str:
    e = html.escape
    sv, wi = d["savings"], d["whatif"]
    t = sv["totals"]
    when = time.strftime("%Y-%m-%d", time.gmtime(d["generated"]))
    if t["projected_saving_usd"] > 0:
        hero = (f"{e(_usd(t['projected_saving_usd']))}<small>projected saving ({t['projected_saving_pct']:.0%} of {e(_usd(t['cost']))} spend), in {t['categories_ready']} of "
                f"{t['categories']} task types where cheaper models cleared the evidence bar. A projection, not a measurement.</small>")
    else:
        hero = f"{e(_usd(t['cost']))}<small>spent over {d['calls']:,} calls. No task type has enough evidence yet for a saving estimate.</small>"
    tiles = [("Spend", _usd(t["cost"])), ("Calls", f"{d['calls']:,}"), ("Models used", str(len(d["models"]))), ("Scored comparisons", f"{d['comparisons']:,}")]
    peak = max([r["cost"] for r in sv["rows"]] or [0]) or 1
    bars = []
    for r in sv["rows"]:
        sav = r["recommended"]["projected_saving_usd"] if r["recommended"] else 0.0
        keep = max(0.0, r["cost"] - sav)
        tip = f"{r['category']}: {_usd(r['cost'])} spent" + (f", {_usd(sav)} projected saving" if sav else "")
        seg = f'<b style="width:{100 * keep / peak:.2f}%;background:var(--s1)"></b>' + (f'<b style="width:{100 * sav / peak:.2f}%;background:var(--s3)"></b>' if sav else "")
        bars.append(f'<div class="row" title="{e(tip)}"><span>{e(r["category"])}</span><div class="track">{seg}</div><span class="n">{e(_usd(r["cost"]))}</span></div>')
    ev = ['<tr><th>Task</th><th class="n">Spend</th><th>State</th><th>Best cheaper model</th><th class="n">Passed</th><th class="n">Lower bound</th><th class="n">Cost vs now</th><th class="n">Projected saving</th></tr>']
    for r in sv["rows"]:
        b, p = r["recommended"], _pick(r)
        cells = [(r["category"], ""), (_usd(r["cost"]), "n"), (STATE[r["state"]], ""), (_short(p["model"]) if p else "-", ""), (_passed(p), "n"), (_lb(p), "n"),
                 (_pct(p["cost_ratio"]) if p else "-", "n"), (_usd(b["projected_saving_usd"]) if b else "-", "n")]
        ev.append("<tr>" + "".join('<td%s>%s</td>' % (' class="n"' if c else "", e(v)) for v, c in cells) + "</tr>")
    mt = ['<tr><th>Model</th><th class="n">Requests</th><th class="n">Spend</th></tr>'] + [
        f'<tr><td>{e(_short(m["model"]))} <span style="color:var(--muted)">{e(m["model"].split("/", 1)[0])}</span></td><td class="n">{m["requests"]:,}</td><td class="n">{e(_usd(m["cost"]))}</td></tr>'
        for m in d["models"][:15]]
    wt = ['<tr><th>Model</th><th class="n">Would cost</th><th class="n">vs actual</th><th class="n">Evidence</th><th>Clears the bar for</th></tr>'] + [
        f'<tr><td>{e(_short(r["model"]))}</td><td class="n">{e(_usd(r["cost"]))}</td><td class="n">{e(_pct(r["ratio"]))}</td>'
        f'<td class="n">{e(_evidence(r))}</td><td>{e(", ".join(r["clears"]) or "-")}</td></tr>' for r in wi["rows"][:10]]
    proj = ""
    if d["projects"]:
        proj = ('<h2>Spend by project</h2><div class="wrap"><table><tr><th>Project</th><th class="n">Requests</th><th class="n">Spend</th></tr>'
                + "".join(f'<tr><td>{e(p["project"])}</td><td class="n">{p["requests"]:,}</td><td class="n">{e(_usd(p["cost"]))}</td></tr>' for p in d["projects"][:15]) + "</table></div>")
    notes = "".join(f"<li>{e(n)}</li>" for n in d["notes"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<meta name="robots" content="noindex"><meta name="color-scheme" content="light dark"><title>{e(d['title'])}</title><style>{CSS}</style></head>
<body><main>
<h1>{e(d['title'])}</h1>
<p class="sub">{e(d['window'] or 'All recorded usage')} &middot; generated {when} by ModelSpend {e(d['version'])} &middot; numbers only: no prompts, responses or keys</p>
<div class="hero">{hero}</div>
<div class="tiles">{''.join(f'<div class="tile"><b>{e(v)}</b><span>{e(k)}</span></div>' for k, v in tiles)}</div>
<h2>Spend by task type, and the projected saving where the evidence clears the bar</h2>
<div class="chart"><div class="legend"><span><i style="background:var(--s1)"></i>Would still cost</span><span><i style="background:var(--s3)"></i>Projected saving</span></div>{''.join(bars) or 'No usage in this window.'}</div>
<h2>Evidence by task type</h2><div class="wrap"><table>{''.join(ev)}</table></div>
<h2>Spend by model</h2><div class="wrap"><table>{''.join(mt)}</table></div>
{proj}
<h2>The same tokens on other models</h2><div class="wrap"><table>{''.join(wt)}</table></div>
<h2>Read this before you rely on it</h2><ul>{notes}</ul>
<p class="foot">Made with <a href="{REPO_URL}">ModelSpend</a>: open source, runs on your machine, uses your own provider keys.</p>
</main></body></html>
"""
