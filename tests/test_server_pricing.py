"""Dashboard 'Pricing' API: sources, refresh, proposals, and the browser-safety guard."""
import json

import pytest
from fastapi.testclient import TestClient

from modelspend import Catalog
from modelspend.server.app import create_app
from test_pricing_refresh import FILLER, Rig, page

H = {"Content-Type": "application/json"}


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def api(rig, monkeypatch):
    monkeypatch.setattr("modelspend.pricing_refresh.fetch", rig.fetcher)
    monkeypatch.setattr("modelspend.sources.fetch", rig.fetcher)
    reloads = []
    def reload():
        reloads.append(1)
        return Catalog.from_config(rig.cfg)
    c = TestClient(create_app(rig.engine, rig.cfg, reload_catalog=reload), base_url="http://127.0.0.1:8787")
    c.reloads = reloads
    return c


def test_sources_get_put_reset_and_ssrf_guard(api):
    j = api.get("/v1/sources").json()["providers"]
    assert [p["provider"] for p in j] == ["anthropic", "openai", "google", "xai"] and j[0]["origin"] == "shipped"
    r = api.put("/v1/sources/openai", json={"url": "https://example.test/pricing?utm_source=chatgpt.com"}, headers=H)
    row = {p["provider"]: p for p in r.json()["providers"]}["openai"]
    assert r.status_code == 200 and row["pricing_url"] == "https://example.test/pricing" and row["origin"] == "user"
    for bad in ("http://127.0.0.1:8080/x", "http://10.0.0.5/pricing", "http://169.254.169.254/latest/meta-data", "not a url"):
        r = api.put("/v1/sources/openai", json={"url": bad}, headers=H)
        assert r.status_code == 400, bad
    r = api.put("/v1/sources/openai", json={"url": None}, headers=H)
    assert {p["provider"]: p for p in r.json()["providers"]}["openai"]["origin"] == "shipped"
    assert api.put("/v1/sources/nonsense", json={"url": "https://x.test"}, headers=H).status_code == 400


def test_check_endpoint_reports_baseline_and_change(api, rig):
    assert api.post("/v1/sources/anthropic/check", json={"accept": True}).json()["accepted"] is True
    assert api.post("/v1/sources/anthropic/check", json={}).json()["state"] == "unchanged"
    rig.html = page(("Claude Haiku 5.5", "0.11", "0.50"))
    assert api.post("/v1/sources/anthropic/check", json={}).json()["state"] == "CHANGED"
    assert api.get("/v1/sources").json()["providers"][0]["baseline_accepted_on"]


def test_refresh_review_apply_flow_reloads_the_live_catalog(api, rig):
    rig.html = page(("Claude Haiku 5.5", "0.12", "0.50"))
    rig.extraction = {"models": [{"name": "Claude Haiku 5.5", "input_per_mtok": 0.12, "output_per_mtok": 0.50}]}
    r = api.post("/v1/pricing/refresh", json={"provider": "anthropic"}).json()
    assert r["state"] == "proposed" and r["extractor"]["total_tokens"] == 5300
    props = api.get("/v1/pricing/proposals").json()["proposals"]
    assert len(props) == 1 and props[0]["changes"][0]["fields"][0]["new"] == 0.12
    live = lambda: rig.engine.catalog.get("anthropic/claude-haiku-5-5").pricing.input_per_mtok  # noqa: E731
    assert live() == 0.10
    a = api.post(f"/v1/pricing/proposals/{props[0]['id']}/apply", json={})
    assert a.status_code == 200 and a.json()["status"] == "applied" and api.reloads == [1] and live() == 0.12
    assert api.post(f"/v1/pricing/proposals/{props[0]['id']}/apply", json={}).status_code == 409
    assert api.get("/v1/pricing/proposals").json()["proposals"] == []
    assert api.post("/v1/pricing/refresh", json={"provider": "anthropic"}).json()["state"] == "unchanged"


