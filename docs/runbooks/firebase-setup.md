# Firebase Auth and App Check setup (owner TODOs)

Status: code only (decision 26). `backend/app/auth_firebase.py` and `backend/app/appcheck.py`
verify tokens with Google's public keys; nothing here has been created or run against a real
project. Every item below needs owner approval, and any spend needs separate approval.
Fill in values yourself; none are recorded in the repository.

## Project and Auth

- [ ] Create or choose the Firebase/GCP project; note its project ID and project number.
- [ ] Enable Firebase Authentication with the Anonymous, Google and Email link (passwordless)
      providers.
- [ ] Leave anonymous-account automatic cleanup OFF (docs/access-tiers.md: it would create a
      time-based registration gate the product has not chosen).
- [ ] Add the production and preview web origins to Auth authorized domains.
- [x] Credential approach decided: Application Default Credentials via workload identity on Cloud
      Run. Never commit a key file. Grant the service account a custom role with
      `firebaseauth.users.get` and `firebaseauth.users.delete` only.

## App Check

- [ ] Register the web app and choose an App Check provider for it (reCAPTCHA Enterprise or the
      provider the owner picks); create the site key.
- [ ] Start in monitor mode; review the verified/invalid ratio before enforcing.
- [ ] Decide when to enforce for Auth and the backend; enforcement on the backend is the
      `APP_CHECK_ENFORCE` setting below.

## Backend configuration (Cloud Run env or Secret Manager)

- [ ] `FIREBASE_PROJECT_ID` = the project ID (used for `aud`, and `iss` =
      `https://securetoken.google.com/<project id>`). Production fails closed without it.
- [ ] `FIREBASE_PROJECT_NUMBER` = the project number (App Check `iss` and `aud`).
- [ ] `APP_CHECK_ENFORCE` = `true` only after the monitor-mode review; default off.

## Composition wiring (done in code)

`backend/app/composition.py` wires this; there is nothing to paste. `server.create_production_app`
calls `firebase_identity_from_env` (needs `FIREBASE_PROJECT_ID`; lazy import of the `auth` extra) and
passes the verifier and its `disabled_uids` set to `build_runtime`, whose `PrincipalResolver` receives
the SAME set object. App Check is built from `FIREBASE_PROJECT_NUMBER` and `APP_CHECK_ENFORCE` and
installed as a router-level gate on every route except `/healthz` (`routes/common.app_check_gate`;
the web client does not send a token yet). The `account.FirebaseAdmin` adapter and the `RevocationChecker`
(`backend/app/firebase_admin_adapter.py`, Identity Toolkit REST with ADC) are built and wired by
`firebase_admin_from_env` whenever `FIREBASE_PROJECT_ID` is set and the `gcp` extra is installed;
`FIREBASE_ADMIN=disabled` is the emergency off switch. IAM needed:
`firebaseauth.users.get`, `firebaseauth.users.delete`. See [cloud-resources-todo.md](cloud-resources-todo.md).

## Dependencies

- [x] The `auth` extra is installed in the production image (`backend/Dockerfile`) and in CI
      (PyJWT and cryptography, pinned in `backend/pyproject.toml`).
