# Handoff: where the project stands

Written 2026-10-22 so another coding agent (for example GitHub Copilot) can continue from the repository alone. Read `AGENTS.md` first; its non-negotiable rules apply to every change. This file is a status snapshot, not a decision record: decisions live in `docs/decisions.md`.

## Branch and merge state

Work is on `feat/composition-root-and-smoke` (pushed to `origin`). It sits on top of several earlier feature branches and has not been merged to `main`. Open a pull request into `main` once the security review below is done.

## What works today

- **Backend** (`backend/`, FastAPI, Python 3.12): the Phase 0 turn pipeline runs end to end against live services: travel-domain guard, one FRAME model call (Gemma 4 31B at DeepInfra via OpenRouter, zero retention), Google Maps Grounding Lite search, verbatim display of Google's output with its sources. A live turn costs about US$0.00006. The suite passes (`cd backend && python -m pytest -q`).
- **Consented foreground location**: `POST/GET/DELETE /v1/me/consents`, an optional `location` on the turn request, rounded to about 100 m and sent to Google only as a 5 km bias. The model never sees it and nothing stores it.
- **No geographic restriction**: a request with no area and no consented location gets a fixed "where should I look?" clarification (a free turn).
- **Web client** (`web/`, Vite + React + TypeScript): a mobile chat screen, the verbatim Google block with sources, the location consent flow and the failure states. Tests: `cd web && npm test && npm run build`.
- **Bootstrap, usage and capabilities**: `POST /v1/session:bootstrap`, `GET /v1/me/usage`, `GET /v1/me/capabilities` (`backend/app/access.py`). The server computes `meter_state` (none, 70% quiet counter, 90% notice, gated) and `TurnResponse` carries `usage`. The web client shows the meter and disables sending when gated.

## How to run it

Dry run, no keys and no network:

```
# terminal 1, in backend/
python scripts/dev_server.py --i-understand-this-is-dev --dry-run
# terminal 2, in web/
npm install
npm run dev        # open http://127.0.0.1:5173
```

Live (needs `OPENROUTER_API_KEY` and `GOOGLE_MAPS_API_KEY` in a `.env` that is never committed):

```
python scripts/dev_server.py --i-understand-this-is-dev --env-file ..\.env --max-spend-usd 0.05
python scripts/live_smoke.py --env-file ../.env --ssl-mode os-store-nonstrict --turns 1 --show-reply --message "..." --location 48.8584,2.2945 --grant-location-consent
```

`--ssl-mode os-store-nonstrict` is only for a corporate TLS proxy. Google's text is shown in the terminal only and must not be saved, pasted into shared files or committed. The dev server and smoke script are never production entry points.

## Known limits of the meter

- The gate wording for the guest turn limit and for registered users is the implementer's own text, adapted from the trip wording in `docs/access-tiers.md`; the owner has not confirmed it.
- `resets_at` is always null and `activated_trips_used` is always 0: the 30-day registered window and trip activation are not built.
- The meter counts committed turns only, so a request in flight at the limit can show the 90% notice and then a gated reply.
- The GATED notice has no create-account button because account linking is not built.

## Next steps, in order

1. **Security review** (read-only `security-reviewer` role) of the new access routes (tenant isolation, the idempotent ledger write on bootstrap, the OpenAPI override), the composition root, `backend/scripts/dev_server.py` and `live_smoke.py`, the consent and location code, and the web client's privacy boundaries.
2. **Production identity**: a Firebase identity verifier (anonymous plus Google or email link). Until then the backend only accepts the dev token in the dev server.
3. **Shared state** (issue #35, ACCESS-02): quota, consent, workspaces, idempotency and the whole-pilot spend budget are in memory per process. Move them to Firestore before any pilot.
4. **Legal gates** (`docs/legal/open-questions.md`): the privacy notice (issue #27, must cover location sent to Google and, later, stored Home and Work), DeepInfra's written zero-retention and no-training terms (issue #25), and confirming the Grounding Lite unit price from Google billing.
5. **Place decisions to build later**: saved places and user-confirmed Home and Work (decision 24, Phase 1); history, patterns, routine reminders (Phases 1–3 in `docs/trip-assistant-roadmap.md`).
6. **Known gaps**: the guard classifies English and French only; unspaced Mandarin queries are rejected; bake-off fixtures still expect `en` for Spanish, Portuguese and Punjabi messages; deployment to Cloud Run does not exist yet.

## Rules that are easy to break

- Google Maps content: show Grounding Lite output verbatim with its sources immediately after, never edit, trim or intersperse it, never store it beyond the display cache, never give it to the model (decisions 17, 18, 23).
- The model and all tool output are untrusted. The server owns identity, quota, policy and every reply sentence.
- Precise locations never appear in logs, URLs, telemetry, the model context or storage. Tests scan for canary coordinates.
- Anything inferred is stored only after the user confirms it (decision 20). A side effect uses a pending action.
- Credentials stay in `.env` (gitignored). OpenRouter spend is capped for tests; always pass a spend cap.
- Tests are deterministic and need no network. Add a focused test for every policy, tenant, quota and malformed-output boundary.

## Where things are

| Need | Look at |
|---|---|
| Product behavior | `docs/trip-assistant-product-plan.md`, `docs/trip-assistant-roadmap.md` |
| Decisions | `docs/decisions.md` (24 so far) |
| Security and privacy | `docs/trip-assistant-security.md`, `docs/data-and-history-design.md` |
| Access and quotas | `docs/access-tiers.md` |
| Model choice and bake-off | `docs/model-hosting-decision.md`, `backend/evals/frame_bakeoff/` |
| Legal | `docs/legal/` |
| Backlog | `docs/backlog.md`, `docs/project-board.md`, GitHub issues #20–#35 |
| Role briefs | `.codex/agents/`, `.github/agents/`, `.github/copilot-instructions.md` |
