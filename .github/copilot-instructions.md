# WhenShouldGo — Copilot instructions

Follow `AGENTS.md` at the repository root. It holds the scope, non-negotiable rules,
stack, repository layout and agent roles; the role briefs live in `.codex/agents/`.
The documents in `docs/` are the product source of truth.

Backend code is in `backend/app/`; run `python -m pytest` from `backend/`. All limits
come from the versioned policy in `backend/app/entitlements.py`, never from constants
in handlers.
