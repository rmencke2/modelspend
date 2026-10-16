"""Runtime configuration. API keys live only in memory (from the environment); they are never
written to SQLite, logs, repr() output or error messages."""
from __future__ import annotations

import json
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


KEYS_FILE = "keys.env"  # managed by `modelspend keys set`; KEY=VALUE, chmod 600, never logged or stored in SQLite
SETTINGS_FILE = "settings.json"  # managed by `modelspend providers|models enable|disable`


def default_home(env: dict[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    return Path(e["MODELSPEND_HOME"]) if e.get("MODELSPEND_HOME") else Path.home() / ".modelspend"


def private_mkdir(path: Path) -> None:
    """Create a directory only the owner can enter (the home dir holds keys, usage metadata and settings)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def keys_path(home: Path) -> Path:
    return home / KEYS_FILE


def read_keys_file(home: Path) -> dict[str, str]:
    p = keys_path(home)
    return parse_env_file(p) if p.exists() else {}


def write_keys_file(home: Path, data: dict[str, str]) -> Path:
    p = keys_path(home)
    private_mkdir(p.parent)
    try:
        os.chmod(p.parent, 0o700)  # a keys file inside a world-readable directory is only half protected
    except OSError:
        pass
    body = "# ModelSpend API keys (plain text, readable only by you). Real environment variables override these.\n"
    body += "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
    # write a temp file (created 0600, so the key is never briefly readable by others), fsync, then replace atomically:
    # a crash mid-write can no longer wipe the keys that were already saved
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    return p


def settings_path(home: Path) -> Path:
    return home / SETTINGS_FILE


def load_settings(home: Path) -> dict:
    p = settings_path(home)
    return json.loads(p.read_text()) if p.exists() else {}


def save_settings(home: Path, data: dict) -> Path:
    p = settings_path(home)
    private_mkdir(p.parent)
    p.write_text(json.dumps(data, indent=2, sort_keys=True))
    return p


def _bool(v: str | None, default: bool = False) -> bool:
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ShadowSettings:
    """Shadow evaluation (see shadow.py): off by default, opt-in, capped. Lives under "shadow" in settings.json."""
    enabled: bool = False
    sample_rate: float = 0.05  # fraction of eligible requests that are also tried on cheaper models in the background
    monthly_budget_usd: float = 5.0  # hard ceiling on shadow spend per UTC month (this process; see docs/shadow.md)
    max_candidates: int = 2  # cheaper models tried per sampled request
    judge: bool = True  # when there are no validators and the answers differ, ask a model whether they are equivalent (costs money)
    include_sensitive: bool = False  # requests marked sensitive are never shadowed unless this is true
    use_as_evidence: bool = False  # let routing use shadow results as evidence where you have no eval evidence (`modelspend shadow apply`)
    target_n: int = 100  # stop sampling a (model, task) pair once it has this many scored comparisons

    def validate(self) -> "ShadowSettings":
        if not 0 <= self.sample_rate <= 1:
            raise ValueError("shadow.sample_rate must be between 0 and 1 (0.05 = 5% of requests)")
        if self.monthly_budget_usd < 0:
            raise ValueError("shadow.monthly_budget_usd must be zero or more")
        if not 1 <= self.max_candidates <= 5:
            raise ValueError("shadow.max_candidates must be between 1 and 5")
        if not 10 <= self.target_n <= 10000:
            raise ValueError("shadow.target_n must be between 10 and 10000")
        return self


_SHADOW_KEYS = {f for f in ShadowSettings.__dataclass_fields__}


def parse_shadow(raw: object, env_enabled: str | None = None) -> ShadowSettings:
    """settings.json "shadow" block (+ MODELSPEND_SHADOW=true|false). Unknown or mistyped keys raise: a typo in a spending limit must not be ignored."""
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("settings.json: 'shadow' must be an object")
    raw = dict(raw or {})
    unknown = sorted(set(raw) - _SHADOW_KEYS)
    if unknown:
        raise ValueError(f"settings.json: unknown shadow setting(s) {', '.join(unknown)}; valid: {', '.join(sorted(_SHADOW_KEYS))}")
    try:
        s = ShadowSettings(
            enabled=_strict_bool(raw.get("enabled", False), "enabled"), sample_rate=float(raw.get("sample_rate", 0.05)),
            monthly_budget_usd=float(raw.get("monthly_budget_usd", 5.0)), max_candidates=int(raw.get("max_candidates", 2)),
            judge=_strict_bool(raw.get("judge", True), "judge"), include_sensitive=_strict_bool(raw.get("include_sensitive", False), "include_sensitive"),
            use_as_evidence=_strict_bool(raw.get("use_as_evidence", False), "use_as_evidence"), target_n=int(raw.get("target_n", 100)))
    except (TypeError, ValueError) as e:
        raise ValueError(f"settings.json: bad shadow setting ({e})") from None
    if env_enabled is not None and env_enabled.strip():
        s.enabled = _bool(env_enabled)
    return s.validate()


def _strict_bool(v: object, name: str) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError(f"shadow.{name} must be true or false")


def _load_thresholds(settings: dict) -> dict:
    """Parse the user's evidence bars. A typo in a quality bar must not be silently ignored, so errors are raised."""
    from .policy import parse_thresholds
    try:
        return parse_thresholds(settings.get("thresholds"))
    except ValueError as e:
        raise ValueError(f"settings.json: {e}") from None


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path.home() / ".modelspend")
    db_path: Path | None = None
    api_keys: dict[str, str] = field(default_factory=dict, repr=False)
    default_providers: list[str] = field(default_factory=list)
    key_sources: dict[str, str] = field(default_factory=dict)  # provider -> 'environment' | 'keys file' (never the key)
    allow_models: list[str] = field(default_factory=list)  # default allowlist of 'provider/model' refs (empty = all)
    deny_models: list[str] = field(default_factory=list)  # default denylist of 'provider/model' refs
    catalog_path: Path | None = None
    overrides_path: Path | None = None
    evidence_path: Path | None = None
    max_pricing_age_days: int = 30
    exclude_stale_pricing: bool = False
    allow_preview_models: bool = False
    allow_unpriced: bool = False  # run models without catalog pricing (cost reported as unknown)
    aliases: dict[str, str] = field(default_factory=dict)  # name -> 'provider/model-id' (see aliases.py); a pinned, unrouted model
    thresholds: dict = field(default_factory=dict)  # user-defined evidence bars per task category (see policy.Threshold); from settings.json
    shadow: ShadowSettings = field(default_factory=ShadowSettings)  # background comparison of cheaper models on real requests (shadow.py)
    sticky: bool = True  # keep the model chosen for earlier turns of a conversation (MODELSPEND_STICKY=false to turn off)
    default_project: str | None = None  # attribution defaults (MODELSPEND_PROJECT / MODELSPEND_AGENT)
    default_agent: str | None = None
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
        from . import redact
        redact.register(*self.api_keys.values(), self.api_token)
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
        home = default_home(e)
        filekeys = read_keys_file(home)
        settings = load_settings(home)
        keys: dict[str, str] = {}
        key_sources: dict[str, str] = {}
        for prov, names in KEY_ENV.items():
            for n in names:  # real environment wins over the keys file
                if e.get(n):
                    keys[prov], key_sources[prov] = e[n], "environment"
                    break
            else:
                for n in names:
                    if filekeys.get(n):
                        keys[prov], key_sources[prov] = filekeys[n], "keys file"
                        break
        if "MODELSPEND_PROVIDERS" in e:
            provs = [normalize_provider(p) for p in e["MODELSPEND_PROVIDERS"].split(",") if p.strip()]
        else:
            provs = [normalize_provider(p) for p in settings.get("providers", [])]
        if "MODELSPEND_SENSITIVE_PROVIDERS" in e:
            sens = [normalize_provider(p) for p in e["MODELSPEND_SENSITIVE_PROVIDERS"].split(",") if p.strip()]
        else:
            sens = [normalize_provider(p) for p in settings.get("sensitive_providers", [])]
        kw: dict = {"home": home}
        if e.get("MODELSPEND_DB"):
            kw["db_path"] = Path(e["MODELSPEND_DB"])
        if e.get("MODELSPEND_CATALOG"):
            kw["catalog_path"] = Path(e["MODELSPEND_CATALOG"])
        if e.get("MODELSPEND_OVERRIDES"):
            kw["overrides_path"] = Path(e["MODELSPEND_OVERRIDES"])
        return cls(
            api_keys=keys,
            key_sources=key_sources,
            aliases={str(k): str(v) for k, v in (settings.get("aliases") or {}).items()},
            thresholds=_load_thresholds(settings),
            shadow=parse_shadow(settings.get("shadow"), e.get("MODELSPEND_SHADOW")),
            sticky=_bool(e.get("MODELSPEND_STICKY"), bool(settings.get("sticky_conversations", True))),
            allow_models=list(settings.get("allow_models", [])),
            deny_models=list(settings.get("deny_models", [])),
            default_providers=provs,
            sensitive_providers=sens,
            max_pricing_age_days=int(e.get("MODELSPEND_MAX_PRICING_AGE_DAYS", 30)),
            exclude_stale_pricing=_bool(e.get("MODELSPEND_EXCLUDE_STALE_PRICING")),
            allow_preview_models=_bool(e.get("MODELSPEND_ALLOW_PREVIEW")),
            allow_unpriced=_bool(e.get("MODELSPEND_ALLOW_UNPRICED")),
            default_project=(e.get("MODELSPEND_PROJECT") or None), default_agent=(e.get("MODELSPEND_AGENT") or None),
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
