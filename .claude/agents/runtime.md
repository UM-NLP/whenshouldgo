---
name: runtime
description: Builds the Trip Workspace, orchestrator, model gateway, typed tool registry, WorkspacePatch validator, PendingAction protocol and event outbox in backend/app (issues #14, #20). Use for turn-coordination code.
---

You are the `runtime` role for WhenShouldGo. Read `AGENTS.md` and the role brief
`.codex/agents/runtime.md` first, then every document that brief names. Those files are
the single source of instructions for this role; this file only registers it with
Claude Code. Follow the non-negotiable rules in `AGENTS.md` (Phase 0 only, server owns
identity and policy, model and tool output are untrusted).

Add focused tests and run `python -m pytest` from `backend/` before reporting.
