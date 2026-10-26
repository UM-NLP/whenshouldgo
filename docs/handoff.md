# Handoff: where the project stands

Updated 2026-10-26 so another coding agent (for example GitHub Copilot) can continue from the repository alone. Read `AGENTS.md` first; its non-negotiable rules apply to every change. This file is a status snapshot, not a decision record: decisions live in `docs/decisions.md`.

## Branch and merge state

Work is on `feat/phase0-completion-and-history-dark`, built on top of the earlier feature branches, not merged to `main`. The last integration pass (this one) is uncommitted in the working tree at the time of writing. Open a pull request into `main` after the security review below.

## What is built (Phase 0, plus Phase 1 dark)

- **Turn pipeline** (`backend/app/`, FastAPI, Python 3.12): travel-domain guard, one FRAME model call (Gemma 4 31B at DeepInfra via OpenRouter, zero retention), Google Maps Grounding Lite search, Google's output shown verbatim with its sources. Every source now carries a typed `place_id` (bound into the display hash).
- **Consented foreground location**, no geographic restriction, bootstrap/usage/capabilities (server-computed meter, trip allowance, registered rolling 30-day window with a real `resets_at`).
- **Trip Board server** (`workspaces.py`, `routes/workspace.py`): `GET/PATCH /v1/workspace` with base-version concurrency, idempotent edits, immutable plan versions (`latest_plan`, `warnings`, `plan_versions` in the view), `POST /v1/outcomes`. A workspace write is a compare-and-set on the version (`WorkspaceStore.put_if_version`) so two instances cannot lose each other's update (a 409 with `current_version`).
- **PendingAction protocol** (`actions.py`, `routes/actions.py`): propose, confirm (nonce, 15-minute expiry, idempotency), cancel, trip activation. Effects: `SET_REMINDER` (typed reminder returned for the device), `LAUNCH_HANDOFF` (a Maps URL built at confirm time from the place ID only), `SAVE_PLACE` (refused at propose while history is not active).
- **Accounts** (`account.py`, `migration.py`, `routes/account.py`): link intents, collision-safe explicit merge, export, delete with a tombstone and per-store deleters. Typed `ErrorReason`s for the link and merge failures. A deleted account is refused at the route and by the orchestrator, workspace service and action service for requests already in flight.
- **Places Details and route check** (`places/`, `routes/places.py`): server-rendered, display-only, never given to the model. Built but OFF: the price table is unreviewed (see "What is off").
- **Durable state behind protocols** (`stores/`): quota, consent ledger, workspaces, replay, display cache, spend counter and kill switch, plan versions, outcomes, edit receipts, pending actions, account records. Memory and Firestore adapters; the Firestore ones are tested only on an in-process fake.
- **Production identity**: Firebase ID-token verifier and App Check (`auth_firebase.py`, `appcheck.py`), wired by `composition.py` / `server.py`; fails closed without `FIREBASE_PROJECT_ID`, Firestore state, a spend budget and the provider keys.
- **Consent**: `LOCATION_FOREGROUND` plus `HISTORY_STORE` and `SAVED_PLACE`, the latter two grantable only while `history_active(config)` (otherwise a typed 404 DISABLED) and bound to the config's approved notice version.
- **Web** (`web/`, Vite + React + TypeScript): chat, verbatim Google block, location consent, local-first storage and offline outbox, an app-shell-only service worker, the Trip Board (editable chips, up to three places, place and route cards, pending actions, Maps handoff, in-app reminders, check-in). The client types come from the generated OpenAPI; nothing on the board is untyped any more.

Composition (`backend/app/composition.py`, `build_runtime`) attaches every service to `app.state`; each route answers a typed 503 until its service exists. The dev server and `live_smoke.py` go through the same function (memory stores, dry run or live).

## What is off

