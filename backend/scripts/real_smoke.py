"""Real-service smoke test against the REAL Google Cloud project (a script, not part of the app).

Run from ``backend/``. It talks to Firestore, Identity Toolkit and Secret Manager of one project
(default ``departure-method-test``) and, for check 4, to OpenRouter and Grounding Lite (a few
cents at most, hard-capped by ``--max-spend-usd``). Nothing is mocked.

    python scripts/real_smoke.py --env-file ..\\.env --web-env-file C:\\path\\web_firebase.env \\
        --ssl-mode os-store-nonstrict --json-out summary.json

Checks (``--skip firestore,identity,secrets,turn`` omits some):
  1 firestore  the real adapters in app/stores/firestore.py under the wsg-backend service account,
               in a run-scoped collection prefix, deleted at the end (zero leftovers verified)
  2 identity   throwaway anonymous user: verify token, get_account, revocation, delete, re-verify
  3 secrets    the service account reads exactly the two secrets; create_production_app boots
  4 turn       one full turn through the production composition on real Firestore, real model and
               real Grounding Lite; replay, then POST /v1/me:delete and a leftover sweep

Credentials: no key file is ever written. The script mints short-lived access tokens with
``gcloud auth print-access-token --impersonate-service-account=<wsg-backend SA>`` (the caller needs
roles/iam.serviceAccountTokenCreator on that one SA) and installs them as the process's Application
Default Credentials by replacing ``google.auth.default`` for this process only. The checks therefore
exercise the SAME least-privilege identity the deployed backend will use.

Output: PASS/FAIL lines with counts, durations and costs only. Never a document body, token, uid,
email, key or Maps text. ``--json-out`` is sanitized the same way. TLS verification is never
disabled (see ``live_smoke.build_ssl_context``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for _p in (str(BACKEND), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from live_smoke import (  # noqa: E402
    SmokeRefused,
    build_ssl_context,
    ledger_totals,
    live_transports,
    parse_env_file,
)

from app.composition import (  # noqa: E402
    GOOGLE_MAPS_KEY_VAR,
    OPENROUTER_KEY_VAR,
    ConfigurationError,
)

DEFAULT_PROJECT = "departure-method-test"
DEFAULT_SA = "wsg-backend@departure-method-test.iam.gserviceaccount.com"
SECRETS = ("openrouter-api-key", "google-maps-api-key")
CHECKS = ("firestore", "identity", "secrets", "turn")
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
THREADS = 12
TURN_LIMIT = 5
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/ +=<>,-]{0,60}$")
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_COORD_PAIR = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")
_COORD_KEYS = frozenset({"lat", "lng", "lon", "latitude", "longitude"})
# Every collection the backend can write (docs/runbooks/cloud-resources-todo.md).
ALL_COLLECTIONS = (
    "quota_usage", "workspaces", "idempotency", "display_cache", "consent_ledgers", "cost_totals",
    "kill_switch", "accounts", "account_link_intents", "account_link_keys", "account_migrations",
    "account_migration_keys", "account_tombstones", "plan_heads", "plan_versions",
    "outcome_ledgers", "edit_receipts", "pending_actions", "history_records", "history_settings",
    "saved_places", "saved_place_asks")
TTL_COLLECTIONS = (
    "workspaces", "idempotency", "display_cache", "plan_heads", "plan_versions", "edit_receipts",
    "pending_actions", "account_link_intents", "history_records")


# --- pure helpers (unit-tested; no network) ------------------------------------------------------
def parse_skip(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    names = {part.strip() for part in text.split(",") if part.strip()}
    unknown = names - set(CHECKS)
    if unknown:
        raise SmokeRefused("unknown check in --skip")
    return frozenset(names)


def run_marker() -> str:
    """A unique, collection-name-safe marker for this run."""
    return "smk" + uuid.uuid4().hex[:10]


def sanitize_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Only numbers, booleans and short plain tokens survive; anything else becomes a marker."""
    clean: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, bool | int | float):
            clean[str(key)] = value
        elif isinstance(value, str) and SAFE_VALUE.fullmatch(value):
            clean[str(key)] = value
        else:
            clean[str(key)] = "<redacted>"
    return clean


def safe_error(error: BaseException) -> str:
    """Class name, plus an HTTP/gRPC code when the exception carries one. Never its text."""
    name = type(error).__name__
    code = getattr(error, "code", None)
    code = code() if callable(code) else code
    code_name = getattr(code, "name", None) or (code if isinstance(code, int) else None)
    return f"{name}:{code_name}" if code_name is not None else name


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        return "<bytes>"
    return value