def test_reject_unknown_and_bad_actions(api, rig):
    rig.extraction = {"models": [{"name": "Claude Sonnet 5.5", "input_per_mtok": 2.5, "output_per_mtok": 10}]}
    r = api.post("/v1/pricing/refresh", json={"provider": "anthropic", "text": FILLER + "\nClaude Sonnet 5.5 | $2.5 | $10"}).json()
    assert api.post(f"/v1/pricing/proposals/{r['proposal_id']}/reject", json={}).json()["status"] == "rejected"
    assert api.post("/v1/pricing/proposals/nope/apply", json={}).status_code == 404
    assert api.post("/v1/pricing/proposals/x/explode", json={}).status_code == 404
    assert api.post("/v1/pricing/refresh", json={"provider": "bogus"}).status_code == 400


def test_page_derived_strings_come_back_as_data_not_markup(api, rig):
    rig.html = page(("Claude Haiku 5.5", "0.12", "0.50"))
    rig.extraction = {"models": [{"name": "Claude Haiku 5.5", "input_per_mtok": 0.12, "output_per_mtok": 0.50},
                                 {"name": "<script>alert(1)</script>", "input_per_mtok": 1, "output_per_mtok": 2}]}
    api.post("/v1/pricing/refresh", json={"provider": "anthropic"})
    body = api.get("/v1/pricing/proposals")
    assert body.headers["content-type"].startswith("application/json")  # never served as HTML
    assert "<script>alert(1)</script>" in json.dumps(body.json()["proposals"][0]["unverified"][0]["name"])  # kept as text
    html = api.get("/dashboard").text
    assert "esc(" in html and "innerHTML" in html and "Pricing" in html and "Paste pricing text" in html


# ---------------------------------------------------------------- browser-safety guard
def test_guard_refuses_cross_site_style_requests_without_a_token(api, rig):
    body = json.dumps({"provider": "anthropic"})
    assert api.post("/v1/pricing/refresh", content=body, headers={"Content-Type": "text/plain"}).status_code == 415
    assert api.post("/v1/pricing/refresh", content=body, headers={**H, "Origin": "https://evil.example"}).status_code == 403
    assert api.post("/v1/pricing/refresh", content=body, headers={**H, "Host": "attacker.test"}).status_code == 403
    assert api.post("/v1/pricing/refresh", content=body, headers={**H, "Origin": "http://127.0.0.1:8787"}).status_code == 200
    # the spend endpoints are covered too: a web page must not be able to make you pay for tokens
    chat = {"model": "auto", "messages": [{"role": "user", "content": "hi"}], "modelspend": {"providers": ["anthropic"]}}
    assert api.post("/v1/chat/completions", content=json.dumps(chat), headers={"Content-Type": "text/plain"}).status_code == 415
    assert api.post("/v1/chat/completions", content=json.dumps(chat), headers={**H, "Origin": "https://evil.example"}).status_code == 403


def test_with_a_token_bearer_auth_replaces_the_origin_checks(rig, monkeypatch):
    monkeypatch.setattr("modelspend.pricing_refresh.fetch", rig.fetcher)
    rig.cfg.api_token = "s3cret"
    c = TestClient(create_app(rig.engine, rig.cfg), base_url="http://anything.internal")
    assert c.post("/v1/pricing/refresh", json={"provider": "anthropic"}).status_code == 401
    ok = c.post("/v1/pricing/refresh", json={"provider": "anthropic"}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert c.get("/v1/sources").status_code == 401 and c.get("/v1/pricing/proposals").status_code == 401


def test_allowed_hosts_env_extends_the_host_check(rig, monkeypatch):
    monkeypatch.setenv("MODELSPEND_ALLOWED_HOSTS", "modelspend.lan")
    c = TestClient(create_app(rig.engine, rig.cfg), base_url="http://modelspend.lan:8787")
    assert c.put("/v1/sources/openai", json={"url": None}, headers=H).status_code == 200
