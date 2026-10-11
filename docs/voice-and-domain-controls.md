# Push-to-Talk, Travel Scope and Token Controls

Updated 2026-10-11. The first Android voice experience is explicit push-to-talk. The assistant serves trip and outing planning, execution and recovery; it is not a general chatbot.

## Activation

The primary Android control is a large microphone button in the app and Trip Board. Press and hold starts capture; releasing submits; sliding or tapping Cancel discards the recording. A home-screen widget and Quick Settings tile may open the same capture screen after the Android MVP proves useful. Hardware-key interception, an always-listening wake word, AccessibilityService and default-assistant replacement are outside the MVP.

1. The first press explains microphone use and requests `RECORD_AUDIO`.
2. A chime, waveform and elapsed time make recording visible.
3. Capture stops on release or after 60 seconds.
4. Audio remains in memory, is transcribed, and is discarded after the transcript is accepted or the request fails.
5. The editable transcript is shown before any consequential action.
6. The transcript enters the same authenticated, quota-controlled API as typed text.
7. The response appears on the Trip Board; a short spoken summary is optional and stops immediately when the user interrupts.

The microphone is off while the button is not held. Voice activation works for anonymous guests under the same access allowance. No background microphone service, boot listener or continuous audio upload is required.

## Allowed domain

The assistant may handle only:

- discovering places, activities, stops, stays and transport for a real or hypothetical trip;
- comparing travel options using time, distance, budget, accessibility, dietary, companion and preference constraints;
- building, changing, validating and explaining an itinerary or outing;
- departure, arrival, visit, return, opening, reservation and connection timing;
- trip-relevant weather, closures, availability, documents and disruptions;
- active-trip navigation handoff, reminders, monitoring and recovery;
- user-approved trip memory, Calendar context and group coordination after their release gates;
- help for this app, its privacy, permissions, account, usage and trip controls.

Destination trivia, general web research, schoolwork, coding, writing, entertainment chat, general medical/legal/financial advice and unrelated personal assistance are out of scope. A mixed request is reduced to its travel-planning portion. Immediate safety or emergency language receives a fixed safety response and no planning tools.

## Enforcement pipeline

Scope is enforced by server code, not only a system prompt.

```text
authenticated request
  -> size/rate/idempotency checks
  -> deterministic app-command and active-trip checks
  -> bounded intent classifier when meaning is ambiguous
  -> TRAVEL | APP_HELP | MIXED | OUT_OF_SCOPE | SAFETY
  -> server-selected tool allowlist
  -> bounded planning model
  -> schema, evidence, scope and token validation
  -> Trip Workspace patch plus concise response
```

The classifier returns only an enum, confidence and the travel span for a mixed request. It has no tools. `OUT_OF_SCOPE` uses a fixed application response rather than a generative answer:

> I can help plan trips and outings. Tell me where you want to go, what you want to do, or when you need to arrive or return.

Low-confidence input gets one travel-focused clarification. It cannot fall through to general conversation. Out-of-scope turns do not consume the public 50-turn allowance, but they increment a separate abuse counter. Repeated attempts receive the same cached response and then a short cooldown without another model call.

Tool capabilities are derived from the server classification and workspace state. The model cannot request arbitrary browsing, code execution or a wider tool set. Search queries must contain a validated trip goal, geography or travel context. Retrieved text and prompt injection cannot change the domain, token budget or permissions.

## Initial token safety policy

These limits are versioned server policy and intentionally below the provider context window.

| Boundary | Initial hard limit | Limit behavior |
|---|---:|---|
| Typed message or accepted transcript | 2,000 input tokens | Reject before the planning model and ask for a shorter request |
| Intent classifier | 2,048 input, 64 output tokens; one call | Return fixed clarification/refusal on failure |
| Compiled planner context per call | 12,000 input tokens | Remove stale/low-value evidence; never truncate confirmed hard constraints or permissions |
| Planner response per call | 1,200 output tokens | Validate finish reason and schema; never execute a truncated action |
| Interactive agent run | 40,000 input and 4,000 output tokens total | Return the best validated partial plan; no automatic budget extension |
| Model calls and tools per run | 6 model calls, 12 tool calls, one escalation | Stop with unresolved choices visible |
| User-visible response | 450 tokens | Put structured detail on the Trip Board |
| Spoken response | 160 tokens | Speak the next action and essential tradeoff; screen holds the detail |
| Guest internal daily ceiling | 60,000 input and 6,000 output tokens | Cooldown until the rolling 24-hour window frees capacity |
| Registered-free internal daily ceiling | 120,000 input and 12,000 output tokens | Same, with exact reset time shown |

Input, output, cached, tool-use and any thinking tokens all enter the cost ledger. The gateway counts the fully compiled request before generation, sets the provider's maximum output tokens, reserves the worst-case units transactionally, and commits actual usage metadata afterward. The public guest/free allowance remains separate from these safety ceilings.

If the provider reports `MAX_TOKENS`, malformed JSON or a truncated function call, the validator rejects the candidate. One bounded repair is allowed only when the remaining run budget covers it; otherwise the user receives the last validated partial plan and a focused next step. Token limits never authorize dropping a hard constraint, hiding uncertainty or executing a partial external action.

## Tests and telemetry

The release suite includes direct and disguised requests for coding, essays, therapy, general search and prompt injection; mixed travel/non-travel prompts; long and multilingual inputs; transcript errors; token-boundary races; tool-output inflation; `MAX_TOKENS`; malformed patches; repeated out-of-scope abuse; and provider usage mismatches.

Targets are zero general-purpose answers in the reviewed boundary suite, zero tool calls for rejected out-of-scope requests, zero actions from truncated output, at least 99% correct routing of supported travel/app-help inputs, and fewer than 1% false refusals on supported goals. Report classifier and planner tokens, latency, repairs, refusals, false accepts, false refusals and cost without recording raw private utterances.

Official references: [Android audio capture](https://developer.android.com/media/platform/av-capture), [Gemini token counting](https://ai.google.dev/gemini-api/docs/tokens), [Gemini generation configuration and finish reasons](https://ai.google.dev/api/generate-content) and [Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).
