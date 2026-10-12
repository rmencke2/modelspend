"""Runtime configuration. API keys live only in memory (from the environment); they are never
written to SQLite, logs, repr() output or error messages."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .types import PROVIDERS, normalize_provider

KEY_ENV = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "xai": ("XAI_API_KEY",),
}


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (no interpolation). Used by `--env-file`."""
    out: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _bool(v: str | None, default: bool = False) -> bool:
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path.home() / ".modelspend")
    db_path: Path | None = None
    api_keys: dict[str, str] = field(default_factory=dict, repr=False)
    default_providers: list[str] = field(default_factory=list)
    catalog_path: Path | None = None
    overrides_path: Path | None = None
    evidence_path: Path | None = None
    max_pricing_age_days: int = 30
    exclude_stale_pricing: bool = False
    allow_preview_models: bool = False
    allow_unpriced: bool = False  # run models without catalog pricing (cost reported as unknown)
    default_max_output_tokens: int = 1024
    reasoning_overhead_tokens: int = 1024
    max_retries: int = 2
    retry_backoff_s: float = 0.5
    request_timeout_s: float = 60.0
    exact_token_counts: bool = False
    classifier_model_enabled: bool = False
    sensitive_providers: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8787
    api_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.home = Path(self.home)
        if self.db_path is None:
            self.db_path = self.home / "modelspend.db"
        if self.overrides_path is None:
            self.overrides_path = self.home / "overrides.json"
        if self.evidence_path is None:
            self.evidence_path = self.home / "evidence.json"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        e = dict(os.environ if env is None else env)
        keys = {}
        for prov, names in KEY_ENV.items():
            for n in names:
                if e.get(n):
                    keys[prov] = e[n]
                    break
        provs = [normalize_provider(p) for p in e.get("MODELSPEND_PROVIDERS", "").split(",") if p.strip()]
        sens = [normalize_provider(p) for p in e.get("MODELSPEND_SENSITIVE_PROVIDERS", "").split(",") if p.strip()]
        kw: dict = {}
        if e.get("MODELSPEND_HOME"):
            kw["home"] = Path(e["MODELSPEND_HOME"])
        if e.get("MODELSPEND_DB"):
            kw["db_path"] = Path(e["MODELSPEND_DB"])
        if e.get("MODELSPEND_CATALOG"):
            kw["catalog_path"] = Path(e["MODELSPEND_CATALOG"])
        if e.get("MODELSPEND_OVERRIDES"):
            kw["overrides_path"] = Path(e["MODELSPEND_OVERRIDES"])
        return cls(
            api_keys=keys,
            default_providers=provs,
            sensitive_providers=sens,
            max_pricing_age_days=int(e.get("MODELSPEND_MAX_PRICING_AGE_DAYS", 30)),
            exclude_stale_pricing=_bool(e.get("MODELSPEND_EXCLUDE_STALE_PRICING")),
            allow_preview_models=_bool(e.get("MODELSPEND_ALLOW_PREVIEW")),
            allow_unpriced=_bool(e.get("MODELSPEND_ALLOW_UNPRICED")),
            exact_token_counts=_bool(e.get("MODELSPEND_EXACT_TOKEN_COUNTS")),
            classifier_model_enabled=_bool(e.get("MODELSPEND_CLASSIFIER_MODEL")),
            host=e.get("MODELSPEND_HOST", "127.0.0.1"),
            port=int(e.get("MODELSPEND_PORT", 8787)),
            api_token=e.get("MODELSPEND_API_TOKEN") or None,
            **kw,
        )

    def configured_providers(self) -> list[str]:
        return [p for p in PROVIDERS if p in self.api_keys]


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")
