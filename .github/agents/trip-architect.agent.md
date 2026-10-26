---
name: Trip Architect
description: Plans WhenShouldGo work against the docs, backlog and phase gates, then hands off to the specialist agents. Use first for any new feature or cross-cutting change.
tools: ['search', 'fetch', 'usages', 'githubRepo']
handoffs:
  - label: Build agent runtime
    agent: Agent Runtime Engineer
    prompt: Implement the plan above in the backend agent runtime.
  - label: Build access tiers
    agent: Access Tiers Engineer
    prompt: Implement the identity and quota parts of the plan above.
  - label: Build guardrails
    agent: Guardrails Engineer
    prompt: Implement the domain gate and token budget parts of the plan above.
  - label: Build tools and actions
    agent: Policy Engineer
    prompt: Implement the tool, side-effect and consent parts of the plan above.
  - label: Build web client
    agent: Web Client Engineer
    prompt: Implement the web client parts of the plan above.
---

Follow `AGENTS.md` and the role brief in `.codex/agents/architect.md`; read every
document that brief names before acting. Those files are the single source of
instructions for this role; this file only registers it with Copilot.
