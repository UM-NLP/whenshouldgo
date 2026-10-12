# WhenShouldGo coding-agent guide

This repository builds an agent-first, travel-only trip assistant. Read the relevant
documents in `docs/` before making a change. They are the product source of truth.

## Non-negotiable rules

- Build Phase 0 only: same-day Toronto/GTA local outings on mobile web.
- Treat the model, tool output, client input, and retrieved text as untrusted.
- The server owns tenant identity, access tier, quotas, tool allowlists, state
  transitions, evidence freshness, and confirmation of external actions.
- Do not implement a general chatbot, autonomous purchases/bookings, background
  microphone, background location, Calendar sync, durable inferred memory, or
  multi-day/multi-stop planning in the Phase 0 path.
- Keep credentials, precise locations, raw provider responses, route geometry,
  private chat data, and provider content outside Git and logs.
- Material claims need fresh, attributable evidence. Never turn an inference into
  a confirmed hard constraint.
- A side effect must use a pending action with a preview, nonce, expiry,
  idempotency key, and explicit confirmation.

## Engineering workflow

1. Start with `docs/trip-assistant-product-plan.md` for behavior and scope.
2. Apply `docs/voice-and-domain-controls.md` and
   `docs/trip-assistant-security.md` to every API and tool change.
3. Keep contracts compatible with `docs/trip-assistant-architecture.md` and
   `docs/access-tiers.md`.
4. Add focused tests for policy, tenant, quota, malformed-output, and action
   confirmation boundaries. Use `docs/agent-evaluation-plan.md` for fixtures.
5. Update a document only when the implementation deliberately changes a product
   decision; otherwise keep the implementation aligned to the existing decision.

## Agent roles

Each brief in `.codex/agents/` names the documents that role must read first.
Start with `architect` for new or cross-cutting work.

| Role | Owns | Reads |
|---|---|---|
| `architect` | Planning and gate checks; no edits | `decisions`, `backlog`, `project-board`, `trip-assistant-roadmap`, `trip-assistant-product-plan` |
| `runtime` | Workspace, orchestrator, model gateway, tools, patch validation | `trip-assistant-architecture`, `agent-conversation-policy`, `model-hosting-decision` |
| `access` | Guest bootstrap, identity, quota, linking | `access-tiers` |
| `guardrails` | Domain gate, token budgets, push-to-talk contract | `voice-and-domain-controls`, `agent-conversation-policy` |
| `policy` | Tools, side effects, consent, data handling | `trip-assistant-security`, `voice-and-domain-controls` |
| `product` | Mobile web client and Trip Board | `trip-assistant-product-plan`, `trip-assistant-roadmap`, `access-tiers` |
| `evaluation` | Tests, fixtures, release evidence | `agent-evaluation-plan` |
| `security-reviewer` | Read-only review of a change | `trip-assistant-security`, `access-tiers`, `voice-and-domain-controls` |

## Repository layout

- `app/` contains the FastAPI modular-monolith foundation.
- `tests/` contains deterministic unit tests; no network or provider credentials.
- `.codex/agents/` holds role-specific coding-agent briefs.
- `docs/` holds the product specifications and delivery decisions.