def find_leaks(doc: Any, needles: list[str]) -> dict[str, int]:
    """Counts (never contents) of Maps-text needles, coordinate pairs and coordinate keys."""
    plain = to_jsonable(doc)
    text = json.dumps(plain, ensure_ascii=False, default=str)
    keys: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                keys.append(str(k).lower())
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plain)
    return {
        "maps_text": sum(1 for n in needles if len(n) >= 6 and n in text),
        "coordinate_pairs": len(_COORD_PAIR.findall(text)),
        "coordinate_keys": sum(1 for k in keys if k in _COORD_KEYS),
    }


def usd_to_cad(usd: Decimal, rate: Decimal) -> Decimal:
    return usd * rate


def check_project(project: str, allow_other: bool) -> str:
    if not isinstance(project, str) or not _PROJECT_ID.fullmatch(project):
        raise SmokeRefused("the project id is malformed")
    if project != DEFAULT_PROJECT and not allow_other:
        raise SmokeRefused("only the departure-method-test project is allowed without "
                           "--allow-other-project")
    return project


class PrefixedClient:
    """Wraps a Firestore client so every collection name gets a run-scoped prefix. The adapters
    are used unmodified; everything else is delegated."""

    def __init__(self, client: Any, prefix: str) -> None:
        if not re.fullmatch(r"[a-z0-9_]{4,40}_", prefix):
            raise ValueError("prefix must be lowercase letters, digits and underscores, ending _")
        self._client, self.prefix = client, prefix

    def collection(self, name: str) -> Any:
        return self._client.collection(self.prefix + name)

    def __getattr__(self, name: str) -> Any:  # transaction(), etc.
        if name == "transactional":
            raise AttributeError(name)  # the real client has none; the adapter imports its own
        return getattr(self._client, name)


class DictCodec:
    """Replay codec stand-in for the store-level checks (the record is a plain dict)."""

    @staticmethod
    def encode(record: Any) -> Any:
        return record

    @staticmethod
    def decode(doc: Any) -> Any:
        return doc


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | SKIP
    seconds: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


class Reporter:
    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit, self.results = emit, []  # type: ignore[var-annotated]

    def add(self, name: str, ok: bool | None, seconds: float = 0.0, **detail: Any) -> bool:
        status = "SKIP" if ok is None else "PASS" if ok else "FAIL"
        clean = sanitize_detail(detail)
        self.results.append(Result(name, status, round(seconds, 2), clean))
        extra = " ".join(f"{k}={v}" for k, v in clean.items())
        self.emit(f"  {status} {name} ({seconds:.2f}s) {extra}".rstrip())
        return bool(ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "FAIL")

    def as_json(self) -> list[dict[str, Any]]:
        return [{"name": r.name, "status": r.status, "seconds": r.seconds, "detail": r.detail}
                for r in self.results]


# --- credentials ---------------------------------------------------------------------------------
def gcloud_access_token(impersonate: str | None) -> str:
    exe = shutil.which("gcloud.cmd") or shutil.which("gcloud")
    if exe is None:
        raise SmokeRefused("gcloud is not on PATH")
    cmd = [exe, "auth", "print-access-token"]
    if impersonate:
        cmd.append(f"--impersonate-service-account={impersonate}")
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SmokeRefused("gcloud could not mint an access token") from None
    token = done.stdout.strip()
    if done.returncode != 0 or not token or " " in token:
        raise SmokeRefused("gcloud could not mint an access token (is "
                           "roles/iam.serviceAccountTokenCreator granted on the service account?)")
    return token


def make_credentials(impersonate: str | None) -> Any:
    from google.auth import credentials as gcreds

    class GcloudCredentials(gcreds.Credentials):
        def refresh(self, request: Any) -> None:  # noqa: ARG002
            self.token = gcloud_access_token(impersonate)
            self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=45)

    creds = GcloudCredentials()
    creds.refresh(None)
    return creds


def install_adc(creds: Any, project: str) -> Callable[[], None]:
    """Replace ``google.auth.default`` for this process (no file, no env var). Returns restore."""
    import google.auth

    original = google.auth.default
    google.auth.default = lambda *a, **k: (creds, project)  # type: ignore[assignment]

    def restore() -> None:
        google.auth.default = original  # type: ignore[assignment]

    return restore


def authed_session(creds: Any, ctx: Any = None) -> Any:
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(creds)


# --- Firestore helpers ---------------------------------------------------------------------------
def count_docs(client: Any, name: str) -> int:
    return sum(1 for _ in client.collection(name).stream())


def doc_ids(client: Any, name: str) -> set[str]:
    return {d.id for d in client.collection(name).stream()}


def delete_collection(client: Any, name: str) -> int:
    n = 0
    for snap in client.collection(name).stream():
        snap.reference.delete()
        n += 1
    return n


