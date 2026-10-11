# Agent Model and Hosting Decision

Updated 2026-10-10.

## Decision

Use the direct hosted **Gemini 3.5 Flash-Lite** API as the default model behind a provider-neutral `ModelGateway`. Enable official Gemini Grounding with Google Maps only for user-initiated geographic discovery. Evaluate a stronger Gemini Flash model for one bounded escalation on complex synthesis. Use OpenRouter for offline comparisons or a restricted fallback. Do not self-host during the pre-seed stage.

This choice is stronger for the agent-first product because Gemini 3.5 Flash-Lite currently supports function calling, structured output, Search grounding and Grounding with Google Maps while remaining a low-cost stable model. Current standard paid token pricing is US$0.30 per million input and US$2.50 per million output tokens. An illustrative 2,000-input/500-output call is about US$0.00185 before grounding, Maps and infrastructure charges.

References: [model capabilities](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite), [pricing](https://ai.google.dev/gemini-api/docs/pricing), [Maps grounding](https://ai.google.dev/gemini-api/docs/maps-grounding).

## Gateway

```text
generate(
  workspaceContext,
  allowedTools,
  outputSchema,
  budget,
  privacyClass,
  groundingPolicy
) -> ModelTurn
```

Pin model and prompt versions. Record tokens, latency, schema retries, tool errors and estimated cost without raw private content. A model change requires the full conversation, tool, recommendation, injection and cost evaluation suites.

## Work allocation

The cheap model handles intent interpretation, goal hypotheses, `WorkspacePatch` proposals, next-action proposals, ordinary discovery and response wording. The stronger model may run once for bounded multi-day synthesis, repeated schema failure or a measured low-confidence case.

No model is needed to deliver a scheduled reminder, detect an unchanged Calendar event, perform arithmetic validation, deduplicate a task or render an already-approved plan.

## Google Maps grounding channel

Use the built-in Maps grounding tool for prompts such as â€œfind a relaxed vegetarian restaurant near meâ€ or â€œwhat can I do with children before dinner?â€ The tool is off by default and enabled only for clear geographic discovery with user location supplied when permitted.

The application must display the Google Maps Grounded Result with its associated Maps links according to the additional terms. It cannot modify or intersperse other content within the grounded result, scrape/export Maps data or train on it. The terms currently allow limited storage for display evaluation and up to six months in the initiating user's chat history, and state that prompts/context/generated content are stored by Google for 30 days for this feature. The consent and retention UI must disclose this.

Grounded discovery is a distinct data path from ordinary Places and Routes APIs. Provider-policy review remains required before sending those API payloads to an LLM or TTS.

## OpenRouter

OpenRouter provides model choice and fallback controls but adds a processor, fee, privacy configuration and failure boundary. It cannot replace the direct official Maps-grounding relationship. Use it for redacted offline evaluation or non-Maps fallback only after processor/privacy review.

## Self-hosting

Defer self-hosting until at least four weeks of production measurements show sustained inference spend and utilization that can beat hosted total cost, or a residency/privacy requirement cannot be met. Include GPU, cold starts, model/tool quality, security patching, observability and on-call work in the comparison.

## Initial budgets

- Six model turns per interactive run.
- Twelve total tool calls.
- One stronger-model escalation.
- A 20-second interactive execution budget, excluding user conversation.
- Search many candidates, fetch details for a shortlist and route finalists only.
- Per-workspace token, grounding, Places, Routes and total-dollar ceilings.
- Global daily caps and kill switches.

At budget exhaustion, return a useful partial result with visible unresolved items and a structured fallback.


