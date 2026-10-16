# Agent Model and Hosting Decision

Updated 2026-10-14. This replaces the earlier Gemini-based decision. Gemini is not used in the first version.

## Decision

Use a low-cost open model reached through **OpenRouter** as the default behind a provider-neutral `ModelGateway`. Use **Google Maps Grounding Lite** (through a server-side MCP client) for user-initiated geographic discovery. Evaluate one stronger model for at most one bounded escalation on complex synthesis. Do not self-host during the pre-seed stage.

The specific model and upstream host are chosen by a bake-off on synthetic fixtures (tool-calling reliability, strict JSON output, travel-domain classification, injection resistance, latency and cost), not by price alone. A candidate must have a zero-data-retention endpoint and written terms on file in `docs/legal/` before use. Cheap open models vary widely in tool calling and structured output, so the evaluation suite is the selection gate.

## Gateway

```text
generate(
  workspaceContext,
  allowedTools,
  outputSchema,
  budget,
  privacyClass,
  runtimeConfigVersion
) -> ModelTurn
```

Every request carries the OpenRouter routing parameters that pin the provider and enforce privacy (zero data retention, data collection denied, no fallback to unreviewed hosts, an explicit allowlist of reviewed providers). Parameter names must be checked against OpenRouter's current documentation when the adapter is built. The gateway fails closed: it refuses to send a request that lacks these parameters, and a startup test asserts this. Account-level settings keep OpenRouter's prompt logging and training opt-ins off. OpenRouter plugins and server-side tools are not used on these prompts, because zero data retention does not cover them.

`RuntimeConfig` is a versioned bundle recorded on every turn: model ID, upstream provider and provider allowlist, the privacy settings (zero retention required, data collection denied, fallbacks disabled; a weakened config cannot be constructed), price table version, prompt version hashes, tool schema versions, classifier version, policy and entitlement versions, token ceilings (including the per-call planner ceilings), TTLs and feature flags per phase. A change is auditable and can be rolled back by changing the config version. A model, provider or prompt change requires the full conversation, tool, recommendation, injection and cost evaluation suites on synthetic fixtures. Track provider deprecation notices and record the exact model ID in every trace.

Record tokens, latency, schema retries, tool errors and estimated cost without raw private content or Maps content. Costs use a versioned price table that includes the model, Grounding Lite, Places and Routes unit prices.

## Work allocation

The cheap model handles intent interpretation, goal hypotheses, `WorkspacePatch` proposals, next-action proposals, grounded discovery through Grounding Lite and response wording. The stronger model may run once for bounded multi-day synthesis, repeated schema failure or a measured low-confidence case.

No model is needed to deliver a scheduled reminder, detect an unchanged Calendar event, perform arithmetic validation, deduplicate a task, compute a pattern, render an already-approved plan or render a Places or Routes result.

## Google Maps Grounding Lite channel

Use Grounding Lite for prompts such as “find a relaxed vegetarian restaurant near me” or “what can I do with children before dinner?” The server holds the API key, exposes only an allowlist of its tools, and enables them only for clear geographic discovery, with user location supplied when permitted. Research reported the tools `search_places`, `lookup_weather`, `compute_routes`, `resolve_names` and `resolve_maps_urls`, a global endpoint at `mapstools.googleapis.com/mcp`, and per-project quotas of 300 requests per minute for search, weather and routes. Those details are unverified and must be checked before implementation.

The model sees tool results, which contain Maps content, within a single request. The application displays the grounded output unmodified with its Google Maps sources immediately after it, reachable in one interaction, and with the required attribution. It cannot extract, modify, reorder or intersperse the grounded output, and it cannot use it to train, test, validate or improve any model. The grounded output may be cached for redisplay for 30 consecutive days at most. The model provider must not store or cache Maps content, which is why zero data retention is required.

The owner-supplied analysis of 2026-10-15 reports Grounding Lite as Generally Available with a 99.9% uptime objective and no Canadian exclusion; confirming this for the owner's billing account and the remaining questions are tracked in [legal/open-questions.md](legal/open-questions.md). Keep a degradation path to a structured, server-rendered fallback regardless.

Caching by the model provider: zero data retention does not by itself satisfy Google's rule that Maps content is not cached by the model provider, because OpenRouter allows some implicit in-memory prompt caching under its zero-retention policy. Before launch, verify each allowlisted upstream endpoint, including any fallback, and disable reusable prompt caching, or obtain Google's written acceptance. Record the verification per provider in `docs/legal/`.

## Ordinary Places and Routes APIs

The server calls these APIs directly for a selected place ID and for route checks, and renders the results. Until a written Google determination says otherwise, their payloads are not placed in model context or text-to-speech. The owner has asked to allow this with user consent. User consent addresses privacy, not Google's licence terms, so the question has been added to [legal/open-questions.md](legal/open-questions.md) as a question for Google, and the evidence contract carries an `llm_visible` flag so the rule can change without a redesign.

## OpenRouter as processor

OpenRouter is a new processor and an additional failure boundary. Before pilot traffic: obtain written zero-retention terms for each allowlisted upstream host, request a data processing agreement and sub-processor information, name OpenRouter and the upstream host in the consent notice, and review each host's stated retention monthly.

## Self-hosting

Defer self-hosting until at least four weeks of production measurements show sustained inference spend and utilization that can beat hosted total cost, or a residency/privacy requirement cannot be met. Include GPU, cold starts, model/tool quality, security patching, observability and on-call work in the comparison.

## Initial budgets

- Six model turns per interactive run.
- Twelve total tool calls.
- One stronger-model escalation.
- A 20-second interactive execution budget, excluding user conversation.
- Search many candidates, fetch details for a shortlist and route finalists only.
- Per-workspace token, grounding, Places, Routes and total-dollar ceilings. Dollar amounts are set before pilot traffic and recorded in `RuntimeConfig`.
- Global daily caps and kill switches. The gateway latches a per-process kill switch on a provider 402 (out of credits). Cloud Run scale-to-zero or a second instance loses it, so a shared flag in Firestore is required before pilot traffic. The per-run dollar ceiling is not yet enforced; it is added once the price table has real prices.

At budget exhaustion, return a useful partial result with visible unresolved items and a structured fallback.