- **Phase 1 history, saved places, Home and Work** (decisions 19, 24, 25, 27): CODE-COMPLETE BUT DARK. `history_enabled` is false and `history_active(config)` also needs a notice version listed in `domain.APPROVED_HISTORY_NOTICE_VERSIONS` (empty), so with the flag off every history and saved-place route is a typed 404 DISABLED before any work and nothing is written. Enabling needs Phase 0 exit, the signed legal checklist, an approved notice version added to `APPROVED_HISTORY_NOTICE_VERSIONS`, the `history_records` TTL policy (runbook) and `build_runtime(history_enabled=True, history_notice_version=...)`. See "Phase 1 built dark" below.
- **Places Details and route check**: off until a reviewed `PlacesPriceTable` is passed to `build_runtime(places_price_table=...)` (owner price review). The web shows "not available yet".
- **Account link completion** answers a typed 503 and a deleted account's login is not removed, until Firebase Admin credentials exist (`account.FirebaseAdmin` has no implementation).
- **App Check** is not enforced unless `APP_CHECK_ENFORCE=true`; the web client sends no App Check token and the production web build sends no auth token yet (no Firebase sign-in in `web/`).
- **Retention sweep**: `Runtime.purge_expired()` exists but nothing schedules it; rely on Firestore TTL policies once created.
- No patterns, routines or notifications, no model escalation or second-model fallback. Server history exists but is dark.

## Phase 1 built dark

Everything below does nothing unless `history_active(config)`; REGISTERED_FREE only (a guest gets a typed 403 `GUEST_NOT_ALLOWED`: history, saved places and the ask-state stay on the device).

