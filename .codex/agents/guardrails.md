# Guardrails agent

Use this role for the travel-only domain gate, tool allowlists, token budgets and
the push-to-talk contract (#23).

Read `AGENTS.md`, `docs/voice-and-domain-controls.md`, the domain boundary in
`docs/agent-conversation-policy.md`, and decisions 8-11 in `docs/decisions.md`.

- Pipeline: size/rate/idempotency, then deterministic app-command and active-trip
  checks, then a bounded classifier (no tools, one call) returning class,
  confidence and travel span. Classes are `TRAVEL | APP_HELP | MIXED |
  OUT_OF_SCOPE | SAFETY`.
- `MIXED` keeps only the travel span and goes on to planning. `OUT_OF_SCOPE`
  returns the fixed redirect with zero tool calls. `SAFETY` returns a fixed
  response with no planning tools. Low confidence gets one travel-focused
  clarification, never general conversation.
- Out-of-scope turns do not consume an allowance but increment an abuse counter;
  repeats get a cached reply, then a cooldown with no model call.
- Enforce every limit in the token table in `docs/voice-and-domain-controls.md`
  from versioned policy. Count the compiled request first, reserve worst case, and
  commit actual provider usage. Never drop confirmed hard constraints or
  permissions when compacting.
- Reject `MAX_TOKENS`, malformed JSON and truncated function calls. Allow one
  repair only when the remaining run budget covers it.
- Push-to-talk is a client contract: hold to record, release submits, cancel
  discards, 60 s cap, audio discarded after transcription, transcript uses the
  same API as typed text. No wake word or background microphone.

Targets: zero general answers, zero tool calls on rejected requests, at least 99%
correct routing, under 1% false refusals. Cover disguised coding, essay and
therapy requests, injection in tool output, mixed and multilingual prompts, the
2,000-token boundary, token races and usage-metadata mismatch.
