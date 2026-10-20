"""The shareable report: safe to hand to someone (no prompts, keys, paths or, by default, project names), honest about being a projection,
static (no scripts, no network), and built from the same numbers as `savings` and `whatif`."""
from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import make_engine
from modelspend.catalog import Catalog
from modelspend.cli import app
from modelspend.config import Config
from modelspend.server.app import create_app
from modelspend.sharereport import build_share, to_html, to_markdown, window_label
from modelspend.store import Store

from test_whatif import CAT, attempt, priced


def seeded(project="acme-secret-client"):
    s = Store(":memory:")
    big, small = priced(2)
    for i in range(10):
        attempt(s, f"a{i}", big.ref, task="classification", cost=0.5, project=project)
    attempt(s, "z", big.ref, task="analysis", cost=2.0, project="modelspend-shadow")
    prov, model = small.ref.split("/", 1)
    for i in range(60):
        s.record_shadow(request_id=f"r{i}", project=None, agent=None, category="classification", used_provider="x", used_model="y", cand_provider=prov,
                        cand_model=model, method="exact", verdict="pass", reason="x", used_cost=1.0, cand_cost=0.1, judge_cost=0.0, cand_input=1, cand_output=1)
    return s


def share(s, **kw):
    return build_share(s, CAT, Config(home="/tmp/x"), window="Last 30 days", **kw)


def test_project_names_stay_out_by_default_and_come_in_only_when_asked():
    s = seeded()
    d = share(s)
    assert "acme-secret-client" not in to_html(d) and "acme-secret-client" not in to_markdown(d)
    d2 = share(s, include_projects=True)
    assert "acme-secret-client" in to_html(d2) and "acme-secret-client" in to_markdown(d2)
    assert "modelspend-shadow" not in to_html(d2)  # tooling projects are never listed


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.urls = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            self.attrs.append(k)
            if k in ("src", "href") and v:
                self.urls.append(v)


def test_the_html_is_static_scriptless_blocks_network_and_escapes_everything():
    s = seeded(project="<script>alert(1)</script>")
    html = to_html(share(s, include_projects=True, title='</title><script>alert(2)</script><img src=x onerror=alert(1)>'))
    p = _Tags()
    p.feed(html)
    assert not {"script", "img", "iframe", "object", "embed", "link", "form"} & set(p.tags)
    assert not [a for a in p.attrs if a.startswith("on")]  # no event handlers, even though the title and project tried to inject one
    assert p.urls == ["https://github.com/rmencke2/modelspend"]  # the one outbound link; nothing is fetched
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # shown as text
    assert "default-src 'none'" in html and "script-src" not in html


def test_the_headline_is_the_same_projection_savings_computes_and_is_labelled_a_projection():
    s = seeded()
    d = share(s)
    t = d["savings"]["totals"]
    assert t["projected_saving_usd"] > 0
    html = to_html(d)
    assert "A projection, not a measurement." in html
    assert f"${t['projected_saving_usd']:,.2f}" in html or f"${t['projected_saving_usd']:,.4f}" in html
    assert "Read this before you rely on it" in html and "projection" in to_markdown(d).lower()


def test_no_evidence_means_no_saving_claim():
    s = Store(":memory:")
    attempt(s, "a", priced()[0].ref, cost=1.0)
    html = to_html(share(s))
    assert "No task type has enough evidence yet" in html and "projected saving (" not in html


def test_an_empty_ledger_still_renders():
    d = share(Store(":memory:"))
    assert "No usage in this window." in to_html(d) and "# ModelSpend report" in to_markdown(d)


def test_window_label_only_echoes_validated_forms():
    assert window_label("30d") == "Last 30 days" and window_label("1d") == "Last 1 day" and window_label("2026-10-01") == "Since 2026-10-01"
    assert window_label("all") == "All recorded usage" and window_label("<b>x</b>") == "All recorded usage"


def test_markdown_tables_have_one_row_per_task_and_the_whatif_rows():
    md = to_markdown(share(seeded()))
    assert "| classification |" in md and "## The same tokens on other models" in md and "unmeasured" in md


def test_cli_writes_a_file_prints_to_stdout_and_rejects_bad_options(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    st = Store(tmp_path / "modelspend.db")
    attempt(st, "a", priced()[0].ref, project="p1")
    st.db.commit()
    out = tmp_path / "r.html"
    r = CliRunner().invoke(app, ["share", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert out.read_text().startswith("<!doctype html>") and "Look it over before you share it" in r.output and "project names" not in r.output
    assert CliRunner().invoke(app, ["share", "--out", "-", "--format", "md"]).output.startswith("# ModelSpend report")
    assert CliRunner().invoke(app, ["share", "--format", "pdf"]).exit_code == 2
    assert CliRunner().invoke(app, ["share", "--out", str(tmp_path)]).exit_code == 2  # a folder
    assert "p1" not in out.read_text()


def _client(tmp_path, token=None):
    cfg = Config(home=tmp_path, api_keys={"openai": "k"}, retry_backoff_s=0.0)
    cfg.api_token = token
    s = seeded()
    eng, _ = make_engine(CAT, cfg, {}, store=s)
    return TestClient(create_app(eng, cfg), base_url="http://127.0.0.1")


def test_endpoint_serves_a_download_and_validates_its_query(tmp_path):
    c = _client(tmp_path)
    r = c.get("/v1/share?format=html&since=7d")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "attachment" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff" and "acme-secret-client" not in r.text
    assert c.get("/v1/share?format=md").text.startswith("# ModelSpend report")
    assert c.get("/v1/share?format=pdf").status_code == 400 and c.get("/v1/share?bogus=1").status_code == 400
    assert c.get("/v1/share?since=garbage").status_code == 400


def test_endpoint_needs_the_token_when_one_is_set(tmp_path):
    c = _client(tmp_path, token="secret-token-123")
    assert c.get("/v1/share").status_code == 401
    assert c.get("/v1/share", headers={"Authorization": "Bearer secret-token-123"}).status_code == 200


def test_notes_do_not_repeat_and_money_precision_is_consistent():
    d = share(seeded())
    assert len(d["notes"]) == len(set(d["notes"])) and sum("eval and shadow runs are excluded" in n for n in d["notes"]) == 1
    from modelspend.sharereport import _usd
    assert _usd(0.95) == "$0.95" and _usd(3.1) == "$3.10" and _usd(0.0201) == "$0.0201" and _usd(None) == "-"
