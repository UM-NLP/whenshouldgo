# Agent Model and Hosting Decision

Updated 2026-10-14. This replaces the earlier Gemini-based decision. Gemini is not used in the first version.

## Decision

Use a low-cost open model reached through **OpenRouter** as the default behind a provider-neutral `ModelGateway`. Use **Google Maps Grounding Lite** (through a server-side MCP client) for user-initiated geographic discovery. Evaluate one stronger model for at most one bounded escalation on complex synthesis. Do not self-host during the pre-seed stage.

The specific model and upstream host are chosen by a bake-off on synthetic fixtures (tool-calling reliability, strict JSON output, travel-domain classification, injection resistance, latency and cost), not by price alone. A candidate must have a zero-data-retention endpoint and written terms on file in `docs/legal/` before use. Cheap open models vary widely in tool calling and structured output, so the evaluation suite is the selection gate.

### Selection (owner decision, 2026-10-17)

Primary FRAME model: `google/gemma-4-31b-it` at DeepInfra (provider slug `deepinfra`). Second: `qwen/qwen3-next-80b-a3b-instruct` at DeepInfra. Both requests carried the zero-retention, data-collection-deny and no-fallback parameters, and OpenRouter served them, so it treats both DeepInfra endpoints as zero-retention eligible. Written DeepInfra terms (retention, training, caching, data location) and a data processing agreement are still required before pilot traffic; DeepInfra's data location is unverified and probably outside Canada, which the consent notice must state.

### Bake-off result (live, 2026-10-17)

150 synthetic hand-labelled fixtures (50 discovery, 30 non-discovery, 30 multilingual, 20 injection, 20 edge) x 3 repeats = 450 calls per model through the real gateway, with every reply replayed through the production query-planning and patch steps (`backend/evals/frame_bakeoff/`). The fixtures were written and labelled by an agent and the harness was tuned against them, so this is a first-pass comparison and not an independent test; the pass bars are proposals and the 450-call sample resolves failure rates only to about 1%.

| | Gemma 4 31B | Qwen3-Next 80B | gpt-oss-120b |
|---|---|---|---|
| Production turn success | 99.3% | 99.3% | 35.6% |
| Parseable first attempt | 100% | 100% | 36.7% |
| Intent accuracy | 99.3% | 99.3% | 36.1% |
| Faithfulness | 99.0% | 97.0% | 35.3% |
| Latency median / 95th percentile | 1.00 / 2.22 s | 1.33 / 3.30 s | n/a (provider errors) |
| Cost per 1,000 FRAME turns | US$0.064 | US$0.063 | n/a |
| Cost of the run | US$0.0289 | US$0.0285 | US$0.0148 |

`gpt-oss-120b` is excluded for now: 62.6% of its calls ended in provider errors (rate limiting, then the circuit breaker), and as a reasoning model it spent about 336 output tokens per call against about 36 for Gemma 4. The cause of the rate limiting is unknown. Qwen3-Next's one failed bar (injection rejection 95%) came from a search attached to an OTHER intent that the server ignores; the scorer is being changed to report such neutralised slips separately.

An earlier run with the model filling the whole workspace patch was far worse (about 6% production turn success, because the model set patch status to CONFIRMED on 61% of calls). That drove the FRAME redesign recorded in decision 23: the model proposes an intent, a search and soft facts, and the server owns state and routing.

Verified live: the response `provider` field is the display name (`DeepInfra`), not the slug; cached tokens were zero; one FRAME call is about 250 input and 40 to 100 output tokens. Known limits: unspaced Mandarin fails the query overlap rule (a product gap), and a word the user wrote that is off-topic (for example "password") passes the overlap rule.

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

In Phase 0 the model never sees the tool results (decision 23). The model only interprets the user's message and proposes the search arguments; the server runs the Grounding Lite tool, validates the result and shows the grounded output unmodified with its Google Maps sources immediately after it, reachable in one interaction, and with the required attribution. The application cannot extract, modify, reorder or intersperse the grounded output, and it cannot use it to train, test, validate or improve any model. The grounded output may be cached for redisplay for 30 consecutive days at most. Because Maps content never reaches the model provider, the question of whether zero retention satisfies Google's "not cached by the LLM" rule (legal question 2) does not gate Phase 0; zero retention is still required on every request. A `RuntimeConfig.maps_content_to_model` flag, default off and not implemented, marks the point where a later phase could let the model read grounded results, and it stays off until that question is answered in writing.

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
