# Aliases and conversation stickiness (0.4)

*Both are tested with mocked providers. They have not been exercised against live providers yet.*

ModelSpend can choose the model for you, but many people prefer to choose it themselves, and a chat that changes model on every turn is
a bad experience. These two features address that.

## Aliases: pick the model by the kind of work

```bash
modelspend alias set coding anthropic/claude-sonnet-5-5
modelspend alias set classify openai/gpt-4.1-nano
modelspend alias list
modelspend route "Classify: ..." --model classify
```

Any OpenAI client against the local server uses the alias as the model name:

```python
client.chat.completions.create(model="coding", messages=[...])      # base_url = http://127.0.0.1:8787/v1
```

and the SDK accepts it as `pin`: `ms.route("...", pin="coding")`.

- An alias is a **pinned model**: ModelSpend runs exactly that model, with no routing, failover or escalation. Tokens, cost and tags
  are still recorded, so [per-project and per-agent reports and budgets](cost-attribution.md) work when you choose the models yourself.
- The target must be in your catalog (it may be unpriced: tokens stay exact, cost shows as unknown). `alias list` tells you if a target
  is missing from the catalog, has no price, or has no API key.
- Names are 1-32 characters (`a-z`, `0-9`, `-`, `_`), case-insensitive, never contain `/`, and `auto` is reserved. An alias wins over a
  bare catalog model id of the same name.
- Aliases live in `~/.modelspend/settings.json`. The server also has `GET /v1/aliases`, `PUT /v1/aliases/{name}` with
  `{"model": "provider/model-id"}` and `DELETE /v1/aliases/{name}` (the same protections as the other write endpoints); changes apply immediately.

## Conversation stickiness: keep the model chosen for earlier turns

When a request carries earlier turns (an assistant or tool message), ModelSpend keeps using the model the conversation has been on
instead of re-routing every message. Reasons: switching models mid-conversation can lose **provider-side prompt caching** (ModelSpend
does not measure this) and changes the model's style partway through.

How it behaves:

- **First message:** routed normally. The chosen model, and the task category, are remembered under a salted hash key (the system prompt
  and first user message, plus project and agent). Nothing readable is stored: a 64-character hash, a model name, a category and a time.
- **Later turns:** the remembered model moves to the front of the candidate list, as long as it still passes the hard constraints
  (provider allowed, not denied, fits the context, budget) and is still **suitable for this turn**. If a follow-up needs more than the
  model offers (for example, a chat about tickets suddenly asks for code), stickiness is **released** for that turn, with the reason in
  the explanation, and the new model becomes the conversation's model.
- **Short follow-ups** such as "and the refund?" cannot be classified on their own, so they inherit the task category of the earlier turns
  instead of defaulting to a stronger, more expensive model.
- **Escalation and failover** work as before; whichever model ends up answering becomes the conversation's model.
- The result says what happened: `sticky: "held"` or `"released: <why>"` (also in `plan.sticky`, and in the explanation, which names the cheaper model
  you would have got for that turn alone, so the price of stickiness is visible).
- **Single-turn requests** are never held: they are routed on their own merits.
- Entries expire after 24 hours and the table is capped at 20,000 rows.

Controls: `sticky=False` on a request (or `"modelspend": {"sticky": false}` on the server) for one call, `MODELSPEND_STICKY=false` or
`"sticky_conversations": false` in `settings.json` for everything. Pinned and alias requests are never held.

**Passing a `run_id`** (SDK `run_id=`, header `X-ModelSpend-Run`) makes the run the conversation's identity instead of its first message. Use it
when a client rewrites or compacts its history, which changes the first message and would otherwise start a new conversation.

### What it does not do (yet)

- It does **not** weigh the cost of switching against the saving: a switch is allowed only when the remembered model is no longer
  suitable or available, never because a cheaper one appeared. A cache-aware comparison is a possible next step.
- Anthropic prompt caching is not used by ModelSpend at all today (it does not send cache markers), so for Anthropic the benefit is
  consistency, not cache savings. OpenAI applies prefix caching automatically, which is where switching is most likely to cost you.
- Conversations are recognised by their first message (or `run_id`). Two unrelated chats that begin with the identical first message
  share a key.
