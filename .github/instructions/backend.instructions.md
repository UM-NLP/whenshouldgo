---
applyTo: "backend/**"
---

- FastAPI modular monolith; modules: `identity`, `quota`, `guard`, `agent`, `models`, `tools`, `workspace`, `actions`, `events`.
- All limits come from `app.policy.CURRENT_POLICY`; never hard-code them in handlers.
- Pydantic models inherit `Strict` (`extra="forbid"`). Owner IDs come from the verified token only.
- Tests must not call real providers; use simulated tools.
