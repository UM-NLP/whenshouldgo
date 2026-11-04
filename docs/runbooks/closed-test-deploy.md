# Closed-test deployment (backend on Cloud Run, web on Firebase Hosting)

Status 2026-11-04: the image `northamerica-northeast1-docker.pkg.dev/departure-method-test/wsg/api:35b0cef`
is built and pushed. The Cloud Run deploy, Hosting deploy and scheduler job below have NOT been run: the
assistant's deploy command was blocked by the permission classifier, so the owner runs them. No value
below is a secret. Owner approved the plan and a US$2 pilot cap on 2026-11-04.

Gates still open (see `docs/legal/open-questions.md`): privacy notice (#27), DeepInfra terms (#25).
Treat this as a closed test for people you trust and do not share the URL publicly.

## 1. Backend (Cloud Run)

```
P=departure-method-test; R=northamerica-northeast1
IMG=$R-docker.pkg.dev/$P/wsg/api:35b0cef
gcloud run deploy wsg-api --image=$IMG --region=$R --project=$P \
  --service-account=wsg-backend@$P.iam.gserviceaccount.com \
  --allow-unauthenticated --min-instances=0 --max-instances=2 --concurrency=20 --cpu=1 --memory=512Mi --timeout=120 \
  --set-env-vars=FIREBASE_PROJECT_ID=$P,FIREBASE_PROJECT_NUMBER=568880043351,FIRESTORE_PROJECT_ID=$P,PILOT_SPEND_MAX_USD=2,APP_CHECK_ENFORCE=false \
  --set-secrets=OPENROUTER_API_KEY=openrouter-api-key:latest,GOOGLE_MAPS_API_KEY=google-maps-api-key:latest
```

`--allow-unauthenticated` only opens the Cloud Run front door; the app still rejects every route except
`/healthz` without a valid Firebase token (fail closed). If the revision does not start, read its logs:
`create_production_app` raises a static `ConfigurationError` naming what is missing.

## 2. Purge scheduler (after step 1)

```
URL=$(gcloud run services describe wsg-api --region=$R --project=$P --format='value(status.url)')
gcloud run services update wsg-api --region=$R --project=$P \
  --update-env-vars=PURGE_OIDC_AUDIENCE=$URL,PURGE_CALLER_SERVICE_ACCOUNT=wsg-scheduler@$P.iam.gserviceaccount.com
gcloud run services add-iam-policy-binding wsg-api --region=$R --project=$P \
  --member=serviceAccount:wsg-scheduler@$P.iam.gserviceaccount.com --role=roles/run.invoker
gcloud scheduler jobs create http wsg-purge --location=$R --project=$P --schedule="17 4 * * *" --time-zone="America/Toronto" \
  --uri=$URL/internal/purge --http-method=POST \
  --oidc-service-account-email=wsg-scheduler@$P.iam.gserviceaccount.com --oidc-token-audience=$URL
```

The service account `wsg-scheduler` already exists.

## 3. Web (Firebase Hosting)

Public web config values are in the operator's `web_firebase.env` (not in Git). From `web/`:

```
set -a; . <path>/web_firebase.env; set +a
npm ci && npm run build
npx firebase-tools@15.34.2 login            # interactive
npx firebase-tools@15.34.2 deploy --only hosting --project departure-method-test
```

`web/firebase.json` rewrites `/v1/**` and `/healthz` to the `wsg-api` Cloud Run service (one origin, no CORS)
and sets the CSP, Permissions-Policy (geolocation only for self) and `Cache-Control: no-cache` on `/sw.js`.
Site: https://departure-method-test.web.app (already an authorized sign-in domain).

## 4. After deploying

- Sign in as an owner/test user on the site; confirm a turn works and the usage meter shows.
- Google sign-in: the OAuth consent screen stays in Testing; add each pilot user as a test user.
- Set a billing budget alert you actually watch (the CA$10 `whenshouldgo-dev-low` budget exists).
- Rollback: `gcloud run services delete wsg-api --region=$R --project=$P` and
  `npx firebase-tools hosting:disable --project departure-method-test`.
