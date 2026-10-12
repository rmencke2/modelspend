"""Cross-provider routing across an EXPLICIT allowlist. Requests never go to unlisted providers."""
from modelspend import ModelSpend

ms = ModelSpend(providers=["openai", "anthropic", "google"], profile="balanced")
text = "Summarize in one sentence: The council approved a $12M bridge repair starting in April."
plan = ms.preview(text, max_budget_usd=0.02)      # no call, no spend
print(plan.explanation)
for c in plan.candidates:
    print(f"  {c.rank}. {c.provider}/{c.model}  est ${c.expected_cost_usd:.6f}  worst ${c.worst_case_cost_usd:.6f}  ({c.suitability_basis})")
res = ms.route(text, max_budget_usd=0.02)
print(res.text, "\n", res.provider, res.model, f"est ${res.estimated_cost_usd:.6f} actual ${res.actual_cost_usd:.6f}",
      "escalations", res.escalations, "catalog", res.catalog_version, "policy", res.policy_version)
