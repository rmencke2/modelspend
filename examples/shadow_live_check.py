"""Try shadow evaluation on real calls (spends a few cents; the cap below keeps it bounded).

    modelspend shadow set --rate 1 --budget 0.50     # sample every request, never spend more than $0.50 this month
    modelspend shadow on
    python examples/shadow_live_check.py

It sends 30 support-ticket classifications to your premium model (pinned, so ModelSpend only measures), lets ModelSpend try the cheaper models
on each in the background, then prints what was measured and the savings projection. Half the calls carry a validator (deterministic check),
half do not (exact match, else a judge model), so both comparison paths are exercised. Edit PREMIUM and PROVIDER for your own setup.
Remember to turn it back down afterwards: `modelspend shadow set --rate 0.05` (or `modelspend shadow off`).
"""
from modelspend import ModelSpend
from modelspend.savings import build_savings, to_table
from modelspend.shadow import aggregate, spent_this_month
from modelspend.types import ValidatorSpec

PROVIDER, PREMIUM = "openai", "openai/gpt-4.1"
TICKETS = [
    "I was charged twice for my subscription this month.", "The export button does nothing when I click it.",
    "Can you send me the invoice for March?", "The app crashes when I open the settings page.",
    "Do you have a student discount?", "My card was declined but I was still billed.",
    "Search returns no results for words I know exist.", "How do I change the email on my account?",
    "I need a refund for the annual plan.", "Dark mode resets every time I restart the app.",
    "What are your support hours?", "The PDF report has the wrong totals.", "I was billed after I cancelled.",
    "Login fails with error 500 on mobile.", "Where can I find the API docs?"]

ms = ModelSpend(providers=[PROVIDER])
if ms.engine.shadow is None:
    raise SystemExit("Shadow evaluation is off. Run: modelspend shadow set --rate 1 --budget 0.50 && modelspend shadow on")

for i, ticket in enumerate((TICKETS * 2)[:30]):
    kw = dict(pin=PREMIUM, project="shadow-demo", agent="live-check", task_type="classification", max_output_tokens=64)
    if i % 2 == 0:
        kw["validators"] = [ValidatorSpec(type="allowed_labels", labels=["billing", "bug", "other"])]
    r = ms.route(f"Classify this support ticket as billing, bug or other. Reply with the label only.\n\n{ticket}", **kw)
    print(f"{i + 1:2d}. {r.text.strip()[:12]:<12} {ticket[:56]}")

print("\nwaiting for the background comparisons ...")
ms.wait_for_shadow(180)
print(f"shadow spend this month: ${spent_this_month(ms.store):.4f}\n")
for a in aggregate(ms.store):
    print(f"{a['category']:<16}{a['provider']}/{a['model']:<22} scored {a['n']:>3}  passed {a['passes']:>3}  unscored {a['unscored']:>3}")
print()
print(to_table(build_savings(ms.store, ms.config, since=None, project="shadow-demo")))
