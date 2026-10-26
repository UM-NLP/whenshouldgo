# WhenShouldGo web (slice 1)

Mobile-first chat screen with a server-driven usage meter (bootstrap on load, updated from each turn; the client never computes entitlements): reply, Google's grounded block verbatim with its sources, and the
foreground location consent flow. Phase 0 only.

## Run (dev, dry run: no keys, no network)

Terminal 1, from `backend/`:

    python scripts/dev_server.py --i-understand-this-is-dev --dry-run

Terminal 2, from `web/`:

    npm install
    npm run dev          # http://127.0.0.1:5173, proxies /v1 to 127.0.0.1:8787

Live: replace the first command with
`python scripts/dev_server.py --i-understand-this-is-dev --env-file C:\path\to\.env --max-spend-usd 0.05`.

## Contract types

`npm run api:generate` exports the backend OpenAPI schema (`backend/scripts/export_openapi.py`)
and regenerates `src/api/schema.d.ts` with openapi-typescript. Re-run it after any backend
contract change and commit both generated files.

## Checks

    npm test
    npm run build

The dev auth provider and the dev server are NON-PRODUCTION. A production build gets a
`NoAuthProvider` and sends no token.


## Hosting headers (production)

Set these at the hosting layer (a `<meta>` CSP cannot set `frame-ancestors`, and a strict meta CSP
would break the Vite dev server's HMR, so none is baked into `index.html`):

    Content-Security-Policy: default-src 'self'; connect-src 'self'; frame-ancestors 'none'
    Permissions-Policy: geolocation=(self)

`connect-src 'self'` assumes the API is served from the same origin (or add its origin
explicitly). Google text is rendered as plain text and Google links are plain `https` anchors.


## Local-first storage, offline outbox and service worker (decisions 18, 19, 27)

- `src/storage/`: typed IndexedDB wrapper (`wsg-local`, `schema_version` per record, migration stub in
  `db.ts`). Stores: `messages`, `drafts`, `plans`, `reminders`, `outing_log`, `saved_places`,
  `ask_state`, plus `outbox` and `meta`. `schema.ts` is an allow-list write guard: unknown fields,
  Maps-looking strings and coordinates that are not rounded to 3 decimals are refused. No Maps content
  is stored: the user's messages and server-authored reply text only, with a `had_discovery` flag, so
  after a reload the UI says "Results are not stored; ask again". `exportAll()`, `deleteAll()`,
  `deleteItem()` and `purgeExpired()` (outing log, default 90 days, 30 to 180, purged on open) work
  at every meter state, including GATED.
- First-save notice: nothing is written until the user accepts "saved on this device only"
  (`useLocalNotice`); declining keeps everything in memory. Delete-all clears the flag too.
- `src/outbox/`: offline queue for turns. The idempotency key and the user's text are persisted (only
  after the notice); a consented location lives in memory only (the record has no location field,
  `location_dropped` is set). Replay is in order with the same key; 4xx other than 429 stops the queue
  with a typed state; 429 honors Retry-After; transient failures retry with the same key.
- `src/sw.ts` + `src/sw-policy.ts`: caches the same-origin app shell only (never `/v1`, never
  cross-origin, never `no-store` or failed responses), versioned cache with cleanup on activate. Built
  as `/sw.js` by `vite build` (second entry); registered only in production over HTTPS or localhost
  (`src/sw-register.ts`); an update shows a "Reload to update" banner. `npm run dev` registers nothing.
- Serve `/sw.js` with `Cache-Control: no-cache` and from the site root so its scope is `/`.


## Trip Board, place and route cards, handoff, reminders, check-in (Phase 0 journey)

Code: `src/board/` (typed API layer `boardApi.ts`, view-models `viewModels.ts`, local reminders
`reminders.ts`), `src/useBoard.ts`, `src/components/board/`. The chat gets the board only when a
`boardApi` is passed (`main.tsx` does); otherwise nothing is requested.