def run_threads(count: int, fn: Callable[[int], Any]) -> list[Any]:
    out: list[Any] = [None] * count
    barrier = threading.Barrier(count)

    def work(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            out[i] = fn(i)
        except Exception as error:  # noqa: BLE001
            out[i] = error

    threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return out


# --- check 1: Firestore --------------------------------------------------------------------------
def check_firestore(rep: Reporter, client: Any, creds: Any, project: str, marker: str) -> None:
    from app.domain import TripWorkspace
    from app.stores import firestore_stores
    from app.stores.firestore import StoredDataError, doc_id
    from app.stores.protocols import CostEntryDelta, QuotaLimits, ReserveOutcome, SpendBudget

    prefix = f"{marker}_"
    pc = PrefixedClient(client, prefix)
    names = ("quota_usage", "workspaces", "idempotency", "cost_totals", "kill_switch")
    started = time.perf_counter()
    created_ok = True
    try:
        stores = firestore_stores(pc, lambda: datetime.now(UTC), replay_codec=DictCodec())
        now = datetime.now(UTC)
        owner = "u-" + marker
        ws_store, quota, idem, costs = stores.workspaces, stores.quota, stores.idempotency, stores.costs

        # write / read / TTL timestamp / hashed id
        ws = TripWorkspace(id="ws-main", owner_id=owner, goal="synthetic goal")
        t0 = time.perf_counter()
        ws_store.put(owner, "main", ws, now, timedelta(days=2))
        got = ws_store.get(owner, "main", now)
        raw = pc.collection("workspaces").document(doc_id(owner, "main")).get().to_dict() or {}
        ttl_ok = isinstance(raw.get("expires_at"), datetime) and raw["expires_at"] > now
        rep.add("firestore.write_read", got is not None and got.id == "ws-main",
                time.perf_counter() - t0)
        rep.add("firestore.ttl_field_is_timestamp", ttl_ok)
        ids = doc_ids(pc, "workspaces")
        hashed = bool(ids) and all(re.fullmatch(r"[0-9a-f]{48}", i) for i in ids) and not any(
            owner in i for i in ids)
        rep.add("firestore.hashed_doc_ids", hashed, doc_count=len(ids))

        # owner_id read verification: a tampered document must raise, not be returned
        pc.collection("workspaces").document(doc_id(owner, "main")).update({"owner_id": "other"})
        try:
            ws_store.get(owner, "main", now)
            tamper_ok = False
        except StoredDataError:
            tamper_ok = True
        rep.add("firestore.owner_id_verified_on_read", tamper_ok)
        pc.collection("workspaces").document(doc_id(owner, "main")).delete()
        rep.add("firestore.delete", ws_store.get(owner, "main", now) is None)

        # put_if_version compare-and-set under contention
        first = ws_store.put_if_version(owner, "cas", TripWorkspace(
            id="ws-cas", owner_id=owner, version=0), now, None, expected_version=None)
        second = ws_store.put_if_version(owner, "cas", TripWorkspace(
            id="ws-cas", owner_id=owner, version=0), now, None, expected_version=None)
        t0 = time.perf_counter()
        wins = run_threads(THREADS, lambda i: ws_store.put_if_version(
            owner, "cas", TripWorkspace(id="ws-cas", owner_id=owner, version=1), now, None,
            expected_version=0))
        errors = [w for w in wins if isinstance(w, Exception)]
        won = sum(1 for w in wins if w is True)
        final = ws_store.get(owner, "cas", now)
        rep.add("firestore.put_if_version_cas",
                first is True and second is False and won == 1 and not errors
                and final is not None and final.version == 1,
                time.perf_counter() - t0, threads=THREADS, winners=won, errors=len(errors))

        # quota reserve/commit race
        limits = QuotaLimits(turn_limit=TURN_LIMIT, trip_limit=1, window=None)
        t0 = time.perf_counter()
        outs = run_threads(THREADS, lambda i: quota.reserve(owner, f"k{i}", limits, now))
        errors = [o for o in outs if isinstance(o, Exception)]
        reserved = [i for i, o in enumerate(outs) if o == ReserveOutcome.RESERVED]
        exhausted = sum(1 for o in outs if o == ReserveOutcome.EXHAUSTED)
        commits = run_threads(len(reserved), lambda j: quota.commit(
            owner, f"k{reserved[j]}", limits, now))
        usage = quota.snapshot(owner, now, None)
        dup = quota.reserve(owner, f"k{reserved[0]}", limits, now) if reserved else None
        rep.add("firestore.quota_race_exact",
                len(reserved) == TURN_LIMIT and exhausted == THREADS - TURN_LIMIT and not errors
                and not any(isinstance(c, Exception) for c in commits)
                and usage.successful_turns == TURN_LIMIT and not usage.reserved_keys
                and dup == ReserveOutcome.DUPLICATE,
                time.perf_counter() - t0, threads=THREADS, reserved=len(reserved),
                exhausted=exhausted, committed=usage.successful_turns, errors=len(errors))

        # idempotency begin / commit / get, TTL timestamp
        idem.begin(owner, "idem-1", "fp", {"x": 1}, now)
        pending = idem.get(owner, "idem-1", now)
        idem.commit(owner, "idem-1", now)
        done = idem.get(owner, "idem-1", now)
        raw = pc.collection("idempotency").document(doc_id(owner, "idem-1")).get().to_dict() or {}
        rep.add("firestore.idempotency_roundtrip",
                pending is not None and pending.pending and done is not None
                and not done.pending and isinstance(raw.get("expires_at"), datetime))

        # cost counter and kill switch
        budget = SpendBudget(max_usd=Decimal("100"))
        costs.add_spend(CostEntryDelta(10, 5, 1234, True), budget, now)
        costs.add_spend(CostEntryDelta(10, 5, 1234, True), budget, now)
        totals = costs.totals()
        rep.add("firestore.cost_counter", totals.total_usd_micros == 2468
                and totals.total_input_tokens == 20, usd_micros=totals.total_usd_micros)
    except Exception as error:  # noqa: BLE001
        created_ok = False
        rep.add("firestore.exception", False, time.perf_counter() - started,
                error=safe_error(error))
    finally:
        removed = leftovers = 0
        try:
            for name in names + ("display_cache", "consent_ledgers"):
                removed += delete_collection(pc, name)
            leftovers = sum(count_docs(pc, n) for n in names + ("display_cache", "consent_ledgers"))
        except Exception as error:  # noqa: BLE001
            rep.add("firestore.cleanup", False, error=safe_error(error))
            leftovers = -1
        rep.add("firestore.cleanup_zero_leftovers", leftovers == 0 and created_ok,
                removed=removed, leftovers=leftovers)
    check_ttl_policies(rep, creds, project)


def check_ttl_policies(rep: Reporter, creds: Any, project: str) -> None:
    """TTL policy state through the admin API. The backend service account deliberately lacks
    the admin permission (datastore.indexes.get), so a 403 retries ONCE with the operator's own
    gcloud identity; the report says which identity answered."""
    states, via = _ttl_states(authed_session(creds), project), "service_account"
    if all(s == "http403" for s in states.values()):
        try:
            owner = make_credentials(None)
            states, via = _ttl_states(authed_session(owner), project), "operator"
        except SmokeRefused:
            via = "none"
    active = sum(1 for s in states.values() if s == "ACTIVE")
    bad = sorted({s for s in states.values() if s != "ACTIVE"})
    rep.add("firestore.ttl_policies_active", active == len(TTL_COLLECTIONS),
            active=active, expected=len(TTL_COLLECTIONS), other=",".join(bad)[:50] or "none",
            read_by=via)


def _ttl_states(session: Any, project: str) -> dict[str, str]:
    states: dict[str, str] = {}
    for coll in TTL_COLLECTIONS:
        url = (f"https://firestore.googleapis.com/v1/projects/{project}/databases/(default)/"
               f"collectionGroups/{coll}/fields/expires_at")
        try:
            resp = session.get(url, timeout=20, allow_redirects=False)
        except Exception as error:  # noqa: BLE001
            states[coll] = safe_error(error)
            continue
        if resp.status_code != 200:
            states[coll] = f"http{resp.status_code}"
            continue
        states[coll] = str(resp.json().get("ttlConfig", {}).get("state", "NONE"))
    return states


# --- check 2: Identity Toolkit -------------------------------------------------------------------
def sign_up_anonymous(api_key: str, ctx: Any) -> tuple[str, str]:
    import httpx

    with httpx.Client(verify=ctx, timeout=20, follow_redirects=False) as http:
        resp = http.post("https://identitytoolkit.googleapis.com/v1/accounts:signUp",
                         params={"key": api_key}, json={"returnSecureToken": True})
    if resp.status_code != 200:
        raise RuntimeError(f"signUp_http_{resp.status_code}")
    body = resp.json()
    return str(body["idToken"]), str(body["localId"])


def build_verifier(project: str, ctx: Any, revocation: Any) -> Any:
    import httpx

    from app.auth_firebase import SECURETOKEN_CERTS_URL, FirebaseIdentityVerifier, HttpKeyFetcher

    fetcher = HttpKeyFetcher(SECURETOKEN_CERTS_URL,
                             client=httpx.Client(verify=ctx, follow_redirects=False))
    return FirebaseIdentityVerifier(project, fetcher, revocation=revocation)


def check_identity(rep: Reporter, project: str, api_key: str, ctx: Any) -> None:
    import httpx

    from app.auth import PrincipalResolver
    from app.domain import PrincipalKind
    from app.firebase_admin_adapter import FirebaseAdminClient, FirebaseRevocationChecker

    admin = FirebaseAdminClient(project)
    uid: str | None = None
    try:
        t0 = time.perf_counter()
        token, uid = sign_up_anonymous(api_key, ctx)
        rep.add("identity.signup_anonymous", True, time.perf_counter() - t0)
        checker = FirebaseRevocationChecker(admin, ttl_seconds=0.0)
        verifier = build_verifier(project, ctx, checker)
        t0 = time.perf_counter()
        ident = verifier.verify(token)
        principal = PrincipalResolver().resolve(ident)
        rep.add("identity.verify_real_public_keys",
                ident.uid == uid and ident.sign_in_provider == "anonymous" and not ident.revoked
                and principal.kind == PrincipalKind.GUEST, time.perf_counter() - t0,
                tier=principal.kind.value if hasattr(principal.kind, "value") else "x")
        t0 = time.perf_counter()
        account = admin.get_account(uid)
        rep.add("identity.get_account", account is not None and account.uid == uid
                and not account.disabled, time.perf_counter() - t0,
                provider_ids=len(account.provider_ids) if account else -1)
        disabled, valid_since = admin.token_state(uid)
        rep.add("identity.revocation_checker_live",
                not disabled and valid_since >= 0 and not checker.is_revoked(uid, time.time()))

        # client-style access to Firestore with the user's token must be denied by the rules
        statuses = []
        with httpx.Client(verify=ctx, timeout=20, follow_redirects=False) as http:
            url = (f"https://firestore.googleapis.com/v1/projects/{project}/databases/(default)"
                   "/documents/workspaces")
            statuses.append(http.get(url, params={"pageSize": 1},
                                     headers={"Authorization": f"Bearer {token}"}).status_code)
            statuses.append(http.get(url, params={"pageSize": 1, "key": api_key}).status_code)
        rep.add("identity.rules_deny_client_reads", all(s in (401, 403) for s in statuses),
                status_with_token=statuses[0], status_key_only=statuses[1])

        t0 = time.perf_counter()
        admin.delete_user(uid)
        again_ok = True
        try:
            admin.delete_user(uid)
        except Exception:  # noqa: BLE001
            again_ok = False
        gone = admin.get_account(uid) is None
        rep.add("identity.delete_user_idempotent", again_ok and gone, time.perf_counter() - t0)
        fresh = build_verifier(project, ctx, FirebaseRevocationChecker(admin, ttl_seconds=0.0))
        after = fresh.verify(token)
        resolved = PrincipalResolver().resolve(after)
        rep.add("identity.old_token_rejected_as_revoked",
                after.revoked and resolved.kind == PrincipalKind.DISABLED)
    except Exception as error:  # noqa: BLE001
        rep.add("identity.exception", False, error=safe_error(error))
    finally:
        left = 0
        if uid is not None:
            try:
                admin.delete_user(uid)
                left = 0 if admin.get_account(uid) is None else 1
            except Exception:  # noqa: BLE001
                left = 1
        rep.add("identity.no_users_left", left == 0, leftover_users=left)


# --- check 3: Secret Manager and boot ------------------------------------------------------------
def secret_url(project: str, name: str) -> str:
    """The secrets are global resources with user-managed (regional) replication."""
    return (f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{name}"
            "/versions/latest:access")


def check_secrets(rep: Reporter, creds: Any, project: str, keys: Mapping[str, str],
                  boot_env: Mapping[str, str]) -> None:
    import base64

    session = authed_session(creds)
    pairs = {"openrouter-api-key": OPENROUTER_KEY_VAR, "google-maps-api-key": GOOGLE_MAPS_KEY_VAR}
    for name in SECRETS:
        t0 = time.perf_counter()
        try:
            resp = session.get(secret_url(project, name), timeout=20,
                               allow_redirects=False)
            if resp.status_code == 200:
                value = base64.b64decode(resp.json()["payload"]["data"]).decode("utf-8").strip()
                same = value == keys.get(pairs[name], "\0")
                rep.add(f"secrets.read.{name}", True, time.perf_counter() - t0,
                        length=len(value), matches_env=same)
            else:
                rep.add(f"secrets.read.{name}", False, time.perf_counter() - t0,
                        http=resp.status_code)
        except Exception as error:  # noqa: BLE001
            rep.add(f"secrets.read.{name}", False, error=safe_error(error))
    other = session.get(secret_url(project, "wsg-not-granted-probe"), timeout=20,
                        allow_redirects=False)
    rep.add("secrets.other_secret_denied", other.status_code == 403, http=other.status_code)
    listing = session.get(
        f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets", timeout=20, allow_redirects=False)
    rep.add("secrets.list_denied", listing.status_code == 403, http=listing.status_code)

    from fastapi.testclient import TestClient

    from app.server import create_production_app

    saved = {k: os.environ.get(k) for k in boot_env}
    t0 = time.perf_counter()
    try:
        os.environ.update(boot_env)  # process-local; restored below; never printed
        app = create_production_app()
        health = TestClient(app, raise_server_exceptions=False).get("/healthz")
        rep.add("secrets.create_production_app_boots", health.status_code == 200,
                time.perf_counter() - t0, healthz=health.status_code)
    except ConfigurationError as error:
        rep.add("secrets.create_production_app_boots", False, error="ConfigurationError",
                reason=str(error)[:50] if SAFE_VALUE.fullmatch(str(error)[:50]) else "x")
    except Exception as error:  # noqa: BLE001
        rep.add("secrets.create_production_app_boots", False, error=safe_error(error))
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


# --- check 4: one full real turn -----------------------------------------------------------------
def snapshot_ids(client: Any) -> dict[str, set[str]]:
    return {c: doc_ids(client, c) for c in ALL_COLLECTIONS}


def owner_doc_counts(client: Any, uid: str) -> dict[str, int]:
    from google.cloud.firestore_v1.base_query import FieldFilter

    out: dict[str, int] = {}
    for coll in ALL_COLLECTIONS:
        q = client.collection(coll).where(filter=FieldFilter("owner_id", "==", uid))
        out[coll] = sum(1 for _ in q.stream())
    return out


def check_turn(rep: Reporter, args: argparse.Namespace, project: str, keys: dict[str, str],
               api_key: str, ctx: Any, client: Any, mode: str) -> None:
    from fastapi.testclient import TestClient
    from google.cloud.firestore_v1.base_query import FieldFilter

    from app.composition import build_runtime, firebase_admin_from_env
    from app.stores.firestore import doc_id
    from app.stores.protocols import SpendBudget

    env = {**keys, "FIREBASE_PROJECT_ID": project, "FIRESTORE_PROJECT_ID": project,
           "FIREBASE_PROJECT_NUMBER": args.project_number, "APP_CHECK_ENFORCE": "false",
           "PILOT_SPEND_MAX_USD": "0.05"}
    admin, revocation = firebase_admin_from_env(env)
    if admin is None:
        rep.add("turn.admin_adapter_built", False)
        return
    verifier = build_verifier(project, ctx, revocation)
    model_t, discovery_t = live_transports(mode)
    before_ids = snapshot_ids(client)
    uid: str | None = None
    try:
        runtime = build_runtime(
            env, identity_verifier=verifier, firebase_admin=admin, require_app_check=True,
            model_transport=model_t, discovery_transport=discovery_t,
            firestore_project_id=project, spend_budget=SpendBudget(max_usd=Decimal("0.05")))
        keys.clear()
        env.pop(OPENROUTER_KEY_VAR, None)
        env.pop(GOOGLE_MAPS_KEY_VAR, None)
        app = TestClient(runtime.app, raise_server_exceptions=False)
        token, uid = sign_up_anonymous(api_key, ctx)
        auth = {"Authorization": f"Bearer {token}"}
        key = f"real-smoke-{uuid.uuid4().hex[:12]}"
        cap = args.max_spend_usd
        pre = ledger_totals(runtime)
        if pre["cost"] >= cap:
            rep.add("turn.cap_precheck", False, cap=str(cap))
            return
        t0 = time.perf_counter()
        resp = app.post("/v1/agent/turns", json={"message": args.message},
                        headers={**auth, "Idempotency-Key": key})
        elapsed = time.perf_counter() - t0
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        after = ledger_totals(runtime)
        cost = after["cost"] - pre["cost"]
        block = body.get("discovery") if resp.status_code == 200 else None
        places = (block or {}).get("places") or []
        rep.add("turn.first_turn_http_200", resp.status_code == 200, elapsed, http=resp.status_code,
                places=len(places), warnings=len(body.get("warnings") or []),
                err=str(body.get("code")) if resp.status_code != 200 else "none")
        rep.add("turn.model_and_maps_cost", cost >= 0 and cost < cap, cost_usd=str(cost),
                cost_cad=str(usd_to_cad(cost, args.cad_rate)), input_tokens=after["input"] - pre["input"],
                output_tokens=after["output"] - pre["output"],
                grounding_lite_calls=after["tool_calls"] - pre["tool_calls"],
                unpriced=after["unpriced"] - pre["unpriced"])
        # documents exist and carry no Maps text / coordinates
        needles = [str(p.get("title", "")) for p in places if isinstance(p, dict)]
        text = str((block or {}).get("text") or "")
        needles += [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 12][:20]
        docs = {
            "workspaces": client.collection("workspaces").where(
                filter=FieldFilter("owner_id", "==", uid)).stream(),
            "idempotency": [client.collection("idempotency").document(doc_id(uid, key)).get()],
            "quota_usage": [client.collection("quota_usage").document(doc_id(uid)).get()],
        }
        counts: dict[str, int] = {}
        leaks = {"maps_text": 0, "coordinate_pairs": 0, "coordinate_keys": 0}
        for coll, snaps in docs.items():
            n = 0
            for snap in snaps:
                if snap.exists:
                    n += 1
                    for k, v in find_leaks(snap.to_dict(), needles).items():
                        leaks[k] += v
            counts[coll] = n
        rep.add("turn.documents_exist", all(v >= 1 for v in counts.values()), **{
            f"n_{k}": v for k, v in counts.items()})
        rep.add("turn.documents_clean", sum(leaks.values()) == 0, needles=len(needles), **leaks)
        # replay: same key, same body, no second charge
        t0 = time.perf_counter()
        again = app.post("/v1/agent/turns", json={"message": args.message},
                         headers={**auth, "Idempotency-Key": key})
        replay_totals = ledger_totals(runtime)
        rep.add("turn.replay_no_second_charge",
                again.status_code == resp.status_code == 200 and again.json() == body
                and replay_totals["cost"] == after["cost"]
                and replay_totals["tool_calls"] == after["tool_calls"]
                and replay_totals["input"] == after["input"], time.perf_counter() - t0,
                http=again.status_code)
        # delete the account
        t0 = time.perf_counter()
        deleted = app.post("/v1/me:delete", headers=auth)
        receipt = deleted.json() if deleted.status_code == 200 else {}
        rep.add("turn.me_delete", deleted.status_code == 200 and receipt.get("completed") is True,
                time.perf_counter() - t0, http=deleted.status_code,
                completed=bool(receipt.get("completed")),
                login_not_removed=bool(receipt.get("login_not_removed")))
        owned = owner_doc_counts(client, uid)
        quota_left = client.collection("quota_usage").document(doc_id(uid)).get().exists
        idem_left = client.collection("idempotency").document(doc_id(uid, key)).get().exists
        rep.add("turn.no_user_data_left", sum(owned.values()) == 0 and not quota_left
                and not idem_left, owner_docs=sum(owned.values()), quota_doc=quota_left,
                idempotency_doc=idem_left)
        rep.add("turn.firebase_user_gone", admin.get_account(uid) is None)
    except Exception as error:  # noqa: BLE001
        rep.add("turn.exception", False, error=safe_error(error))
    finally:
        left_user = 0
        if uid is not None:
            try:
                admin.delete_user(uid)
                left_user = 0 if admin.get_account(uid) is None else 1
            except Exception:  # noqa: BLE001
                left_user = 1
        # Anything this run created that survived (ids are hashes, never printed): remove the
        # documents the server wrote for the throwaway user, and the shared counters only when
        # they did not exist before the run.
        extra: dict[str, int] = {}
        try:
            after_ids = snapshot_ids(client)
            for coll in ALL_COLLECTIONS:
                new = after_ids[coll] - before_ids[coll]
                if new:
                    extra[coll] = len(new)
            removed = 0
            for coll, n in extra.items():
                if coll in ("cost_totals", "kill_switch", "account_tombstones"):
                    for did in after_ids[coll] - before_ids[coll]:
                        client.collection(coll).document(did).delete()
                        removed += 1
            rep.add("turn.run_created_docs_cleaned", True,
                    new_collections=",".join(sorted(extra)) or "none", removed_shared=removed)
            final = snapshot_ids(client)
            stray = sum(len(final[c] - before_ids[c]) for c in ALL_COLLECTIONS)
            rep.add("turn.zero_leftover_documents", stray == 0, stray=stray, users_left=left_user)
        except Exception as error:  # noqa: BLE001
            rep.add("turn.cleanup", False, error=safe_error(error))


# --- main ----------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python scripts/real_smoke.py",
        description="Smoke test the REAL Google Cloud project (Firestore, Identity Toolkit, "
                    "Secret Manager) and one real turn.")
    p.add_argument("--env-file", default=None, help="dotenv with the two provider keys")
    p.add_argument("--web-env-file", default=None,
                   help="dotenv with VITE_FIREBASE_API_KEY (public web key; never printed)")
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--allow-other-project", action="store_true")
    p.add_argument("--project-number", default="568880043351")
    p.add_argument("--service-account", default=DEFAULT_SA,
                   help="impersonated for every check")
    p.add_argument("--use-owner-credentials", action="store_true",
                   help="skip impersonation (NOT the backend's identity; broader than production)")
    p.add_argument("--ssl-mode", choices=("strict", "os-store-nonstrict"), default="strict")
    p.add_argument("--max-spend-usd", type=Decimal, default=Decimal("0.02"))
    p.add_argument("--cad-rate", type=Decimal, default=Decimal("1.43"))
    p.add_argument("--message", default="quiet vegetarian dinner near the Eiffel Tower")
    p.add_argument("--skip", default=None, help=f"comma list of: {', '.join(CHECKS)}")
    p.add_argument("--json-out", default=None, help="sanitized summary path (keep out of Git)")
    return p


def main(argv: list[str] | None = None, *, emit: Callable[[str], None] | None = None,
         env: Mapping[str, str] | None = None) -> int:
    if emit is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    emit = emit or (lambda line: print(line, flush=True))
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_spend_usd <= 0 or args.max_spend_usd > Decimal("0.05"):
        parser.error("--max-spend-usd must be above 0 and at most 0.05")
    try:
        return _run(args, emit, os.environ if env is None else env)
    except (SmokeRefused, ConfigurationError) as refusal:
        emit(f"refused: {refusal}")
        return EXIT_USAGE


def _run(args: argparse.Namespace, emit: Callable[[str], None], environ: Mapping[str, str]) -> int:
    project = check_project(args.project, args.allow_other_project)
    skip = parse_skip(args.skip)
    ctx = build_ssl_context(args.ssl_mode)
    keys: dict[str, str] = {}
    api_key = ""
    names = (OPENROUTER_KEY_VAR, GOOGLE_MAPS_KEY_VAR)
    if "turn" not in skip or "secrets" not in skip:
        keys = parse_env_file(args.env_file, names) if args.env_file else {}
        for name in names:
            if not keys.get(name) and environ.get(name):
                keys[name] = environ[name]
        if not all(keys.get(n) for n in names):
            raise SmokeRefused("both provider keys are needed (--env-file or the environment)")
    if "identity" not in skip or "turn" not in skip:
        web = parse_env_file(args.web_env_file, ("VITE_FIREBASE_API_KEY",)) \
            if args.web_env_file else {}
        api_key = web.get("VITE_FIREBASE_API_KEY") or environ.get("VITE_FIREBASE_API_KEY", "")
        if not api_key:
            raise SmokeRefused("the Firebase web API key is needed (--web-env-file)")
    impersonate = None if args.use_owner_credentials else args.service_account
    creds = make_credentials(impersonate)
    restore = install_adc(creds, project)
    rep = Reporter(emit)
    marker = run_marker()
    emit(f"REAL SERVICES project={project} identity="
         f"{'owner' if impersonate is None else 'impersonated service account'} "
         f"ssl={args.ssl_mode} cap=US${args.max_spend_usd} run={marker}")
    summary: dict[str, Any] = {"project": project, "run": marker, "ssl_mode": args.ssl_mode,
                               "impersonated": impersonate is not None}
    try:
        client = None
        if "firestore" not in skip or "turn" not in skip:
            from app.stores.firestore import make_client

            client = make_client(project)
        if "firestore" not in skip:
            emit("check 1: firestore")
            check_firestore(rep, client, creds, project, marker)
        if "identity" not in skip:
            emit("check 2: identity toolkit")
            check_identity(rep, project, api_key, ctx)
        if "secrets" not in skip:
            emit("check 3: secret manager and boot")
            boot_env = {**keys, "FIREBASE_PROJECT_ID": project, "FIRESTORE_PROJECT_ID": project,
                        "PILOT_SPEND_MAX_USD": "0.05", "FIREBASE_PROJECT_NUMBER":
                        args.project_number, "APP_CHECK_ENFORCE": "false"}
            check_secrets(rep, creds, project, keys, boot_env)
        if "turn" not in skip:
            emit("check 4: full real turn")
            check_turn(rep, args, project, dict(keys), api_key, ctx, client, args.ssl_mode)
    finally:
        restore()
        keys.clear()
    passed = sum(1 for r in rep.results if r.status == "PASS")
    emit(f"done: {passed} PASS, {rep.failed} FAIL, "
         f"{sum(1 for r in rep.results if r.status == 'SKIP')} SKIP")
    summary["results"] = rep.as_json()
    summary["failed"] = rep.failed
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        emit(f"summary: {args.json_out}")
    return EXIT_FAILED if rep.failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
