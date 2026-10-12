"""Read/write the user's overrides.json and apply validated, revertible edits to it."""
from __future__ import annotations

import json
from datetime import date

from .catalog import Catalog
from .config import Config


def load_overrides(cfg: Config) -> dict:
    p = cfg.overrides_path
    return json.loads(p.read_text()) if p and p.exists() else {}


def save_overrides(cfg: Config, data: dict) -> None:
    p = cfg.overrides_path
    p.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    p.write_text(json.dumps(data, indent=2, sort_keys=True))  # type: ignore[union-attr]


def apply_assignments(cfg: Config, ref: str, assignments: list[str]) -> None:
    """Apply `dotted.key=json-value` edits to one model. The catalog is reloaded to validate; on failure the
    file is restored and ValueError is raised."""
    if ref not in {m.ref for m in Catalog.from_config(cfg).models}:
        raise ValueError(f"unknown model '{ref}'")
    ov = load_overrides(cfg)
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
    save_overrides(cfg, ov)
    try:
        Catalog.from_config(cfg)
    except Exception as e:  # noqa: BLE001
        save_overrides(cfg, json.loads(before))
        raise ValueError(f"invalid change, reverted: {str(e)[:300]}") from e


PRICE_FIELDS = ("input_per_mtok", "output_per_mtok", "cached_input_per_mtok", "cache_write_per_mtok")


def set_prices(cfg: Config, ref: str, prices: dict[str, float]) -> None:
    """Set price fields on one model and stamp today as the verification date."""
    parts = [f"pricing.{k}={v}" for k, v in prices.items() if k in PRICE_FIELDS and v is not None]
    parts.append(f'pricing.verified_on="{date.today()}"')
    apply_assignments(cfg, ref, parts)
