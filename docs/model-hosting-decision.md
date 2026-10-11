# Model and Hosting Decision

Updated 2026-10-10. This decision concerns the conversational agent. Route feasibility and schedule optimization remain deterministic server functions.

## Decision

Use a direct hosted **Gemini 3.5 Flash-Lite** API as the initial production model behind a provider-neutral `ModelGateway`. Use OpenRouter for offline comparisons and, only if needed, a carefully restricted fallback for redacted, non-sensitive requests. Do not self-host a model during the pre-seed stage.

Gemini 3.5 Flash-Lite is a stable, low-cost model with function calling and structured outputs. Current standard paid-tier pricing is US$0.30 per million input tokens and US$2.50 per million output tokens, and paid-tier data is not used to improve Google products. A 2,000-input-token and 500-output-token call is about US$0.00185; three such calls are about US$0.00555. These are illustrative token costs, excluding Maps and other infrastructure, and must be replaced with measured production traces. See [Gemini 3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite) and [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

## Why direct hosting first

Direct access has fewer processors, no routing markup, predictable tool behavior, and simpler debugging. It also matches the selected Google Cloud stack. The gateway keeps the application portable:

```text
generate(messages, allowedTools, outputSchema, budget, privacyClass)
  -> ModelTurn
```

Pin an explicit model version where the provider permits it. Run tool-selection, extraction, prompt-injection, latency, and cost evaluations before any model change.

## OpenRouter role

OpenRouter provides a standardized tool-calling interface, model/provider choice, and routing controls. Its current Standard plan adds a 5.5% platform fee, adds another processor and failure point, and privacy behavior depends on the chosen downstream provider and routing settings. It is useful for evaluation and operational fallback, but it is not the default path for calendar and exact-location context. See [OpenRouter pricing](https://openrouter.ai/pricing/), [tool calling](https://openrouter.ai/docs/guides/features/tool-calling), and [guardrails](https://openrouter.ai/docs/guides/features/guardrails/overview).

## Why self-hosting is deferred

A small open model still requires GPU capacity, model loading and cold-start control, scaling, security updates, observability, licenses, and proof that its tool calls meet the same evaluation threshold. GPU Cloud Run can scale to zero, but an L4 service has minimum CPU and memory requirements and the workload must pay model-start and operational costs. At pre-seed traffic, per-token hosted inference is the cheaper and safer operating choice. See [Cloud Run GPU support](https://docs.cloud.google.com/run/docs/configuring/services/gpu).

Reconsider self-hosting only after at least four weeks of measured production volume show sustained inference spend and GPU utilization sufficient to beat hosted total cost, or a privacy/residency requirement cannot be met by hosted APIs. The comparison must include engineering and on-call cost, not GPU price alone.

## Model responsibilities and budgets

The model may extract candidate intent from user-authored text, choose from an allowed non-provider tool subset, ask the next adaptive question, and explain user-authored constraints. It may not invent travel times, decide feasibility, order stops without the solver, bypass a confirmation, or directly write calendars and reminders.

Until the applicable route-provider agreement is reviewed in writing, the model receives no Google Maps durations, ETAs, routes, place descriptions, calendar-derived text, or computed recommendation based on that content. Google-backed computation ends the model loop and a deterministic UI template renders the result with attribution. Text-to-speech must not speak Google-derived driving times. This boundary may be relaxed only for a provider and use case whose contract expressly permits the LLM and voice processing. See [Google Maps Platform terms](https://cloud.google.com/maps-platform/terms).

Use compact typed context from the user's current utterance and confirmed, consented defaults. Do not send calendar feeds, calendar text, Google Maps content, raw route payloads, or the user's full history. Start with a maximum of six turns and a per-request dollar ceiling. Escalate to a stronger model only after a schema retry or for a validated complex itinerary class, with a separate cap and logged reason.

Required pre-production evaluations are 100% preservation of hard constraints in critical fixtures, at least 98% correct tool selection and schema-valid arguments, zero unauthorized commits, resistance to instructions embedded in calendar/place text, p95 agent overhead within the interactive latency budget, and measured model cost per activated plan.


