from __future__ import annotations

import json
from datetime import date

import pytest

from modelspend import Catalog, Config
from modelspend.catalog import shipped_catalog_path
from modelspend.engine import Engine
from modelspend.providers.mock import ScriptedAdapter
from modelspend.store import Store

TODAY = date(2026, 10, 12)

_ISOLATED_ENV = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "MODELSPEND_PROVIDERS",
                 "MODELSPEND_SENSITIVE_PROVIDERS", "MODELSPEND_ALLOW_UNPRICED", "MODELSPEND_API_TOKEN", "MODELSPEND_DB",
                 "MODELSPEND_CATALOG", "MODELSPEND_OVERRIDES", "MODELSPEND_EXACT_TOKEN_COUNTS", "MODELSPEND_CLASSIFIER_MODEL"] + \
                [f"MODELSPEND_PRICING_URL_{p}" for p in ("ANTHROPIC", "OPENAI", "GOOGLE", "XAI")]


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path, monkeypatch):
    """Tests never see the developer's real keys, settings, overrides or database."""
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path / "ms-home"))
    for k in _ISOLATED_ENV:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _provider_registry_is_per_test():
    """User-defined providers live in process-wide state (registry.py); no test may leak them into the next."""
    from modelspend import registry
    registry.register_custom({})
    yield
    registry.register_custom({})


@pytest.fixture
def catalog():
    return Catalog.load(shipped_catalog_path("synthetic_test_catalog.json"))


@pytest.fixture
def cfg(tmp_path):
    keys = {p: "test-key" for p in ("openai", "anthropic", "google", "xai")}
    return Config(home=tmp_path, api_keys=keys, retry_backoff_s=0.0, max_retries=2)


@pytest.fixture
def store():
    return Store(":memory:")


async def _nosleep(_s):
    return None


def make_engine(catalog, cfg, scripts=None, store=None, **kw):
    """Engine with one ScriptedAdapter per provider. `scripts` maps provider -> script fn or str."""
    scripts = scripts or {}
    adapters = {}
    for p in ("openai", "anthropic", "google", "xai"):
        s = scripts.get(p, "ok")
        adapters[p] = ScriptedAdapter(p, (lambda r, n, s=s: s(r, n) if callable(s) else s))
    eng = Engine(catalog, cfg, adapters, store=store, sleep=_nosleep, sandbox_runner=None, **kw)
    return eng, adapters


@pytest.fixture
def engine_factory(catalog, cfg, store):
    def f(scripts=None, **kw):
        return make_engine(catalog, cfg, scripts, store=store, **kw)
    return f
