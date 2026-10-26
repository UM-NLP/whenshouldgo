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
- [ ] Decide the Firebase Admin credential approach for revocation checks (workload identity
      preferred over a key file). Never commit a key file.

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

## Composition wiring (owner of `composition.py`)

Add to the production branch only, after reading config:

```python
from .auth_firebase import FirebaseIdentityVerifier
from .appcheck import AppCheckPolicy, AppCheckVerifier, default_fetcher

disabled_uids: set[str] = set()
verifier = FirebaseIdentityVerifier.from_environment(
    os.environ, revocation=revocation_checker_or_None, disabled_uids=disabled_uids
)
resolver = PrincipalResolver(policies=..., disabled_uids=disabled_uids)  # same set object
app_check = AppCheckPolicy(
    enforce=os.environ.get("APP_CHECK_ENFORCE") == "true",
    verifier=AppCheckVerifier(os.environ["FIREBASE_PROJECT_NUMBER"], default_fetcher()),
)
# per protected router:  APIRouter(dependencies=[Depends(app_check)])
```

The `RevocationChecker` (`is_revoked(uid, issued_at) -> bool`) needs the Firebase Admin SDK
(`get_user().disabled` / `tokens_valid_after_timestamp`) and real credentials; it is not
implemented here.

## Dependencies

- [ ] Install the `auth` extra in the production image: `pip install ".[auth]"`
      (PyJWT and cryptography, pinned in `backend/pyproject.toml`).
