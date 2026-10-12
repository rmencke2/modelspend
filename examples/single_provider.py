"""Within-provider routing: ModelSpend picks the cheapest suitable model of ONE provider.
Usage: ANTHROPIC_API_KEY=... python single_provider.py anthropic"""
import sys

from modelspend import ModelSpend, NoEligibleModel

provider = sys.argv[1] if len(sys.argv) > 1 else "anthropic"
ms = ModelSpend(providers=[provider])  # explicit allowlist of one provider
try:
    res = ms.route(
        "Classify the ticket as billing, bug or other. Reply with the label only.\n\nTicket: I was charged twice.",
        max_budget_usd=0.01,
        max_output_tokens=16,
        validators=[{"type": "allowed_labels", "labels": ["billing", "bug", "other"]}],
    )
except NoEligibleModel as e:
    sys.exit(f"nothing eligible for {provider}: {e.message}")
print(res.text, f"| {res.provider}/{res.model} | actual ${res.actual_cost_usd:.6f} | validation {res.validation.status}")
print(res.explanation)
