import asyncio

from modelspend import ModelSpend, StreamInterrupted

ms = ModelSpend(providers=["anthropic", "openai"])


async def stream():
    try:
        async for ev in ms.astream("Rewrite politely: send me the file now", max_budget_usd=0.01):
            if ev.type == "route":
                print("selected:", ev.plan.candidates[0].provider, ev.plan.candidates[0].model)
            elif ev.type == "delta":
                print(ev.text, end="", flush=True)
            elif ev.type == "done":
                print("\ncost", ev.result.actual_cost_usd, "validation", ev.result.validation.status)
    except StreamInterrupted as e:   # model is never silently swapped once output began
        print("\ninterrupted after:", e.details["partial_text"][:60], "| cost so far", e.details["cost_usd"])


async def tools():
    res = await ms.aroute(
        "What's the weather in Oslo?",
        tools=[{"name": "get_weather", "description": "Weather by city",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
    )
    for call in res.tool_calls:      # returned to YOU, with provider ids; ModelSpend never executes tools
        print(call.id, call.name, call.arguments)

asyncio.run(stream())
asyncio.run(tools())
