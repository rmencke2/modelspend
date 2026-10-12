"""Versioned, editable model catalog with user overrides and measured-evidence overlay.

Layers (later wins):  shipped catalog.json  ->  overrides.json (prices/availability/etc.)
                      ->  evidence.json (measured results written by `modelspend eval calibrate`)
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from ..config import Config
from ..sources import resolve_sources, shipped_sources
from .models import CatalogFile, EvidenceItem, Model

DATA = "modelspend.catalog.data"


def shipped_catalog_path(name: str = "catalog.json") -> Path:
    return Path(str(resources.files("modelspend.catalog").joinpath("data", name)))


@dataclass
class Issue:
    severity: str  # error | warning
    model: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper()}: {self.model}: {self.message}"


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Catalog:
    def __init__(self, models: list[Model], base_version: str, content_hash: str):
        self.models = models
        self.base_version = base_version
        self._by_ref = {m.ref: m for m in models}
        self.version = f"{base_version}+{content_hash[:8]}"

    # ---- loading -------------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        overrides: str | Path | dict | None = None,
        evidence: str | Path | dict | None = None,
        sources: dict[str, str] | None = None,
    ) -> "Catalog":
        raw = json.loads(Path(path or shipped_catalog_path()).read_text())
        raw_models = {f'{m["provider"]}/{m["id"]}': m for m in raw.get("models", [])}

        ov = _read_json(overrides)
        # add_models first, so `models` patches (catalog set/price/sync) can also edit models the user added
        for m in ov.get("add_models") or []:
            ref = f'{m["provider"]}/{m["id"]}'
            if "pricing" in m:
                m["pricing"] = {"origin": "user_override", **m["pricing"]}
            raw_models[ref] = m
        for ref, patch in (ov.get("models") or {}).items():
            if ref not in raw_models:
                raise ValueError(f"override targets unknown model '{ref}' (use add_models to add one)")
            patch = copy.deepcopy(patch)
            if "pricing" in patch:
                patch["pricing"] = {"origin": "user_override", **patch["pricing"]}
            raw_models[ref] = _deep_merge(raw_models[ref], patch)
            if "pricing" in patch and raw_models[ref].get("pricing"):
                raw_models[ref]["pricing"].setdefault("origin", "user_override")
        ev = _read_json(evidence)
        for ref, cats in (ev.get("evidence") or {}).items():
            if ref in raw_models:
                raw_models[ref].setdefault("evidence", {}).update(cats)

        # pricing without its own source_url uses the provider's configured pricing page (see sources.py)
        src = sources if sources is not None else shipped_sources()
        for m in raw_models.values():
            pr = m.get("pricing")
            if pr and not pr.get("source_url") and src.get(m["provider"]):
                pr["source_url"] = src[m["provider"]]
        merged = {**raw, "models": list(raw_models.values())}
        cf = CatalogFile.model_validate(merged)
        cf.validate_providers()
        digest = hashlib.sha256(json.dumps(merged, sort_keys=True, default=str).encode()).hexdigest()
        return cls(cf.models, cf.catalog_version, digest)

    @classmethod
    def from_config(cls, cfg: Config) -> "Catalog":
        ov = cfg.overrides_path if cfg.overrides_path and Path(cfg.overrides_path).exists() else None
        ev = cfg.evidence_path if cfg.evidence_path and Path(cfg.evidence_path).exists() else None
        return cls.load(cfg.catalog_path, ov, ev, {p: s.pricing_url for p, s in resolve_sources(cfg).items()})

    # ---- access --------------------------------------------------------------------------
    def get(self, ref: str) -> Model:
        return self._by_ref[ref]

    def for_providers(self, providers: list[str]) -> list[Model]:
        return [m for m in self.models if m.provider in providers]

    # ---- validation ----------------------------------------------------------------------
    def validate(self, today: date | None = None, max_age_days: int = 30) -> list[Issue]:
        today = today or date.today()
        issues: list[Issue] = []
        seen = set()
        for m in self.models:
            if m.ref in seen:
                issues.append(Issue("error", m.ref, "duplicate model entry"))
            seen.add(m.ref)
            if m.availability == "available" and m.verified_on is None:
                issues.append(Issue("error", m.ref, "marked available without a verified_on date"))
            if m.limits.context_tokens is None or m.limits.max_output_tokens is None:
                issues.append(Issue("warning", m.ref, "context/output limits unknown; model cannot be auto-selected"))
            p = m.pricing
            if p is None or not p.known:
                issues.append(Issue("warning", m.ref, "pricing unknown; excluded from automatic cost optimization"))
            else:
                if not p.source_url and p.origin == "catalog":
                    issues.append(Issue("error", m.ref, "pricing has no source_url"))
                if p.effective_date is None and p.origin == "catalog":
                    issues.append(Issue("warning", m.ref, "pricing has no effective_date"))
                age = pricing_age_days(m, today)
                if age is None:
                    issues.append(Issue("warning", m.ref, "pricing has no verified_on date; treat as stale"))
                elif age > max_age_days:
                    issues.append(Issue("warning", m.ref, f"pricing is stale ({age} days since verification)"))
                if p.currency != "USD":
                    issues.append(Issue("warning", m.ref, f"currency {p.currency}: budgets are USD; model excluded"))
            if m.tier is None and not m.evidence:
                issues.append(Issue("warning", m.ref, "no tier prior and no evidence; never selected automatically"))
            for cat, e in m.evidence.items():
                if e.passes > e.n or e.n < 0:
                    issues.append(Issue("error", m.ref, f"evidence for {cat}: passes > n"))
        return issues

    def stale(self, today: date | None = None, max_age_days: int = 30) -> list[Model]:
        today = today or date.today()
        out = []
        for m in self.models:
            if m.pricing and m.pricing.known:
                age = pricing_age_days(m, today)
                if age is None or age > max_age_days:
                    out.append(m)
        return out


def pricing_age_days(m: Model, today: date) -> int | None:
    if not m.pricing or m.pricing.verified_on is None:
        return None
    return (today - m.pricing.verified_on).days


def _read_json(src: str | Path | dict | None) -> dict[str, Any]:
    if src is None:
        return {}
    if isinstance(src, dict):
        return src
    return json.loads(Path(src).read_text())


def write_evidence(path: Path, updates: dict[str, dict[str, EvidenceItem]]) -> None:
    """Merge measured evidence into the user's evidence overlay file."""
    cur = _read_json(path) if path.exists() else {}
    ev = cur.setdefault("evidence", {})
    for ref, cats in updates.items():
        for cat, item in cats.items():
            ev.setdefault(ref, {})[cat] = json.loads(item.model_dump_json())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, indent=2, sort_keys=True))
