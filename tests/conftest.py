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
