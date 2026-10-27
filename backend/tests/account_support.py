"""Shared builders for the account tests (link, merge, export, delete). Holds no tests.

Everything is synthetic: no network, no Firebase, no keys. ``Env`` builds the same app over the
in-memory stores or the Firestore adapters on the in-process fake.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

from fs_backend import client_for, txn_for
from typing import Any

from fastapi.testclient import TestClient
from maps_simulator import NOW

from app.account import AccountService, AdminAccount, build_account_service
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.consent import InMemoryConsentStore
from app.domain import (
    Confirmation,
    ConsentPurpose,
    Evidence,
    EvidenceKind,
    Fact,
    FactSource,
    Principal,
    PrincipalKind,
    TripWorkspace,
)
from app.entitlements import Allowance, EntitlementPolicy, Period, PolicyRegistry
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService
from app.stores import StateStores, firestore_stores, memory_stores
from app.stores.fake_firestore import FakeFirestoreClient
from app.telemetry import InMemorySink

POLICY_VERSION = "account-test-1"
MAPS_CANARY = "MAPSCANARY-9b2e41"
TOKENS = {
    "tok-g1": VerifiedIdentity("g1", "anonymous"),
    "tok-g2": VerifiedIdentity("g2", "anonymous"),
    "tok-g1-linked": VerifiedIdentity("g1", "google.com"),  # g1's refreshed token after linking
    "tok-g1-stale": VerifiedIdentity("g1", "anonymous"),
    "tok-r1": VerifiedIdentity("r1", "google.com"),
    "tok-r2": VerifiedIdentity("r2", "password"),
    "tok-off": VerifiedIdentity("off-1", "google.com"),
}
H = {name: {"Authorization": f"Bearer tok-{name}"} for name in
     ("g1", "g2", "g1-linked", "r1", "r2", "off")}


class Clock:
    def __init__(self) -> None:
        self.now = NOW
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeAdmin:
    """The ``FirebaseAdmin`` protocol, in memory."""

    def __init__(self) -> None:
        self.accounts: dict[str, AdminAccount] = {
            "g1": AdminAccount("g1", frozenset()),
            "g2": AdminAccount("g2", frozenset()),
            "r1": AdminAccount("r1", frozenset({"google.com"})),
            "r2": AdminAccount("r2", frozenset({"password"})),
        }
        self.deleted: list[str] = []
        self.fail_delete = False

    def link(self, uid: str, provider: str = "google.com") -> None:
        self.accounts[uid] = AdminAccount(uid, frozenset({provider}))

    def get_account(self, uid: str) -> AdminAccount | None:
        return self.accounts.get(uid)

    def delete_user(self, uid: str) -> None:
        if self.fail_delete:
            raise RuntimeError("admin down")
        self.deleted.append(uid)
        self.accounts.pop(uid, None)


def registry(guest_turns: int = 50, guest_trips: int = 10, reg_turns: int = 100,
             reg_trips: int = 10) -> PolicyRegistry:
    policy = EntitlementPolicy(
        version=POLICY_VERSION, effective_at=NOW, grace_rule="unknown-version-uses-current",
        allowances={
            PrincipalKind.GUEST: Allowance(guest_turns, guest_trips, Period.LIFETIME),
            PrincipalKind.REGISTERED_FREE: Allowance(reg_turns, reg_trips, Period.ROLLING_30_DAYS),
        })
    return PolicyRegistry((policy,), POLICY_VERSION)


def guest(uid: str = "g1") -> Principal:
    return Principal(uid, PrincipalKind.GUEST, POLICY_VERSION)


def registered(uid: str = "r1") -> Principal:
    return Principal(uid, PrincipalKind.REGISTERED_FREE, POLICY_VERSION)


def user_fact(fid: str, value: str, source: FactSource = FactSource.USER_EXPLICIT) -> Fact:
    return Fact(id=fid, key="note", value=value, source=source,
                confirmation=Confirmation.CONFIRMED, hard=False, scope="trip")


def workspace(owner: str, goal: str = "a quiet cafe near the park", *,
              facts: list[Fact] | None = None, with_maps: bool = False) -> TripWorkspace:
    evidence = []
    items = list(facts if facts is not None else [user_fact("f1", "no loud places")])
    if with_maps:
        items.append(Fact(
            id="f-tool", key="hours", value=f"open late {MAPS_CANARY}", source=FactSource.TOOL,
            confirmation=Confirmation.UNCONFIRMED, hard=False, scope="trip",
            evidence_ids=("ev1",)))
        evidence.append(Evidence(
            id="ev1", tool="search_places", tool_version="1", source="synthetic",
            retrieved_at=NOW, expires_at=NOW + timedelta(hours=1), precision="place",
            kind=EvidenceKind.GROUNDED_ANSWER, content_class="MAPS_GROUNDED", llm_visible=False,
            storage_class="DISPLAY_CACHE", attribution_required=True,
            cache_expires_at=NOW + timedelta(hours=24), tool_call_id="tc1"))
    return TripWorkspace(id="ws-main", owner_id=owner, goal=goal, facts=items,
                         evidence=evidence)


class Env:
    def __init__(self, kind: str, *, admin: bool = True, with_service: bool = True,
                 guest_turns: int = 50, history_notice: bool = False, **limits: int) -> None:
        self.kind = kind
        self.clock = Clock()
        self.client: FakeFirestoreClient | None = None
        if kind == "memory":
            self.stores: StateStores = memory_stores(self.clock)
        else:
            self.client = client_for(kind, self.clock)
            self.stores = firestore_stores(
                self.client, self.clock, transactional=txn_for(self.client))
        self.policies = registry(guest_turns=guest_turns)
        self.quotas = QuotaService(self.policies, store=self.stores.quota, clock=self.clock)
        self.consents = InMemoryConsentStore(self.clock, ledger=self.stores.consents)
        self.sink = InMemorySink()
        self.admin = FakeAdmin()
        orchestrator = TurnOrchestrator(
            self.quotas, dev_scaffold=True, workspace_store=self.stores.workspaces,
            idempotency_store=self.stores.idempotency, clock=self.clock)
        self.app = create_app(
            verifier=StaticTokenVerifier(TOKENS),
            resolver=PrincipalResolver(policies=self.policies, disabled_uids={"off-1"}),
            orchestrator=orchestrator, trace_sink=self.sink,
            consent_store=self.consents)
        self.service: AccountService | None = None
        if with_service:
            self.service = build_account_service(
                stores=self.stores, quotas=self.quotas, consents=self.consents,
                firebase_client=self.client, firebase_admin=self.admin if admin else None,
                clock=self.clock, sink=self.sink)
            self.app.state.account_service = self.service
        self.http = TestClient(self.app)

    # -- helpers
    def seed(self, uid: str, *, turns: int = 0, goal: str = "a quiet cafe near the park",
             with_maps: bool = False, consent: bool = False) -> None:
        principal = guest(uid) if uid.startswith("g") else registered(uid)
        self.quotas.ensure_record(principal)
        for i in range(turns):
            self.quotas.commit_turn(principal, f"{uid}-seed-{i}")
        self.stores.workspaces.put(uid, "default", workspace(uid, goal, with_maps=with_maps),
                                   self.clock(), None)
        if consent:
            self.consents.grant(uid, ConsentPurpose.LOCATION_FOREGROUND, "location-notice-1")

    def start_intent(self, who: str = "g1", provider: str = "GOOGLE", **headers: str) -> dict[str, Any]:
        response = self.http.post("/v1/account/link-intents", json={"provider": provider},
                                  headers={**H[who], **headers})
        assert response.status_code == 200, response.text
        return response.json()

    def complete(self, intent: dict[str, Any], who: str = "g1-linked", nonce: str | None = None,
                 intent_id: str | None = None):
        return self.http.post(
            f"/v1/account/link-intents/{intent_id or intent['id']}:complete",
            json={"nonce": nonce or intent["nonce"]}, headers=H[who])

    def collide(self, uid: str = "g1", who: str = "r1") -> dict[str, Any]:
        """Run the collision: g1's intent is presented by the existing account ``who``."""
        intent = self.start_intent("g1" if uid == "g1" else "g2")
        response = self.complete(intent, who=who)
        assert response.status_code == 409 and response.json().get("reason") == "ACCOUNT_COLLISION"
        return intent

    def start_migration(self, intent: dict[str, Any], who: str = "r1", **headers: str):
        return self.http.post("/v1/account-migrations",
                              json={"intent_id": intent["id"], "nonce": intent["nonce"]},
                              headers={**H[who], **headers})

    def run_migration(self, migration_id: str, who: str = "r1", **body: Any):
        return self.http.post(f"/v1/account-migrations/{migration_id}:run", json=body,
                              headers=H[who])

    def documents(self) -> list[tuple[str, Any]]:
        """Every durable document or object held for any store, as (label, data)."""
        rows: list[tuple[str, Any]] = []
        if self.client is not None:
            rows.extend(self.client.documents())
        else:
            s = self.stores
            rows.extend(("quota", (uid, repr(u))) for uid, u in s.quota.usage.items())  # type: ignore[attr-defined]
            rows.extend(("consents", (uid, repr(r)))
                        for uid, r in s.consents._ledger.items())  # type: ignore[attr-defined]
            rows.extend(("workspaces", (k, repr(v)))
                        for k, v in s.workspaces.items.items())  # type: ignore[attr-defined]
            rows.extend(("idempotency", (k, repr(v)))
                        for k, v in s.idempotency.items.items())  # type: ignore[attr-defined]
            rows.extend(("display_cache", repr(s.display_cache._entries))  # type: ignore[attr-defined]
                        for _ in [0] if getattr(s.display_cache, "_entries", None))
            assert self.service is not None
            rows.extend(("account", repr(d)) for d in self.service.store.all_documents())
        return rows

    def dump(self) -> str:
        return json.dumps([(label, repr(data)) for label, data in self.documents()])

    def account_dump(self) -> str:
        """Only the account store's own documents (intents, journals, tombstones, keys)."""
        assert self.service is not None
        if self.client is not None:
            return json.dumps([(path, repr(data)) for path, data in self.client.documents()
                               if path.split("/")[0].startswith("account")])
        return json.dumps([repr(d) for d in self.service.store.all_documents()])
