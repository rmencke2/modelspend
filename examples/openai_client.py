"""Run `modelspend serve` first. Requires `pip install openai` (already a ModelSpend dependency)."""
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="unused-unless-MODELSPEND_API_TOKEN-is-set")
r = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Translate to French: Where is the station?"}],
    extra_body={"modelspend": {"providers": ["openai", "google"], "profile": "economy", "max_budget_usd": 0.01}},
)
print(r.choices[0].message.content)
print(r.model, r.model_extra["modelspend"]["explanation"])
