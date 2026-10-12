---
applyTo: "backend/**"
---

- Follow `AGENTS.md` and the role brief in `.codex/agents/` for the area you change.
- All limits come from the versioned policy in `backend/app/entitlements.py`; never
  hard-code them in handlers.
- Pydantic models inherit `Strict` from `backend/app/domain.py` (`extra="forbid"`).
  Owner IDs come from the verified token only.
- Tests are offline and must not call real providers; use simulated tools.