- Board: goal, assumptions (unconfirmed) and confirmed constraints as editable chips (edit, add, remove,
  one-tap "Confirm" on an assumption; the server stamps the confirmation). Edits are
  `PATCH /v1/workspace` with `base_version` and an `Idempotency-Key`; a retry reuses both; a 409 shows the
  conflict state with "Reload the board". Up to three places (place ID plus the user's own label, in memory),
  timeline, warnings, pending actions and the plan version. Earlier plan versions are listed by number.
- Pick: "Check this place" appears after Google's block (never inside it) and for every source (the typed `place_id`). `POST /v1/places/details` renders name, address, open-now and rating as text with "Google Maps"
  immediately after the fields. `POST /v1/routes/check` (DRIVE) shows "About N minutes by car" (rounded to 5)
  only with location consent; otherwise a fixed prompt opens the existing consent flow. The position is read
  inside the tap, sent once, never kept. Card and route data live in memory only.
- Actions: the server's preview, Confirm / Cancel, expiry text. The nonce stays in memory and is never
  rendered. Confirm sends the action's own `idempotency_key`, so a retry repeats the same key and nonce.
  LAUNCH_HANDOFF opens the server-returned URL with `noopener,noreferrer` only if it is https on
  `www.google.com/maps` (also offered as a link), and works while the meter is GATED. SET_REMINDER saves
  place ID + the user's label + due time via the storage layer, after the first-save notice.
- Reminders: due and upcoming (next hour) reminders show as a banner when the app is open, with Snooze 10
  minutes, Dismiss and "Did you go?". A closed web tab cannot fire a reminder; nothing runs in the background.
- Check-in: WENT / DID_NOT_GO / CHANGED_PLAN plus an optional note, `POST /v1/outcomes` with a per-check-in key
  (reused on retry). If the ack says `recorded: false` (or the route is not available yet) the report is kept
  in the local `outing_log` (place ID and the user's label only). The note is never stored or logged.
- Every failure shows fixed copy chosen by kind; a typed 501/503 DISABLED means "not available yet".

### Contract notes

The gaps the board work found were closed by the backend integration (2026-10-26); the client now uses
typed fields only:

- `DiscoveryPlace.place_id` is typed, so "Check this place" appears for every source (a source whose
  ID is empty still gets no button). The client never parses a URL or text for an ID.
- `ConfirmActionResponse.handoff_url` and `reminder` are typed. The handoff URL is still pinned to
  `https://www.google.com/maps` before it is opened; the reminder saved on the device uses the
  server's confirmed `due_at`.
- `ProposeActionRequest.due_at` is sent for SET_REMINDER. The due time lives in the server's proposal,
  so choosing another time creates a new proposal (new key) and cancels the old one.
- `WorkspaceView` carries `latest_plan`, `warnings` and `plan_versions`: the board shows the plan
  version, earlier version numbers and the server's warnings as fixed copy. An action or activation
  names `latest_plan.version` (1 while there is no plan yet), not the workspace version.
- `Idempotency-Key` is declared in OpenAPI for PATCH workspace, propose, confirm (optional, must be the
  action's own key) and outcomes, and is sent through the typed `params.header`.
- `classifyFailure` has an `unavailable` kind: 501, NOT_IMPLEMENTED, DISABLED and a 503 with no
  transient provider reason. A 503 PROVIDER_* reason stays a retryable provider error.
- Still missing: an App Check token (not sent yet) and real Firebase config (owner TODO, see below).


## Accounts, sign-in, merge, export and delete (`src/firebase.ts`, `src/account/`)

- `src/firebase.ts`: the `AuthService` interface (`signInAnonymously`, `linkWithGoogle`, `sendEmailLink` +
  `linkWithEmailLink`, `continueAsExisting`, `getIdToken`, `signOut`). Implementations: the real
  `FirebaseAuthService` (pinned `firebase@13.0.0`, imported lazily on first use and only when all four
  `VITE_FIREBASE_API_KEY`, `_AUTH_DOMAIN`, `_PROJECT_ID`, `_APP_ID` are set), `NotConfiguredAuthService`
  (a production build with no config: nothing signs in, no token is sent, and a fixed "sign-in is not
  configured" notice shows) and `DevAuthService` in `src/firebaseDev.ts` (dev only; referenced under
  `import.meta.env.DEV`, so it is absent from `dist/`; `firebase.test.ts` greps `dist/` for the dev token).
  No real config is in the repository. Owner TODO: create the Firebase web app, set the four variables for
  the production build, add App Check (docs/runbooks/firebase-setup.md).
- The Firebase SDK keeps its own session in its own browser storage so an anonymous uid survives a reload.
  App code never stores a token, email or intent nonce (in memory only) and never logs or puts them in a URL.
  The email link's continue URL carries no email; the user types it again on return.
- Flow (`useAccount`): "Create a free account" (90% notice and the gate; shown only for a guest whose
  server `can_link_account` is not false) -> `POST /v1/account/link-intents` -> provider link ->
  `POST .../{id}:complete` -> re-bootstrap. Cancelling at any step consumes nothing. A collision (from the
  provider, or a 409 `ACCOUNT_COLLISION` on complete) parks the intent, signs in as the existing account and
  opens the explicit merge screen (`useMerge`): `POST /v1/account-migrations` manifest, item checkboxes,
  `KEEP_REGISTERED` default or `USE_GUEST`, `:run`, and "Continue copying" after a 503 `MIGRATION_INTERRUPTED`
  (the same call resumes). The intent id and nonce live in memory only, so a page reload during a merge
  cannot resume it (assumption; a durable resume needs a server-side lookup of the open migration).
- `DataRights`: "Export my account data" (`POST /v1/me:export`, downloaded from the response, never stored)
  and "Delete my account data" (confirm dialog, `POST /v1/me:delete`, then the existing `deleteAll()`/
  `session.deleteData()` and sign-out). Neither depends on the allowance, so both work while GATED.
  501, `DISABLED` and `SERVER_NOT_CONFIGURED` read as "not available yet".

## Phase 1 UI, behind the server capability (`src/history/`, `src/places/Saved*`)

Nothing renders, and nothing is requested, unless the server's `Capabilities.history_enabled` is true
(taken from `session:bootstrap`; never from client state or env) and the app wires `phase1` (`main.tsx` does).

- Guests (decision 19/27): history, saved places and the ask-state live in the existing `outing_log`,
  `saved_places` and `ask_state` stores. The retention notice states the period (default 90, ceiling 180)
  and `RetentionPicker` shortens it through `store.setRetentionDays` (purges at once). Registered users:
  `/v1/history/records` and `/v1/saved-places`, each behind a consent prompt (`HISTORY_STORE`, `SAVED_PLACE`)
  with a PLACEHOLDER notice clearly marked pending counsel (issue #27).
- History view: the user's label and activity tag; "Show place details" fetches `POST /v1/places/details` by
  place ID into memory only ("Details unavailable" on a miss); edit label, delete one, clear all, export.
- Saved places: list, rename, delete, export. Home, Work and custom places are created ONLY by the confirmed
  `SAVE_PLACE` PendingAction (the board's `ActionCard`): the position is read inside the tap, rounded to 3
  decimals, held in memory, and written (device store or server upsert) only after the server confirms.
- The fixed question "Is where you are now your home? Save it as Home?" shows only when the guest's local
  deterministic rule fires (`places/SavedAskPolicy.ts`: a "near home/work" phrase in the user's own message,
  no saved label, no don't-ask-again, no decline in the last 30 days) or, for registered users, when the
  server signals it (not in the contract yet, so never today).
- A saved Home/Work is never applied silently: "Use my saved Home as the starting area" is an explicit tap that
  arms ONE next message to carry the rounded saved position through the existing consented `location` path.
- Everything that depends on a still-moving backend contract is in `src/history/contractAdapter.ts`.
