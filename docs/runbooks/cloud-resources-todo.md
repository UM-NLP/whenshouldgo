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
- [x] The store contract suites also run against the Firestore emulator: CI job `firestore-emulator`
      (a pinned `google-cloud-cli` emulators image), locally by setting `FIRESTORE_EMULATOR_HOST`
      (the `emulator` parametrisation is skipped without it). Findings are at the end of
      `docs/backlog.md`. The emulator does NOT enforce TTL and does not demand composite indexes.
- [ ] Indexes: the adapters use only automatic single-field indexes (equality on `owner_id`, range on
      `expires_at`). No composite index is declared, which is unverified against real Firestore:
      watch for a "requires an index" error in the emulator run.
- [ ] TTL policies on the `expires_at` field of: `workspaces`, `idempotency` (turn replay),
      `display_cache`, `plan_heads`, `plan_versions` (guest rows only carry it), `edit_receipts`,
      `pending_actions` (record expiry = action expiry + 24 hours; a SAVE_PLACE record that still
      holds the user's position expires with its action, no grace), `account_link_intents`, and
      the Phase 1 collection `history_records` (`expires_at` = the record's retention expiry:
      90 days by default, 180 at most). Create the `history_records` policy BEFORE history is
      enabled; TTL deletion is late, the adapters also hide expired records on read.
- [ ] No TTL on: `quota_usage`, `consent_ledgers`, `outcome_ledgers` (pruned at write to the 180-day
      ceiling), the Phase 1 collections `history_settings` (the user's retention choice, deleted on
      consent withdrawal), `saved_places` and `saved_place_asks` (kept until the user deletes them,
      withdraws SAVED_PLACE or deletes the account), `accounts`, `account_link_keys`, `account_migrations`, `account_migration_keys`,
      `account_tombstones`, `cost_totals`, `kill_switch`.
- [ ] Phase 1 collections (code only, DARK until `history_enabled` and an approved notice):
      `history_records` (one doc per OutingRecord: place ID, the user's label, tag, timestamps,
      outcome, feedback; no Maps content), `history_settings`, `saved_places` (place ID, label,
      kind, position rounded to 3 decimals), `saved_place_asks` (when a Home/Work question was
      declined, "don't ask again"). Indexes: automatic single-field only (equality on `owner_id`,
      range on `expires_at` for `history_records`); no composite index is declared. Every
      document has `schema_version`, `kind`, `owner_id` and a hashed id.
- [ ] Security rules: deny all client access (only the backend's service account reads or writes).
- [ ] Load-test `cost_totals/pilot` (one hot document; shard the counter if it contends).
- [ ] Retention sweep (built, NOT scheduled). `POST /internal/purge` (`backend/app/internal.py`) runs
      `Runtime.purge_expired(limit)`: on Firestore it deletes expired documents early from every
      collection that holds user data with `expires_at` (`backend/app/retention.py`: `history_records`,
      `outcome_ledgers`, `pending_actions` (this is where an expired SAVE_PLACE record holding a
      position lives), `display_cache`, `plan_heads`, `plan_versions`, `idempotency`, `edit_receipts`,
      `account_link_intents`, `workspaces`), at most `PURGE_MAX_DOCUMENTS` per call (default 500, max
      5000), and returns counts only. TTL policies stay the primary mechanism; this is the early,
      auditable backstop. `saved_places` and `saved_place_asks` have no expiry by design.
      The route is not mounted (404) unless BOTH variables below are set, is never in the OpenAPI
      document, is not behind App Check or Firebase auth, and accepts only a Google-signed OIDC ID
      token (audience and verified email checked), rate limited per process.

      | Variable | Meaning |
      |---|---|
      | `PURGE_OIDC_AUDIENCE` | the Cloud Run service URL the scheduler signs for (the token `aud`) |
      | `PURGE_CALLER_SERVICE_ACCOUNT` | email of the scheduler's service account (the token `email`) |
      | `PURGE_MAX_DOCUMENTS` | optional, per-call document cap |

      Create after Cloud Run exists (placeholders in angle brackets; NOT run, no service URL yet).
      The service account needs only `roles/run.invoker` on this service (Cloud Run IAM stays
      authenticated; the app then re-checks the token itself):

      ```
      gcloud iam service-accounts create wsg-purge-caller --project=<PROJECT>
      gcloud run services add-iam-policy-binding <SERVICE> --region=northamerica-northeast1         --member=serviceAccount:wsg-purge-caller@<PROJECT>.iam.gserviceaccount.com         --role=roles/run.invoker --project=<PROJECT>
      gcloud scheduler jobs create http wsg-purge --project=<PROJECT>         --location=northamerica-northeast1 --schedule="17 */6 * * *" --time-zone=Etc/UTC         --uri="<SERVICE_URL>/internal/purge" --http-method=POST         --oidc-service-account-email=wsg-purge-caller@<PROJECT>.iam.gserviceaccount.com         --oidc-token-audience="<SERVICE_URL>" --attempt-deadline=120s --max-retry-attempts=1
      ```

      Set `PURGE_OIDC_AUDIENCE=<SERVICE_URL>` and
      `PURGE_CALLER_SERVICE_ACCOUNT=wsg-purge-caller@<PROJECT>.iam.gserviceaccount.com` on the
      service. A busy backlog drains over successive runs (each deletes up to the cap); raise the
      schedule frequency or the cap if the counts stay at the cap.

## Firebase Auth, Admin and App Check

- [ ] Everything in [firebase-setup.md](firebase-setup.md).
- [x] Adapter built: `backend/app/firebase_admin_adapter.py` (`FirebaseAdminClient` implements
      `account.FirebaseAdmin`; `FirebaseRevocationChecker` implements `RevocationChecker`, 60 s
      per-uid cache). `create_production_app` builds both when `FIREBASE_PROJECT_ID` is set and the
      `gcp` extra (google-auth) is installed. `FIREBASE_ADMIN=disabled` turns both off (emergency);
      any other value but `enabled` is refused at boot.
- [ ] Grant the Cloud Run service account a custom role with exactly two permissions,
      `firebaseauth.users.get` (accounts:lookup) and `firebaseauth.users.delete` (accounts:delete).
      Do not use `roles/firebaseauth.admin` (it also creates and updates users). Credentials are
      Application Default Credentials (workload identity); never a key file.
- [ ] Verify in the pilot project: link completion no longer answers 503 and `me:delete` returns
      `completed=true`.

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

## Provisioned on 2026-10-27 (done by the assistant with the owner's authorisation)

Project `departure-method-test` (number 568880043351, billing enabled, Firebase project already present).
No secret value is recorded here. Nothing is deployed and no Cloud Run service exists.

- APIs enabled: Firestore, Firebase, Identity Toolkit, App Check, Firebase Rules, Secret Manager, Cloud Run,
  Artifact Registry, Cloud Build, IAM, Resource Manager, Cloud Scheduler (Grounding Lite and Routes were already on).
- Firestore `(default)`, native mode, `northamerica-northeast1` (Montreal), delete protection ON (decision 29).
- Firestore TTL policies on `expires_at`, all ACTIVE: `workspaces`, `idempotency`, `display_cache`, `plan_heads`,
  `plan_versions`, `edit_receipts`, `pending_actions`, `account_link_intents`, `history_records`. No TTL on
  `outcome_ledgers` (one document per user, pruned at write) or the no-TTL collections listed above.
- Firestore security rules: deny all client access (released as `cloud.firestore`); only the backend service account
  reaches the data.
- Firebase Auth (Identity Platform initialised): anonymous ON, email link ON (passwordless), anonymous auto-delete OFF.
  Google sign-in is NOT enabled: it needs an OAuth consent screen and client ID created in the console (owner).
- Firebase web app "WhenShouldGo Web". Its public config was written outside the repository; supply the four
  `VITE_FIREBASE_*` values at web build time. Restrict that browser key (HTTP referrers, Identity Toolkit only)
  once the hosting domain is known.
- Secret Manager (user-managed, `northamerica-northeast1`): `openrouter-api-key`, `google-maps-api-key` (copied from the
  local `.env`; rotate there and add a version).
- Service account `wsg-backend@departure-method-test.iam.gserviceaccount.com` with `roles/datastore.user`,
  `secretmanager.secretAccessor` on the two secrets only, and the custom role `wsgFirebaseAuthUsers`
  (`firebaseauth.users.get`, `firebaseauth.users.delete`) for the Firebase Admin adapter.
- Artifact Registry Docker repository `wsg` in `northamerica-northeast1`.
- Budget `whenshouldgo-dev-low` (CA$10) already existed.

Still open for you: Google sign-in OAuth client, App Check provider and keys (needs a reCAPTCHA key), the Places API
and key restriction extension (feature stays off until prices are reviewed), the owner-chosen
`PILOT_SPEND_MAX_*` values, Cloud Run deployment (deliberately not done: the privacy notice and legal checklist are
unsigned), and creating the scheduled purge job (the trigger is built; the command is under
"Retention sweep").

