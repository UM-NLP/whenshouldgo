"""Shared builders for the Phase 1 (history, saved places, ask policy) tests. Holds no tests.

``HEnv`` builds the REAL composition (``build_runtime``) over the in-memory stores or the Firestore
adapters on the in-process fake, with history switched on by config and an approved notice. Every
document is synthetic. Canary strings stand in for Maps content, labels and coordinates.
"""
from __future__ import annotations

import dataclasses
import enum
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from composition_support import build
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import composition
from app import domain as d
from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.domain import ConsentPurpose
from app.stores.fake_firestore import FakeFirestoreClient
from app.telemetry import InMemorySink

NOTICE = "history-notice-1"
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
BUDGET = composition.SpendBudget(max_tokens=10_000_000)
PLACE = "ChIJplaceA_-1"
PLACE2 = "ChIJplaceB_-2"
LABEL_CANARY = "LABELCANARY-5d1c"
NOTE_CANARY = "NOTECANARY-8e2f"
MAPS_CANARY = "MAPSCANARY-7f3a91"
# An unrounded coordinate pair: it must never be stored anywhere (a rounded one only in a saved
# place or, until confirm or expiry, the pending SAVE_PLACE record).
RAW_LAT, RAW_LON = 43.653226, -79.383184
ROUNDED = {"latitude": 43.653, "longitude": -79.383}
TOKENS = {
    "tok-g": VerifiedIdentity("guest-a", "anonymous"),
    "tok-ra": VerifiedIdentity("reg-a", "google.com"),
    "tok-rb": VerifiedIdentity("reg-b", "password"),
    "tok-off": VerifiedIdentity("off-1", "google.com"),
}
WHO = {"g": "tok-g", "a": "tok-ra", "b": "tok-rb", "off": "tok-off"}


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def deep_text(value: Any) -> str:
    """Everything inside ``value`` as text, including fields a repr hides."""
    if isinstance(value, BaseModel):
        return deep_text(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return deep_text({f.name: getattr(value, f.name) for f in dataclasses.fields(value)})
    if isinstance(value, dict):
        return " ".join(f"{deep_text(k)} {deep_text(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(deep_text(v) for v in value)
    if isinstance(value, enum.Enum):
        return str(value.value)
    return str(value)


class HEnv:
    def __init__(self, kind: str, monkeypatch: Any, *, enabled: bool = True,
                 notice: str | None = NOTICE, approve: bool = True) -> None:
        if approve:
            monkeypatch.setattr(d, "APPROVED_HISTORY_NOTICE_VERSIONS", frozenset({NOTICE}))
        self.kind = kind
        self.clock = Clock()
        self.sink = InMemorySink()
        self.fs: FakeFirestoreClient | None = None
        kwargs: dict[str, Any] = dict(
            identity_verifier=StaticTokenVerifier(TOKENS), clock=self.clock, sink=self.sink,
            history_enabled=enabled, history_notice_version=notice)
        if kind != "memory":
            self.fs = FakeFirestoreClient(self.clock)
            kwargs.update(firestore_client=self.fs, spend_budget=BUDGET,
                          allow_in_memory_state=False)
        self.runtime = build(**kwargs)
        self.app = self.runtime.app
        self.http = TestClient(self.app)

    # -- requests ---------------------------------------------------------------------------------
    @staticmethod
    def h(who: str = "a", key: str | None = None) -> dict[str, str]:
        out = {"Authorization": f"Bearer {WHO.get(who, who)}"}
        if key is not None:
            out["Idempotency-Key"] = key
        return out

    def call(self, method: str, path: str, who: str = "a", body: Any = None,
             key: str | None = None, **kw: Any):
        return self.http.request(method, path, json=body, headers=self.h(who, key), **kw)

    def consent(self, who: str = "a", purpose: ConsentPurpose = ConsentPurpose.HISTORY_STORE):
        return self.call("POST", "/v1/me/consents", who,
                         {"purpose": purpose.value, "notice_version": NOTICE})

    def grant_all(self, who: str = "a") -> None:
        for purpose in (ConsentPurpose.HISTORY_STORE, ConsentPurpose.SAVED_PLACE):
            assert self.consent(who, purpose).status_code == 200

    def workspace(self, who: str = "a") -> dict[str, Any]:
        response = self.call("GET", "/v1/workspace", who)
        assert response.status_code == 200, response.text
        return response.json()

    def outcome(self, who: str = "a", key: str = "outcome-key-1", **body: Any):
        payload = {"place_id": PLACE, "outcome": "WENT", **body}
        return self.call("POST", "/v1/outcomes", who, payload, key)

    def save_history(self, who: str = "a", key: str = "hist-key-0001", **body: Any):
        ws = self.workspace(who)
        payload = {"workspace_id": ws["id"], "plan_version": 1, "place_id": PLACE,
                   "intent_tag": "FOOD_DRINK", **body}
        return self.call("POST", "/v1/history/records", who, payload, key)

    def propose_save(self, who: str = "a", key: str = "save-key-0001", **body: Any):
        ws = self.workspace(who)
        payload = {"workspace_id": ws["id"], "plan_version": 1, "effect": "SAVE_PLACE",
                   "target": PLACE, "user_label": "Home", "label_kind": "HOME",
                   "position": {"latitude": RAW_LAT, "longitude": RAW_LON}, **body}
        return self.call("POST", "/v1/actions/propose", who, payload, key)

    def save_via_action(self, who: str = "a", key: str = "save-key-0001", **body: Any) -> dict[str, Any]:
        proposed = self.propose_save(who, key, **body)
        assert proposed.status_code == 200, proposed.text
        action = proposed.json()
        confirmed = self.call("POST", f"/v1/actions/{action['id']}/confirm", who,
                              {"nonce": action["nonce"]}, key)
        assert confirmed.status_code == 200, confirmed.text
        return confirmed.json()

    # -- state inspection -------------------------------------------------------------------------
    def stores_text(self) -> str:
        """Every durable document of every store, with hidden fields."""
        if self.fs is not None:
            return " ".join(f"{path} {deep_text(data)}" for path, data in self.fs.documents())
        rt = self.runtime
        parts = [
            deep_text(rt.stores.quota.usage), deep_text(rt.stores.consents._ledger),
            deep_text(rt.stores.workspaces.items), deep_text(rt.stores.idempotency.items),
            deep_text(rt.plan_stores.plans.items), deep_text(rt.plan_stores.outcomes.items),
            deep_text(rt.plan_stores.receipts.items),
            deep_text(rt.action_service._store.items),
            deep_text(rt.history_service.store.items),
            deep_text(rt.saved_place_service.places.items),
            deep_text(rt.saved_place_service.asks.items),
        ]
        return " ".join(parts)

    def history_text(self) -> str:
        """Only the history and saved-place documents."""
        if self.fs is not None:
            return " ".join(
                f"{path} {deep_text(data)}" for path, data in self.fs.documents()
                if path.split("/")[0] in ("history_records", "history_settings", "saved_places",
                                          "saved_place_asks"))
        rt = self.runtime
        return " ".join((deep_text(rt.history_service.store.items),
                         deep_text(rt.saved_place_service.places.items),
                         deep_text(rt.saved_place_service.asks.items)))

    def state_marker(self) -> str:
        return self.stores_text()

    def events_text(self) -> str:
        return " ".join(repr(e) + " " + e.model_dump_json() for e in self.sink.events)


def events_of(env: HEnv, cls: type) -> list[Any]:
    return [e for e in env.sink.events if isinstance(e, cls)]
