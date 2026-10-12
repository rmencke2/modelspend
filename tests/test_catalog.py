import json
from datetime import date

import pytest

from modelspend import Catalog
from modelspend.catalog import shipped_catalog_path, write_evidence
from modelspend.catalog.models import EvidenceItem
from modelspend.pricing import cost_for, estimate_cost
from modelspend.types import Usage


def test_shipped_catalog_is_anthropic_pricing_only_and_not_routable_until_synced():
    c = Catalog.load()
    assert {m.provider for m in c.models} == {"anthropic"} and not any(m.synthetic for m in c.models)
    for m in c.models:
        assert m.availability == "unverified" and m.limits.context_tokens is None  # IDs/limits unconfirmed until `catalog sync`
        assert m.pricing.source_url.startswith("https://platform.claude.com/") and m.pricing.verified_on is not None
        assert m.tier in (1, 2, 3) and "not a measurement" in m.tier_note
    h = c.get("anthropic/claude-haiku-5-5").pricing  # pinned to the official pricing page fetched 2026-10-12
    assert (h.input_per_mtok, h.output_per_mtok, h.cached_input_per_mtok, h.cache_write_per_mtok) == (0.10, 0.50, 0.01, 0.125)
    assert h.long_context_tiers[0].prompt_tokens_over == 100000 and h.long_context_tiers[0].input_per_mtok == 0.50
    assert [(m.pricing.input_per_mtok, m.pricing.output_per_mtok) for m in c.models if m.id != "claude-haiku-5-5"] == [(2.0, 10.0), (4.0, 20.0), (10.0, 50.0)]
    assert not [i for i in c.validate() if i.severity == "error"]
    assert all(i.severity != "error" for i in c.validate())


def test_synthetic_catalog_validates(catalog):
    issues = catalog.validate(today=date(2026, 10, 12))
    assert not [i for i in issues if i.severity == "error"]
    assert any("pricing unknown" in i.message for i in issues)


def test_stale_detection(catalog):
    assert not catalog.stale(date(2026, 10, 20))
    assert catalog.stale(date(2027, 3, 1))


def test_overrides_change_price_and_availability(tmp_path):
    ov = {"models": {"openai/synthetic-openai-small": {"availability": "unavailable",
                                                      "pricing": {"input_per_mtok": 9.0, "verified_on": "2026-10-12"}}}}
    c = Catalog.load(shipped_catalog_path("synthetic_test_catalog.json"), overrides=ov)
    m = c.get("openai/synthetic-openai-small")
    assert m.availability == "unavailable" and m.pricing.input_per_mtok == 9.0
    assert m.pricing.output_per_mtok == 0.40 and m.pricing.origin == "user_override"
    base = Catalog.load(shipped_catalog_path("synthetic_test_catalog.json"))
    assert c.version != base.version  # content hash reflects overrides


def test_override_unknown_model_rejected():
    with pytest.raises(ValueError):
        Catalog.load(shipped_catalog_path("synthetic_test_catalog.json"), overrides={"models": {"openai/nope": {}}})


def test_add_models_and_evidence_overlay(tmp_path):
    new = {"provider": "openai", "id": "my-model", "availability": "available", "verified_on": "2026-10-12",
           "limits": {"context_tokens": 1000, "max_output_tokens": 100}, "tier": 1,
           "pricing": {"input_per_mtok": 1, "output_per_mtok": 2, "verified_on": "2026-10-12"}}
    p = tmp_path / "ev.json"
    write_evidence(p, {"openai/my-model": {"code": EvidenceItem(n=50, passes=45, dataset="d")}})
    c = Catalog.load(shipped_catalog_path("synthetic_test_catalog.json"), overrides={"add_models": [new]}, evidence=p)
    assert c.get("openai/my-model").evidence["code"].passes == 45


def test_cost_math_with_cache_and_tiers(catalog):
    m = catalog.get("openai/synthetic-openai-mid")  # in 0.5, out 2.0, cached 0.05, write 0.6; >50k prompt: 1.0/4.0
    u = Usage(input_tokens=1000, cache_read_tokens=2000, cache_write_tokens=500, output_tokens=300)
    assert cost_for(m, u) == pytest.approx((1000 * .5 + 2000 * .05 + 500 * .6 + 300 * 2.0) / 1e6)
    big = Usage(input_tokens=60000, output_tokens=1000)
    assert cost_for(m, big) == pytest.approx((60000 * 1.0 + 1000 * 4.0) / 1e6)


def test_unknown_pricing_raises(catalog):
    with pytest.raises(ValueError):
        estimate_cost(catalog.get("xai/synthetic-xai-unpriced"), 10, 10)


def test_catalog_validate_flags_errors():
    bad = {"catalog_version": "x", "models": [{"provider": "openai", "id": "a", "availability": "available",
                                               "pricing": {"input_per_mtok": 1, "output_per_mtok": 1}}]}
    import tempfile, pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / "c.json"
    p.write_text(json.dumps(bad))
    msgs = [str(i) for i in Catalog.load(p, sources={}).validate()]  # no provider pricing URL configured
    assert any("verified_on" in m for m in msgs) and any("source_url" in m for m in msgs)
