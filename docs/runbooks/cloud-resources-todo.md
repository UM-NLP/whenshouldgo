# Cloud resources and release: owner TODO list

Status: code only (decision 26). Everything below needs owner approval to create, and anything that
spends needs separate approval. No value is recorded in the repository; fill each in yourself. The
Firebase and App Check items live in [firebase-setup.md](firebase-setup.md).

The backend fails closed: `create_production_app` raises `ConfigurationError` (static text) until
every required setting below exists, so a half-configured deployment cannot serve traffic.

## Production environment variables (Cloud Run env or Secret Manager)

| Variable | Needed | Meaning |
|---|---|---|
| `FIREBASE_PROJECT_ID` | required | Firebase project ID: token `aud`, and `iss` = `https://securetoken.google.com/<id>` |
| `FIRESTORE_PROJECT_ID` | required | GCP project holding Firestore; without it the build refuses process-local state |
| `PILOT_SPEND_MAX_TOKENS` and/or `PILOT_SPEND_MAX_USD` | required with Firestore | whole-pilot spend cap; the build refuses durable state without one |
| `OPENROUTER_API_KEY` | required | model gateway key (Secret Manager) |
| `GOOGLE_MAPS_API_KEY` | required | Grounding Lite key, also used by Places and Routes once enabled (Secret Manager) |
| `FIREBASE_PROJECT_NUMBER` | for App Check | App Check `iss` and `aud` |
| `APP_CHECK_ENFORCE` | optional | `true` only after the monitor-mode review; empty or `false` records the signal and never blocks; any other value is refused |

The production image installs the `auth` extra (PyJWT, cryptography) and the `gcp` extra
(`google-cloud-firestore`): `backend/Dockerfile` runs `pip install ".[auth,gcp]"`. CI installs
`.[dev,auth]` so the verifier tests run; the Firestore client is never needed by tests.

## Firestore

- [ ] Create the database (the region is a legal and cost decision; see `docs/legal/`).
- [ ] Run the store contract suite against the Firestore emulator in a separate CI job. The adapters
      are only tested on an in-process fake, which does not prove real behaviour (see the section at
      the end of `docs/backlog.md`).
- [ ] Indexes: the adapters use only automatic single-field indexes (equality on `owner_id`, range on
      `expires_at`). No composite index is declared, which is unverified against real Firestore:
      watch for a "requires an index" error in the emulator run.
- [ ] TTL policies on the `expires_at` field of: `workspaces`, `idempotency` (turn replay),
      `display_cache`, `plan_heads`, `plan_versions` (guest rows only carry it), `edit_receipts`,
      `pending_actions` (record expiry = action expiry + 24 hours), `account_link_intents`.
- [ ] No TTL on: `quota_usage`, `consent_ledgers`, `outcome_ledgers` (pruned at write to the 180-day
      ceiling), `accounts`, `account_link_keys`, `account_migrations`, `account_migration_keys`,
      `account_tombstones`, `cost_totals`, `kill_switch`.
- [ ] Security rules: deny all client access (only the backend's service account reads or writes).
- [ ] Load-test `cost_totals/pilot` (one hot document; shard the counter if it contends).
- [ ] Retention sweep: `Runtime.purge_expired()` deletes expired pending actions and display-cache
      entries early. Nothing calls it yet. Either rely on the TTL policies above or add a Cloud
      Scheduler job and an internal, OIDC-authenticated trigger (not built; it must not be a public
      route).

## Firebase Auth, Admin and App Check

- [ ] Everything in [firebase-setup.md](firebase-setup.md).
- [ ] Firebase Admin credentials (workload identity preferred, never a key file in Git). Until they
      exist `create_production_app` passes no `firebase_admin`: `POST /v1/account/link-intents/{id}:complete`
      answers a typed 503 (`SERVER_NOT_CONFIGURED`), and a deleted account's login is not removed
      (its server data is). The code to write is an adapter implementing `account.FirebaseAdmin`
      (`get_account`, `delete_user`) and a `RevocationChecker` for `FirebaseIdentityVerifier`.
- [ ] Decide whether to wire the optional revocation checker (`disabled_uids` is shared with the
      principal resolver and stays empty until one exists).

## Places and Routes (display-only features)

- [ ] Enable the Places API (New) and Routes API for the key's project and restrict the key to them
      and to the server's egress.
- [ ] Review real SKU prices and enter them in a REVIEWED `PlacesPriceTable`
      (`backend/app/places/pricing.py`, `reviewed=True`), then pass it as
      `build_runtime(places_price_table=...)`. With the shipped unreviewed table the feature is OFF:
      the routes answer a typed 503 and no network call is made. The SKU assumptions in that file's
      docstring need your review.
- [ ] Decide whether to turn on the beta WALK mode.
- [ ] Set a Google Cloud budget and API quota: Grounding Lite, Places and Routes spend is not capped
      by the in-process pilot cap (decision 23).

## Budgets and quotas

- [ ] Choose `PILOT_SPEND_MAX_TOKENS` / `PILOT_SPEND_MAX_USD` (owner-tunable; no default is assumed).
- [ ] Confirm the Grounding Lite unit price from billing (`GROUNDING_LITE_USD_PER_REQUEST` is 0 in
      `composition.py` until then).
- [ ] Cloud Run: scale to zero, a max-instances cap, concurrency, request timeout above the turn
      deadline, minimum CPU only while a request runs.

## CI and repository

- [ ] First CI run: `ruff check .` has not been run in CI yet and the repository has many findings
      (hundreds at the 2026-10-26 snapshot with ruff 0.17: unused imports and style rules in tests,
      re-exports in `app/gateway/__init__.py`, `print` in scripts). Either clean them, narrow the
      rule set in `backend/pyproject.toml`, or make the lint job non-blocking at first; that is a
      decision for you, not applied here.
- [ ] Branch protection on `main`: required checks `backend`, `web`, `openapi-drift`, `repo-hygiene`.
- [ ] After any API change run `npm run api:generate` in `web/` and commit both generated files.

## Legal gates that still block a pilot

See `docs/legal/open-questions.md`: the privacy notice (#27), DeepInfra's written zero-retention and
no-training terms (#25), and counsel review of stored saved places (decision 24). History and saved
places stay dark (`history_enabled` false) until Phase 0 exit and the signed legal checklist.
