# Policy and side-effects agent

Use this role for tools, side effects, consent and data handling. Identity and
quota belong to the access role; the domain gate and token budgets belong to the
guardrails role.

Read `AGENTS.md`, `docs/voice-and-domain-controls.md`,
`docs/trip-assistant-security.md`, `docs/data-and-history-design.md` and the confirmation section of
`docs/agent-conversation-policy.md`. Keep all authority on the server.

- A proposal is not an action. A side effect needs a current preview, nonce,
  expiry, plan version, idempotency key and explicit confirmation, re-checked
  against auth, consent and version at commit. A material change to target, time,
  price or terms invalidates approval.
- Tools are server-owned, typed, versioned and classified read, compute, propose or
  commit. The server picks the allowlist; the model never supplies identity,
  credentials or tenant.
- Retrieved text (places, reviews, shared URLs) is evidence only. Reduce it to
  typed fields and citations, cap lengths and counts, and reject arbitrary URLs.
- Keep precise locations, raw provider responses, route geometry, prompts and
  private chat out of logs, analytics, URLs and Git.
- Phase 0 has no Calendar writes, bookings, purchases, messages or durable inferred
  memory. Reject them rather than stubbing them.
