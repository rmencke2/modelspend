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
    from .config import private_mkdir as _pm
    _pm(p.parent)  # type: ignore[union-attr]
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


def remove_added_models(cfg: Config, patterns: list[str], apply: bool = False) -> dict:
    """Remove models YOU added (by `catalog sync --add-new`, `import-openrouter` or hand) whose `provider/id` matches a pattern (`*` and `?` work).
    Shipped models are never touched: they are not in your overrides, and `models disable` is the way to keep one out of routing. Hand-set patches on a removed
    model go with it (a patch for a model that no longer exists would make the whole catalog refuse to load). With apply=False nothing is written."""
    import fnmatch
    import shutil
    pats = [p.strip() for p in patterns if p and p.strip()]
    if not pats:
        raise ValueError("give at least one model or pattern, e.g. 'openrouter/*:batch'")
    ov = load_overrides(cfg)
    added = {f"{m['provider']}/{m['id']}" for m in ov.get("add_models") or []}

    def match(ref: str) -> bool:
        return any(fnmatch.fnmatchcase(ref, p) for p in pats)

    hit = sorted(r for r in added if match(r))
    shipped = sorted(r for r in {m.ref for m in Catalog.from_config(cfg).models} - added if match(r))
    aliases = sorted(a for a, target in cfg.aliases.items() if target in hit)
    if apply and hit:
        before = json.dumps(ov, sort_keys=True)
        p = cfg.overrides_path
        shutil.copy2(p, p.with_name(p.name + ".bak"))  # type: ignore[union-attr]
        ov["add_models"] = [m for m in ov["add_models"] if f"{m['provider']}/{m['id']}" not in hit]
        if ov.get("models"):
            ov["models"] = {k: v for k, v in ov["models"].items() if k not in hit}
        save_overrides(cfg, ov)
        try:
            Catalog.from_config(cfg)
        except Exception as e:  # noqa: BLE001
            save_overrides(cfg, json.loads(before))
            raise ValueError(f"the catalog would not load after the removal, so nothing was changed: {str(e)[:300]}") from e
    return {"removed": hit, "not_added": shipped, "aliases": aliases}


PRICE_FIELDS = ("input_per_mtok", "output_per_mtok", "cached_input_per_mtok", "cache_write_per_mtok")


def anchors_path(cfg: Config):
    return cfg.home / "price_anchors.json"


def load_anchors(cfg: Config) -> dict[str, dict[str, float]]:
    p = anchors_path(cfg)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_anchors(cfg: Config, data: dict) -> None:
    from .config import private_mkdir
    private_mkdir(cfg.home)
    anchors_path(cfg).write_text(json.dumps(data, indent=2, sort_keys=True))


def set_prices(cfg: Config, ref: str, prices: dict[str, float], *, human: bool = True,
               anchor_defaults: dict[str, float] | None = None) -> None:
    """Set price fields on one model and stamp today as the verification date.

    `anchors` remember the last price a HUMAN approved for each field. Unattended (auto-apply) updates are measured against the
    anchor, not against the current price, so a series of small unattended changes cannot ratchet a price far from what a person
    reviewed. A human edit (human=True) moves the anchor; an unattended one never does. `anchor_defaults` seeds an anchor that does
    not exist yet (the price before the first unattended change)."""
    parts = [f"pricing.{k}={v}" for k, v in prices.items() if k in PRICE_FIELDS and v is not None]
    parts.append(f'pricing.verified_on="{date.today()}"')
    apply_assignments(cfg, ref, parts)
    anchors = load_anchors(cfg)
    slot = anchors.setdefault(ref, {})
    if human:
        slot.update({k: v for k, v in prices.items() if k in PRICE_FIELDS and v is not None})
    else:
        for k, v in (anchor_defaults or {}).items():
            if v is not None and k not in slot:
                slot[k] = v
    _save_anchors(cfg, anchors)