- **History** (`history.py`, `stores/history_store.py`, `routes/history.py`): opt-in `OutingRecord` = place ID + the user's own label + an activity tag (derived conservatively from the user's own request, never KIDS, or chosen at check-in) + timestamp + outcome + feedback; no Maps content, no coordinates. Needs an active `HISTORY_STORE` consent bound to the approved notice. Written from a confirmed outcome (`POST /v1/outcomes`, server-side storage already gated on consent) and by explicit save (`POST /v1/history/records`, `Idempotency-Key` required); both idempotent by key. `GET /v1/history/records` returns place IDs and labels only; the client re-fetches details via `/v1/places/details` and shows "details unavailable" on a miss. Edit (`PATCH /v1/history/records/{id}`: label, tag, feedback), delete one, clear all, retention (`GET/PUT /v1/history/settings`: default 90, 30 to 180, over 180 rejected, shortening purges at once, lengthening never extends stored records), export (in `POST /v1/me:export`, only while active).
- **Saved places** (`saved_places.py`, `stores/saved_places_store.py`, `routes/saved_places.py`): place ID + label + kind HOME, WORK or CUSTOM + position rounded to 3 decimals. Needs `SAVED_PLACE` consent. Created ONLY by a confirmed `SAVE_PLACE` PendingAction (`ProposeActionRequest.position`, the user's consented foreground position, rounded on parse; held in the pending record only until confirm, cancel or expiry, then scrubbed). `PUT /v1/saved-places` edits the label of an existing entry only (a different kind or position is a 422, an unknown place a 404). One Home and one Work per user; a new one replaces the old only through a confirmed action. A saved Home or Work is never applied silently: the client may send it in the turn `location` field (decision 27).
- **Fixed question** (`ask_policy.py`): "Is where you are now your home? Save it as Home?" (and Work). Shown when the message references home or work (conservative English and French keyword rules, negations ignored), no such saved place exists, 30 days have passed since a decline and the user has not chosen "don't ask again". Server-authored text. Delivered as `TurnResponse.saved_place_prompt` (registered users) and `POST /v1/saved-places/ask-check`; the user's answer goes to `PUT /v1/saved-places/ask-state` (stored only with `SAVED_PLACE` consent, else `recorded=false` and the client keeps it). A guest's client keeps the ask-state and applies the same rules locally. No inference from visit patterns.
- **Cascade**: consent withdrawal runs hooks (`app.state.consent_withdrawal_hooks`, registered in `composition.py`): `HISTORY_STORE` deletes history, retention choice and the outcome ledger; `SAVED_PLACE` deletes saved places, the ask-state and any SAVE_PLACE action records. The cascade also runs on a retried DELETE, so a failed cleanup (503) is retryable. Account deletion registers `history` and `saved_places` deleters. Export contributors fill `ExportBundle.outings` and `saved_places`.
- **Tests**: `tests/test_history_*.py`, `test_saved_places_*.py`, `test_ask_policy.py` over the memory stores and the Firestore fake: flag-off write-count, flag-on without an approved notice, guest refusal, tenant matrix, retention bounds, cooldown, Maps-content, label and position canaries, raw-document scan after account deletion.

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

Checks: `cd backend && python -m pytest -q` (3432 passed, 1 skipped at this snapshot; includes the OpenAPI drift check) and `cd web && npm test && npm run build` (186 tests at this snapshot, the web agent was mid-change). After any API change run `npm run api:generate` in `web/` and commit `openapi.json` and `schema.d.ts`. Install the extras for the verifier tests: `pip install -e ".[dev,auth]"`.

## Known limits

- The gate wording for the guest turn limit and for registered users is the implementer's own text; the owner has not confirmed it.
- The meter counts committed turns only, so a request in flight at the limit can show the 90% notice and then a gated reply.
- The GATED notice still has no create-account button in the web client (the backend link and merge flows exist).
- A user edit that selects a place appends the plan version before the workspace compare-and-set; if the compare-and-set loses, an orphan plan version remains (the workspace pointer is unchanged, the user gets a 409).
- The export bundle has no field for pending-action records (place ID, label, due time); they are deleted on account deletion and expire on their own.
- The per-principal cost windows, the operation limiters, the quota latch and the circuit breakers are still per process (#35).
- The guard classifies English and French only; unspaced Mandarin queries are rejected; bake-off fixtures still expect `en` for Spanish, Portuguese and Punjabi messages.
- `ruff check .` has not run in CI yet and the repository has many pre-existing findings (see `docs/runbooks/cloud-resources-todo.md`).

## Next steps, in order

1. **Security review** (read-only `security-reviewer` role) of the integration: identity wiring, the App Check gate, the deleted-account guard on in-flight writers, the compare-and-set paths, the consent changes and the account routes.
2. **Owner cloud work**: everything in `docs/runbooks/cloud-resources-todo.md` and `docs/runbooks/firebase-setup.md` (Firebase and Admin credentials, App Check, Firestore with TTL policies, Places and Routes enablement and price review, budgets, CI first-run lint).
3. **Web sign-in**: Firebase Auth (anonymous plus Google or email link) and App Check in `web/`, and the create-account button on the gated notice. Until then the production web build cannot authenticate.
4. **Shared state** (#35): move the remaining per-process limiters and the identity-creation and per-IP limits to shared stores; prove the Firestore adapters on the emulator.
5. **Legal gates** (`docs/legal/open-questions.md`): privacy notice (#27, must cover location sent to Google and later stored places), DeepInfra's written zero-retention and no-training terms (#25), the Grounding Lite unit price from billing.
6. **Phase 0 exit** (#20, #21): the evaluation corpus and pilot; then, and only then, the Phase 1 history work behind the dark flag.

## Rules that are easy to break

- Google Maps content: show Grounding Lite output verbatim with its sources immediately after, never edit, trim or intersperse it, never store it beyond the display cache, never give it to the model (decisions 17, 18, 23). From Maps, store only place IDs and user data.
- The model and all tool output are untrusted. The server owns identity, quota, policy and every reply sentence.
- Precise locations never appear in logs, URLs, telemetry, the model context or storage. Tests scan for canary coordinates.
- Anything inferred is stored only after the user confirms it (decision 20). A side effect uses a pending action.
- Credentials stay in `.env` (gitignored). OpenRouter spend is capped for tests; always pass a spend cap.
- Tests are deterministic and need no network. Add a focused test for every policy, tenant, quota and malformed-output boundary.

## Where things are

| Need | Look at |
|---|---|
| Product behavior | `docs/trip-assistant-product-plan.md`, `docs/trip-assistant-roadmap.md` |
| Decisions | `docs/decisions.md` (27 so far) |
| Security and privacy | `docs/trip-assistant-security.md`, `docs/data-and-history-design.md` |
| Access and quotas | `docs/access-tiers.md` |
| Model choice and bake-off | `docs/model-hosting-decision.md`, `backend/evals/frame_bakeoff/` |
| Legal | `docs/legal/` |
| Cloud and release TODOs | `docs/runbooks/` |
| Backlog | `docs/backlog.md`, `docs/project-board.md`, GitHub issues #20–#35 |
| Role briefs | `.codex/agents/`, `.github/agents/`, `.github/copilot-instructions.md` |
