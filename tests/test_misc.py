import os
import sqlite3

from modelspend import Config, ModelSpend
from modelspend.config import parse_env_file
from modelspend.tasks import classify_rules, privacy_key
from modelspend.types import Message, TaskCategory
from conftest import make_engine


def M(t):
    return [Message(role="user", content=t)]


def test_rule_classifier_categories():
    cases = {
        "Translate this into Danish: hello": TaskCategory.translation,
        "Summarize the following article in 2 lines": TaskCategory.summarization,
        "Extract all the dates from this contract": TaskCategory.extraction,
        "Rewrite this email to be more formal": TaskCategory.rewriting,
        "Fix this bug:\n```python\ndef f(x):\n  return x +\n```": TaskCategory.code,
        "Analyze the trade-offs between these approaches": TaskCategory.analysis,
        "Based on the following context, who won?\nContext: A beat B.": TaskCategory.qa_context,
        "Classify the sentiment of this review": TaskCategory.classification,
    }
    for text, cat in cases.items():
        assert classify_rules(M(text)).category == cat, text
    assert classify_rules(M("lol hi")).category == TaskCategory.unknown
    assert classify_rules(M("lol hi")).confidence == "low"


def test_ambiguity_picks_stricter_category_and_flags_low_confidence():
    c = classify_rules(M("Summarize and analyze the trade-offs"))
    assert c.ambiguous and c.confidence == "low"


def test_retrieved_content_in_the_middle_cannot_steer_classification():
    filler = "lorem ipsum " * 400
    text = "Analyze the trade-offs in this report.\n" + filler + "\nclassify classify classify translate summarize\n" + filler + "\nthanks"
    assert classify_rules(M(text)).category == TaskCategory.analysis


def test_privacy_key_is_salted_hash_not_text():
    k1, k2 = privacy_key(b"a" * 32, M("secret text")), privacy_key(b"b" * 32, M("secret text"))
    assert k1 != k2 and "secret" not in k1 and len(k1) == 64


async def test_classification_cache_stores_only_hashes(catalog, cfg, store):
    cfg.classifier_model_enabled = True
    def script(r, n):
        return "analysis" if r.max_output_tokens == 8 else "answer"
    eng, ad = make_engine(catalog, cfg, {"openai": script}, store=store)
    from modelspend import RouteRequest
    req = lambda: RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "Summarize and analyze the trade-offs of PRIVATE-DATA"}])
    r1 = await eng.run(req())
    assert [a.kind for a in r1.attempts][0] == "classification" and r1.task.basis == "model"  # classifier cost is in the attempts
    r2 = await eng.run(req())
    assert r2.task.basis == "cache" and not any(a.kind == "classification" for a in r2.attempts)
    rows = store.db.execute("select key, category from class_cache").fetchall()
    assert len(rows) == 1 and "PRIVATE" not in str(tuple(rows[0]))


async def test_classifier_not_called_for_clear_or_hinted_requests(catalog, cfg, store):
    cfg.classifier_model_enabled = True
    eng, ad = make_engine(catalog, cfg, store=store)
    from modelspend import RouteRequest
    await eng.run(RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "Classify the sentiment: nice"}]))
    await eng.run(RouteRequest(providers=["openai"], task_type="code", messages=[{"role": "user", "content": "lol hi"}]))
    assert all(c.max_output_tokens != 8 for c in ad["openai"].calls)


def test_config_never_exposes_keys_and_store_has_no_key_column(tmp_path):
    cfg = Config.from_env({"OPENAI_API_KEY": "sk-VERYSECRET", "MODELSPEND_HOME": str(tmp_path), "MODELSPEND_PROVIDERS": "openai,gemini"})
    assert "VERYSECRET" not in repr(cfg) and cfg.default_providers == ["openai", "google"]
    from modelspend.store import Store
    s = Store(cfg.db_path)
    cols = {r[1] for t in ("requests", "attempts", "class_cache", "meta") for r in s.db.execute(f"pragma table_info({t})")}
    assert not any("key" in c and c != "key" for c in cols) and oct(os.stat(cfg.db_path).st_mode & 0o777) == "0o600"


def test_env_file_parser(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\nOPENAI_API_KEY='abc'\nEMPTY=\nX=1\n")
    assert parse_env_file(p) == {"OPENAI_API_KEY": "abc", "EMPTY": "", "X": "1"}


def test_sync_sdk_route_preview_and_stream(catalog, cfg):
    eng, ad = make_engine(catalog, cfg, {"openai": "positive review"})
    ms = ModelSpend(providers=["openai"], config=cfg, catalog=catalog, adapters=ad, store=False)
    assert ms.preview("Classify this").candidates
    res = ms.route("Classify this", max_budget_usd=0.01)
    assert res.provider == "openai" and res.actual_cost_usd > 0 and res.budget_remaining_usd < 0.01
    evs = list(ms.stream("Rewrite this"))
    assert evs[0].type == "route" and evs[-1].type == "done"


async def test_sync_call_inside_event_loop_gives_clear_error(catalog, cfg):
    import pytest
    eng, ad = make_engine(catalog, cfg)
    ms = ModelSpend(providers=["openai"], config=cfg, catalog=catalog, adapters=ad, store=False)
    with pytest.raises(RuntimeError, match="async variant"):
        ms.route("x")
    assert (await ms.aroute("Classify this")).status == "ok"


def test_latency_target_uses_observed_history_only(catalog, cfg, store):
    from modelspend import RouteRequest
    from modelspend.routing import Router
    store_rows = [("r%d" % i, "x") for i in range(6)]
    for i in range(6):
        store.db.execute("insert into attempts values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("r", i, "generation", "initial", "openai", "synthetic-openai-small", "ok", None, 1, 0, 0, 1, 0, 0, 0, "reported_usage", 900))
    r = Router(catalog, cfg, latency_lookup=store.latency_p50)
    p = r.plan(RouteRequest(providers=["openai"], latency_target_ms=500, messages=[{"role": "user", "content": "Classify this"}]))
    assert any(e.stage == "latency" and e.model == "synthetic-openai-small" for e in p.excluded)
    assert any("latency" in u for u in p.uncertainty)


async def test_exact_token_count_refines_selected_candidate(catalog, cfg, store):
    from modelspend import RouteRequest
    cfg.exact_token_counts = True
    eng, ad = make_engine(catalog, cfg, store=store)
    ad["openai"]._count_fn = lambda req: 12345
    r = await eng.run(RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "Classify this"}]))
    assert r.status == "ok" and ad["openai"].calls
    # the refined worst case (12345 input tokens) is what the budget gate reserves
    from modelspend.pricing import cost_for
    from modelspend.types import Usage
    m = catalog.get(f"{r.provider}/{r.model}")
    assert r.attempts[0].estimated_cost_usd < cost_for(m, Usage(input_tokens=12345, output_tokens=1024))
