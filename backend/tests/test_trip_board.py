"""Item 7: Trip Board server (workspace read, user edit, plan versions, outcome check-in).

Store-backed tests run over BOTH the in-memory stores and the Firestore adapters over the fake.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import domain as d
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import (
    Confirmation,
    FactSource,
    OutcomeCheckin,
    OutcomeValue,
    PlanDestination,
    PlanVersion,
    ReplacedReason,
    TripWorkspace,
    WorkspaceEditRequest,
)
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.patching import PatchRejected, StaleEdit, apply_patch, apply_user_edit
from app.quota import QuotaService
from app.stores import firestore_stores, memory_stores
from app.stores.fake_firestore import FakeFirestoreClient, fake_transactional
from app.stores.plans_store import (
    PlanConflict,
    firestore_plan_stores,
    memory_plan_stores,
)
from app.telemetry import InMemorySink
from app.workspaces import (
    EditRejected,
    IdempotencyKeyReused,
    NoteRejected,
    WorkspaceError,
    build_workspace_service,
)

T0 = datetime.fromisoformat("2026-10-26T12:00:00+00:00")
PLACE_A, PLACE_B = "ChIJplaceAAAA1", "ChIJplaceBBBB2"
TOKENS = {
    "tok-a": VerifiedIdentity("guest-a", "anonymous"),
    "tok-b": VerifiedIdentity("guest-b", "anonymous"),
    "tok-reg": VerifiedIdentity("reg-1", "google.com"),
    "tok-off": VerifiedIdentity("off-1", "google.com"),
}
HA, HB, HREG = ({"Authorization": f"Bearer {t}"} for t in ("tok-a", "tok-b", "tok-reg"))
NOTE_CANARY = "CANARYNOTE-zebra-quartz-77"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Consents:
    def __init__(self, held: bool) -> None:
        self.held = held

    def has_active(self, owner_id: str, purpose: d.ConsentPurpose) -> bool:
        return self.held and purpose == d.ConsentPurpose.HISTORY_STORE


@dataclass
class Env:
    kind: str
    clock: Clock
    client: TestClient
    service: Any
    sink: InMemorySink
    stores: Any
    workspaces: Any
    fs: FakeFirestoreClient | None
    consents: Consents
    orchestrator: TurnOrchestrator

    def edit(self, body: dict, key: str = "key-00000001", headers=HA):
        return self.client.patch("/v1/workspace", json=body, headers={**headers, "Idempotency-Key": key})

    def facts(self, headers=HA) -> dict[str, dict]:
        return {f["key"]: f for f in self.client.get("/v1/workspace", headers=headers).json()["facts"]}


def build(kind: str, monkeypatch: pytest.MonkeyPatch, *, history: bool = False,
          consent: bool = False) -> Env:
    clock, sink = Clock(), InMemorySink()
    fs = None
    if kind == "firestore":
        fs = FakeFirestoreClient(clock)
        state = firestore_stores(fs, clock, transactional=fake_transactional)
        plan_stores = firestore_plan_stores(fs, transactional=fake_transactional)
    else:
        state = memory_stores(clock)
        plan_stores = memory_plan_stores()
    config = None
    if history:
        monkeypatch.setattr(d, "APPROVED_HISTORY_NOTICE_VERSIONS", frozenset({"history-notice-1"}))
        from app.composition import pinned_runtime_config
        config = d.evolve(pinned_runtime_config(), history_enabled=True,
                          history_notice_version="history-notice-1")
    quotas = QuotaService()
    orchestrator = TurnOrchestrator(quotas, dev_scaffold=True, workspace_store=state.workspaces,
                                    clock=clock, runtime_config=config)
    app = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=quotas.policies, disabled_uids={"off-1"}),
        orchestrator=orchestrator)
    consents = Consents(consent)
    service = build_workspace_service(
        state.workspaces, plan_stores, policies=quotas.policies, consents=consents,
        runtime_config=config, clock=clock, sink=sink, lock=orchestrator._workspace_lock)  # noqa: SLF001
    app.state.workspace_service = service
    return Env(kind, clock, TestClient(app), service, sink, plan_stores, state.workspaces, fs,
               consents, orchestrator)


@pytest.fixture(params=["memory", "firestore"])
def env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Env:
    return build(request.param, monkeypatch)


def fact(key: str, value: object = "v", hard: bool = False, scope: str = "trip") -> dict:
    return {"key": key, "value": value, "hard": hard, "scope": scope}


def body(version: int, upserts=(), removals=()) -> dict:
    return {"base_version": version, "upsert_facts": list(upserts), "remove_fact_ids": list(removals)}


def docs(env: Env) -> list[tuple[str, dict]]:
    return env.fs.documents() if env.fs else []


# --- read ---------------------------------------------------------------------------------------
def test_get_creates_a_stable_empty_workspace(env: Env) -> None:
    first = env.client.get("/v1/workspace", headers=HA)
    second = env.client.get("/v1/workspace", headers=HA)
    assert first.status_code == 200
    j = first.json()
    assert j["kind"] == "workspace_view" and j["version"] == 0 and j["facts"] == []
    assert j["status"] == "CAPTURED" and j["id"] == second.json()["id"]
    assert "owner_id" not in j and "evidence" not in j


def test_auth_disabled_and_missing_service(env: Env) -> None:
    assert env.client.get("/v1/workspace").status_code == 401
    assert env.client.get("/v1/workspace", headers={"Authorization": "Bearer tok-off"}).status_code == 403
    env.client.app.state.workspace_service = None
    for r in (env.client.get("/v1/workspace", headers=HA), env.edit(body(0, [fact("a")])),
              env.client.post("/v1/outcomes", json={"place_id": PLACE_A, "outcome": "WENT"},
                              headers=HA)):
        assert r.status_code == 503 and r.json()["reason"] == "SERVER_NOT_CONFIGURED"


def test_view_shows_a_turn_workspace(env: Env) -> None:
    turn = env.client.post("/v1/agent/turns", json={"message": "a picnic in a park this afternoon"},
                           headers={**HA, "Idempotency-Key": "turn-000001"})
    assert turn.status_code == 200, turn.text
    view = env.client.get("/v1/workspace", headers=HA).json()
    assert view["id"] == turn.json()["workspace_id"] and view["version"] == 1


# --- edit ---------------------------------------------------------------------------------------
def test_user_edits_are_confirmed_user_facts(env: Env) -> None:
    r = env.edit(body(0, [fact("budget", "under 20", hard=True), fact("vibe", "quiet")]))
    assert r.status_code == 200, r.text
    facts = {f["key"]: f for f in r.json()["facts"]}
    assert r.json()["version"] == 1
    for f in facts.values():
        assert f["source"] == "USER_EXPLICIT" and f["confirmation"] == "CONFIRMED"
    assert facts["budget"]["hard"] is True and facts["vibe"]["hard"] is False
    r2 = env.edit(body(1, [fact("vibe", "lively")], ["budget"]), key="key-00000002")
    facts = {f["key"]: f for f in r2.json()["facts"]}
    assert set(facts) == {"vibe"} and facts["vibe"]["value"] == "lively" and r2.json()["version"] == 2


@pytest.mark.parametrize("payload", [
    {"base_version": 0, "upsert_facts": [], "remove_fact_ids": [], "uid": "x"},
    {"base_version": 0, "upsert_facts": [{"key": "a", "owner_id": "x"}], "remove_fact_ids": []},
    {"base_version": 0, "upsert_facts": [{"key": "a", "source": "INFERENCE"}], "remove_fact_ids": []},
    {"base_version": 0, "upsert_facts": [{"key": "a", "confirmation": "UNCONFIRMED"}], "remove_fact_ids": []},
    {"base_version": -1, "upsert_facts": [], "remove_fact_ids": []},
    {"base_version": 0, "upsert_facts": [{"key": "bad key!"}], "remove_fact_ids": []},
])
def test_malformed_requests_are_422_and_write_nothing(env: Env, payload: dict) -> None:
    assert env.edit(payload).status_code == 422
    assert env.client.get("/v1/workspace", headers=HA).json()["version"] == 0


def test_idempotency_key_is_required(env: Env) -> None:
    r = env.client.patch("/v1/workspace", json=body(0, [fact("a")]), headers=HA)
    assert r.status_code == 422


@pytest.mark.parametrize("upserts,removals", [
    ([fact("planning_area", "elsewhere")], []),                       # reserved key
    ([fact("Planning_Area", "elsewhere")], []),                       # case-folded
    ([fact("a", "x" * 201)], []),                                     # value cap
    ([fact("a", "<b>hi</b>")], []),                                   # markup
    ([fact("a", "meet at 123 Queen Street West")], []),               # address
    ([fact("a", "43.6532,-79.3832")], []),                            # coordinates
    ([fact("a", "see https://example.com")], []),                     # link
    ([fact("a", "x‮y")], []),                                    # bidi control
    ([fact("a", "ok", scope="<i>")], []),                             # scope text
    ([fact("a", None, hard=True)], []),                               # hard without a value
    ([fact("a"), fact("A")], []),                                     # duplicate key
    ([fact("a")], ["a"]),                                             # upsert and remove
    ([], ["nope"]),                                                   # remove unknown
    ([fact(f"k{i}") for i in range(21)], []),                         # count cap
    ([], []),                                                         # empty edit
    ([fact("selected_place_id", "https://x.test/p")], []),            # selection must be an id
    ([fact("selected_place_id", PLACE_A, hard=True)], []),            # selection is never hard
    ([fact("selected_place_label", "12 Main Street")], []),           # label that is an address
])
def test_strict_validation_rejects_the_whole_edit(env: Env, upserts, removals) -> None:
    r = env.edit(body(0, upserts, removals))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "VALIDATION"
    assert env.client.get("/v1/workspace", headers=HA).json()["version"] == 0  # nothing written


def test_non_finite_numbers_are_rejected_at_the_service(env: Env) -> None:
    ws = TripWorkspace(id="w1", owner_id="guest-a")
    req = WorkspaceEditRequest(base_version=0, upsert_facts=(d.FactEdit(key="a", value=float("nan")),))
    with pytest.raises(PatchRejected):
        apply_user_edit(ws, req)


def test_fact_cap(env: Env) -> None:
    version = 0
    for batch in range(5):
        r = env.edit(body(version, [fact(f"k{batch}-{i}") for i in range(10)]), key=f"key-batch-{batch}")
        assert r.status_code == 200
        version = r.json()["version"]
    over = env.edit(body(version, [fact("one-too-many")]), key="key-overflow")
    assert over.status_code == 422


def test_conflict_is_typed_with_the_current_version(env: Env) -> None:
    assert env.edit(body(0, [fact("a")])).status_code == 200
    r = env.edit(body(0, [fact("b")]), key="key-00000002")
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT" and "current version 1" in r.json()["message"]
    assert set(env.facts()) == {"a"}


def test_edit_is_idempotent_by_key(env: Env) -> None:
    first = env.edit(body(0, [fact("a", "1")]))
    again = env.edit(body(0, [fact("a", "1")]))
    assert first.status_code == again.status_code == 200
    assert again.json()["version"] == 1 and again.json() == first.json()
    env.edit(body(1, [fact("b", "2")]), key="key-00000002")
    replay = env.edit(body(0, [fact("a", "1")]))  # old key after a newer edit: no re-apply
    assert replay.status_code == 200 and replay.json()["version"] == 2
    assert set(env.facts()) == {"a", "b"}
    reused = env.edit(body(2, [fact("c", "3")]))  # same key, different request
    assert reused.status_code == 409 and reused.json()["code"] == "CONFLICT"
    assert "c" not in env.facts()


def test_concurrent_edits_with_one_base_version_apply_once(env: Env) -> None:
    results: list[int] = []
    guard = threading.Lock()

    def go(i: int) -> None:
        r = env.edit(body(0, [fact(f"f{i}")]), key=f"key-thread-{i:03d}")
        with guard:
            results.append(r.status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1 and results.count(409) == 5
    assert env.client.get("/v1/workspace", headers=HA).json()["version"] == 1


def test_terminal_workspace_cannot_be_edited(env: Env) -> None:
    ws = env.client.get("/v1/workspace", headers=HA).json()
    stored = env.workspaces.get("guest-a", "default", env.clock())
    env.workspaces.put("guest-a", "default", d.evolve(stored, status=d.WorkspaceStatus.COMPLETED),
                       env.clock(), None)
    assert ws["id"] == stored.id
    assert env.edit(body(0, [fact("a")])).status_code == 422


# --- model proposals never override the user ----------------------------------------------------
def _model_fact(key: str, **kw: Any) -> d.Fact:
    return d.Fact(id=key, key=key, value=kw.get("value", "model"), source=FactSource.INFERENCE,
                  confirmation=Confirmation.UNCONFIRMED, hard=False, scope="trip")


def _patch(ws: TripWorkspace, upserts=(), removals=()) -> d.WorkspacePatch:
    return d.WorkspacePatch(workspace_id=ws.id, base_version=ws.version, next_action=d.NextAction.ASK,
                            upsert_facts=list(upserts), remove_fact_ids=list(removals))


def test_model_proposal_never_overrides_or_removes_a_user_fact(env: Env) -> None:
    env.edit(body(0, [fact("budget", "under 20", hard=True), fact("vibe", "quiet")]))
    ws = env.workspaces.get("guest-a", "default", env.clock())
    for upserts, removals in (([_model_fact("budget")], []), ([_model_fact("Vibe")], []),
                              ([], ["budget"]), ([], ["vibe"])):
        with pytest.raises(PatchRejected):
            apply_patch(ws, _patch(ws, upserts, removals))
    # a brand-new model fact is an unconfirmed soft assumption and does not touch the user's
    out = apply_patch(ws, _patch(ws, [_model_fact("pace")]))
    by_key = {f.key: f for f in out.facts}
    assert by_key["pace"].confirmation == Confirmation.UNCONFIRMED and not by_key["pace"].hard
    assert by_key["budget"].source == FactSource.USER_EXPLICIT and by_key["budget"].hard


def test_a_model_fact_can_never_be_hard_or_confirmed(env: Env) -> None:
    ws = TripWorkspace(id="w1", owner_id="guest-a")
    with pytest.raises(Exception):
        d.Fact(id="x", key="x", value="v", source=FactSource.INFERENCE,
               confirmation=Confirmation.CONFIRMED, hard=True, scope="trip")
    for hard, confirmation in ((True, Confirmation.UNCONFIRMED), (False, Confirmation.CONFIRMED)):
        proposal = d.Fact(id="x", key="x", value="v", source=FactSource.INFERENCE,
                          confirmation=confirmation, hard=hard, scope="trip")
        with pytest.raises(PatchRejected):
            apply_patch(ws, _patch(ws, [proposal]))


def test_user_edit_confirms_a_model_assumption_and_then_it_is_protected(env: Env) -> None:
    ws = TripWorkspace(id="w2", owner_id="guest-a")
    ws = apply_patch(ws, _patch(ws, [_model_fact("pace", value="slow")]))
    out = apply_user_edit(ws, WorkspaceEditRequest(
        base_version=ws.version, upsert_facts=(d.FactEdit(key="Pace", value="fast", hard=True),))).workspace
    (f,) = out.facts
    assert f.id == "pace" and f.value == "fast" and f.source == FactSource.USER_EXPLICIT
    assert f.confirmation == Confirmation.CONFIRMED and f.hard
    with pytest.raises(PatchRejected):
        apply_patch(out, _patch(out, [_model_fact("pace")]))


def test_reserved_and_tool_facts_cannot_be_touched_by_the_user() -> None:
    ws = TripWorkspace(id="w3", owner_id="u", facts=[
        d.Fact(id="planning_area", key="planning_area", value="x", source=FactSource.USER_EXPLICIT,
               confirmation=Confirmation.CONFIRMED, hard=False, scope="trip"),
        d.Fact(id="t1", key="weather", value="rain", source=FactSource.TOOL,
               confirmation=Confirmation.CONFIRMED, hard=False, scope="trip")])
    for upserts, removals in (((), ("planning_area",)), ((), ("t1",)),
                              ((d.FactEdit(key="weather", value="sun"),), ())):
        with pytest.raises(PatchRejected):
            apply_user_edit(ws, WorkspaceEditRequest(base_version=0, upsert_facts=upserts,
                                                     remove_fact_ids=removals))
    with pytest.raises(StaleEdit):
        apply_user_edit(ws, WorkspaceEditRequest(base_version=3, remove_fact_ids=("t1",)))


# --- tenant isolation ---------------------------------------------------------------------------
def test_tenant_isolation_matrix(env: Env) -> None:
    env.edit(body(0, [fact("secret", "a-only")]))
    sel = env.edit(body(1, [fact("selected_place_id", PLACE_A)]), key="key-00000002")
    assert sel.status_code == 200
    a_id = sel.json()["id"]
    b_view = env.client.get("/v1/workspace", headers=HB).json()
    assert b_view["id"] != a_id and b_view["facts"] == [] and b_view["version"] == 0
    # B cannot edit A's state: B's own base version 0 applies to B's own workspace only
    r = env.edit(body(0, [fact("b-fact", "b")]), key="key-00000002", headers=HB)  # same key, other tenant
    assert r.status_code == 200 and r.json()["id"] == b_id_of(env)
    assert "secret" not in env.facts(HB) and "b-fact" not in env.facts(HA)
    a_after = env.client.get("/v1/workspace", headers=HA).json()
    assert a_after["id"] == a_id and a_after["version"] == 2
    # plan versions are per tenant; the service never takes a tenant from the caller's input
    assert env.service.plan_version(_principal(env, "guest-a"), 1) is not None
    assert env.service.plan_version(_principal(env, "guest-b"), 1) is None
    # stored documents carry their tenant and never another's data
    for _name, doc in docs(env):
        if doc.get("kind") in {"plan_version_record", "plan_head", "edit_receipt"}:
            assert doc["owner_id"] in {"guest-a", "guest-b"}


def b_id_of(env: Env) -> str:
    return env.client.get("/v1/workspace", headers=HB).json()["id"]


def _principal(env: Env, uid: str):
    resolver = env.client.app.state.resolver
    kind = "google.com" if uid.startswith("reg") else "anonymous"
    return resolver.resolve(VerifiedIdentity(uid, kind))


def test_plan_store_rejects_another_tenants_document(env: Env) -> None:
    if env.fs is None:
        pytest.skip("firestore adapter only")
    env.edit(body(0, [fact("selected_place_id", PLACE_A)]))
    ws = env.client.get("/v1/workspace", headers=HA).json()
    # B asking for A's workspace id through the store finds nothing (digest ids include the tenant)
    assert env.stores.plans.get("guest-b", ws["id"], 1, env.clock()) is None
    assert env.stores.plans.latest("guest-b", ws["id"], env.clock()) is None
    assert env.stores.plans.history("guest-b", ws["id"], env.clock()) == ()


# --- plan versions ------------------------------------------------------------------------------
def test_selecting_a_candidate_creates_immutable_versions(env: Env) -> None:
    env.edit(body(0, [fact("budget", "under 20", hard=True)]))
    r1 = env.edit(body(1, [fact("selected_place_id", PLACE_A), fact("selected_place_label", "The Cafe")]),
                  key="key-00000002")
    assert r1.status_code == 200 and r1.json()["status"] == "PROPOSED"
    pa = _principal(env, "guest-a")
    v1 = env.service.plan_version(pa, 1)
    assert v1.version == 1 and v1.parent_version is None and v1.replaced_reason is None
    assert v1.destination == PlanDestination(place_id=PLACE_A, user_label="The Cafe")
    assert v1.user_constraint_fact_ids == ("budget",)
    assert d.PlanWarning.HARD_NEED_UNVERIFIED in v1.warnings
    r2 = env.edit(body(2, [fact("selected_place_id", PLACE_B)]), key="key-00000003")
    assert r2.status_code == 200
    v2, v1_again = env.service.plan_version(pa, 2), env.service.plan_version(pa, 1)
    assert v2.parent_version == 1 and v2.replaced_reason == ReplacedReason.USER_EDIT
    assert v2.destination.place_id == PLACE_B
    assert v1_again == v1  # the replaced version is unchanged and still readable
    with pytest.raises(Exception):
        v1.version = 9  # type: ignore[misc]
    ws = env.workspaces.get("guest-a", "default", env.clock())
    assert ws.active_plan_version == 2 and ws.status == d.WorkspaceStatus.PROPOSED
    snap = env.service.read(pa)
    assert snap.plan_versions == (1, 2) and snap.latest_plan == v2


def test_changing_a_fact_the_plan_depends_on_makes_a_new_version(env: Env) -> None:
    env.edit(body(0, [fact("selected_place_id", PLACE_A)]))
    env.edit(body(1, [fact("vibe", "quiet")]), key="key-00000002")  # soft user fact: no new version
    pa = _principal(env, "guest-a")
    assert env.service.read(pa).plan_versions == (1,)
    env.edit(body(2, [fact("budget", "under 20", hard=True)]), key="key-00000003")
    snap = env.service.read(pa)
    assert snap.plan_versions == (1, 2)
    assert snap.latest_plan.replaced_reason == ReplacedReason.CONSTRAINT_CHANGED
    assert snap.latest_plan.destination.place_id == PLACE_A
    env.edit(body(3, [fact("budget", "under 30", hard=True)]), key="key-00000004")
    assert env.service.read(pa).plan_versions == (1, 2, 3)


def test_a_new_version_drops_evidence_and_stale_pending_actions(env: Env) -> None:
    env.edit(body(0, [fact("selected_place_id", PLACE_A)]))
    pa = _principal(env, "guest-a")
    ws = env.workspaces.get("guest-a", "default", env.clock())
    action = d.PendingAction(
        id="act-1", workspace_id=ws.id, plan_version=1, effect=d.PendingActionEffect.SAVE_PLACE,
        target=PLACE_A, preview="Save this place", nonce="n" * 20, idempotency_key="idem-0001",
        created_at=env.clock(), expires_at=env.clock() + timedelta(minutes=5))
    env.workspaces.put("guest-a", "default", d.evolve(ws, pending_actions=[action]), env.clock(), None)
    assert env.client.get("/v1/workspace", headers=HA).json()["pending_actions"][0]["id"] == "act-1"
    env.edit(body(1, [fact("selected_place_id", PLACE_B)]), key="key-00000002")
    assert env.client.get("/v1/workspace", headers=HA).json()["pending_actions"] == []
    assert env.service.read(pa).latest_plan.version == 2


def test_create_plan_version_directly_and_lineage(env: Env) -> None:
    pa = _principal(env, "guest-a")
    ws = env.service.read(pa).workspace
    first = env.service.create_plan_version(pa, ws, destination=PlanDestination(place_id=PLACE_A))
    second = env.service.create_plan_version(
        pa, ws, destination=PlanDestination(place_id=PLACE_B), reason=ReplacedReason.EVIDENCE_STALE,
        grounded_evidence_id="ev-1", warnings=(d.PlanWarning.EVIDENCE_STALE,))
    assert (first.version, first.parent_version) == (1, None)
    assert (second.version, second.parent_version, second.replaced_reason) == (
        2, 1, ReplacedReason.EVIDENCE_STALE)
    assert second.grounded_evidence_id == "ev-1"
    skip = PlanVersion(id="x", workspace_id=ws.id, version=5, parent_version=2,
                       destination=PlanDestination(place_id=PLACE_A), origin_ref="o" * 12,
                       replaced_reason=ReplacedReason.USER_EDIT, created_at=env.clock())
    with pytest.raises(PlanConflict):
        env.stores.plans.append("guest-a", skip, env.clock(), None)
    with pytest.raises(WorkspaceError):
        other = d.evolve(ws, owner_id="someone-else")
        env.service.create_plan_version(pa, other, destination=PlanDestination(place_id=PLACE_A))


def test_plan_version_rejects_maps_shaped_content() -> None:
    for label in ("123 Queen Street West", "43.6532, -79.3832", "M5V 2T6"):
        with pytest.raises(Exception):
            PlanDestination(place_id=PLACE_A, user_label=label)
    assert not (set(PlanVersion.model_fields) & {"name", "address", "hours", "rating", "url", "lat", "lng"})


# --- no Maps content anywhere --------------------------------------------------------------------
MAPS_CANARIES = ("Cafe Example", "open until 9pm", "123 Queen Street West", "4.6 stars",
                 "https://maps.example/cafe", "43.6532")


def test_maps_content_never_reaches_responses_or_stored_documents(env: Env) -> None:
    ev = d.Evidence(
        id="ev-1", tool="discovery", tool_version="1", source="Grounded: Cafe Example open until 9pm",
        retrieved_at=env.clock(), expires_at=env.clock() + timedelta(hours=1),
        precision="grounded", kind=d.EvidenceKind.GROUNDED_ANSWER,
        content_class=d.ContentClass.MAPS_GROUNDED, llm_visible=False,
        storage_class=d.EvidenceStorageClass.DISPLAY_CACHE, attribution_required=True,
        cache_expires_at=env.clock() + timedelta(hours=24), tool_call_id="tc-1")
    snapshot = env.service.read(_principal(env, "guest-a"))
    env.workspaces.put("guest-a", "default", d.evolve(snapshot.workspace, evidence=[ev]),
                       env.clock(), None)
    bodies = [env.client.get("/v1/workspace", headers=HA).text]
    for i, payload in enumerate((
            body(0, [fact("selected_place_id", PLACE_A), fact("selected_place_label", "My cafe")]),)):
        bodies.append(env.edit(payload, key=f"key-canary-{i:03d}").text)
    bodies.append(env.client.get("/v1/workspace", headers=HA).text)
    for text in bodies:
        assert not any(c in text for c in MAPS_CANARIES)
    stored_ws = env.workspaces.get("guest-a", "default", env.clock())
    assert stored_ws.evidence == []  # a destination change invalidated the grounded evidence
    for name, doc in docs(env):
        if not str(name).startswith("workspaces"):  # the workspace document is the only one that may hold evidence ids
            assert not any(c in json.dumps(doc, default=str) for c in MAPS_CANARIES)
    plans = env.stores.plans.history("guest-a", stored_ws.id, env.clock())
    assert not any(c in plan.model_dump_json() for plan in plans for c in MAPS_CANARIES)


# --- outcomes -----------------------------------------------------------------------------------
def outcome(env: Env, headers, note: str | None = None, place: str = PLACE_A, outcome="WENT",
            key: str | None = None):
    payload = {"place_id": place, "outcome": outcome}
    if note is not None:
        payload["note"] = note
    hdr = {**headers, **({"Idempotency-Key": key} if key else {})}
    return env.client.post("/v1/outcomes", json=payload, headers=hdr)


@pytest.fixture(params=["memory", "firestore"])
def reg_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Env:
    return build(request.param, monkeypatch, history=True, consent=True)


def test_guest_outcome_stores_nothing_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    for kind in ("memory", "firestore"):
        env = build(kind, monkeypatch, history=True, consent=True)
        r = outcome(env, HA, note="lovely")
        assert r.status_code == 200 and r.json() == {"schema_version": 1, "kind": "outcome_ack",
                                                     "recorded": False}
        assert env.stores.outcomes.entries("guest-a", env.clock()) == ()
        assert not [n for n, _ in docs(env) if n == "outcome_ledgers"]


def test_registered_outcome_is_stored_idempotently(reg_env: Env) -> None:
    env = reg_env
    r = outcome(env, HREG, note="great", key="outcome-key-1")
    assert r.json()["recorded"] is True
    assert outcome(env, HREG, note="great", key="outcome-key-1").json()["recorded"] is True
    (entry,) = env.stores.outcomes.entries("reg-1", env.clock())
    assert (entry.place_id, entry.outcome, entry.note) == (PLACE_A, OutcomeValue.WENT, "great")
    outcome(env, HREG, place=PLACE_B, outcome="DID_NOT_GO", key="outcome-key-2")
    assert len(env.stores.outcomes.entries("reg-1", env.clock())) == 2
    # without a key the same content is one entry
    outcome(env, HREG, place=PLACE_B, outcome="CHANGED_PLAN")
    outcome(env, HREG, place=PLACE_B, outcome="CHANGED_PLAN")
    assert len(env.stores.outcomes.entries("reg-1", env.clock())) == 3
    assert env.stores.outcomes.entries("guest-a", env.clock()) == ()


def test_registered_outcome_is_dark_without_history_or_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    for history, consent in ((False, True), (True, False), (False, False)):
        env = build("memory", monkeypatch, history=history, consent=consent)
        assert outcome(env, HREG).json()["recorded"] is False
        assert env.stores.outcomes.entries("reg-1", env.clock()) == ()


def test_outcome_validation(reg_env: Env) -> None:
    env = reg_env
    for payload in ({"place_id": "bad id!", "outcome": "WENT"}, {"place_id": PLACE_A, "outcome": "MAYBE"},
                    {"place_id": PLACE_A, "outcome": "WENT", "note": "x" * 281},
                    {"place_id": PLACE_A, "outcome": "WENT", "uid": "other"},
                    {"place_id": PLACE_A, "outcome": "WENT", "extra": 1}):
        assert env.client.post("/v1/outcomes", json=payload, headers=HREG).status_code == 422
    for note in ("ate at 123 Queen Street West", "see https://x.example", "call me a@b.co", "<b>x</b>",
                 "x‮y"):
        r = outcome(env, HREG, note=note)
        assert r.status_code == 422 and r.json()["code"] == "VALIDATION"
        assert note not in r.text
    assert outcome(env, HA, note="see https://x.example").status_code == 422
    assert env.stores.outcomes.entries("reg-1", env.clock()) == ()


def test_outcome_retention_prunes_old_entries(reg_env: Env) -> None:
    env = reg_env
    outcome(env, HREG, key="outcome-old-1")
    env.clock.advance(days=181)
    outcome(env, HREG, place=PLACE_B, key="outcome-new-1")
    (entry,) = env.stores.outcomes.entries("reg-1", env.clock())
    assert entry.place_id == PLACE_B


def test_the_note_never_reaches_logs_or_telemetry(reg_env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env = reg_env
    caplog.set_level(logging.DEBUG)
    for headers in (HREG, HA):
        assert outcome(env, headers, note=NOTE_CANARY).status_code == 200
    assert outcome(env, HREG, note="ate at 123 Queen Street West " + NOTE_CANARY).status_code == 422
    events = [e for e in env.sink.events if e.event.value == "OUTCOME_CHECKIN"]
    assert len(events) == 2 and all(e.has_note for e in events)
    assert NOTE_CANARY not in "".join(e.model_dump_json() for e in env.sink.events)
    assert NOTE_CANARY not in caplog.text
    assert NOTE_CANARY not in repr(OutcomeCheckin(place_id=PLACE_A, outcome=OutcomeValue.WENT,
                                                  note=NOTE_CANARY))
    ledgers = [doc for name, doc in docs(env) if not str(name).startswith("outcome_ledgers")]
    assert NOTE_CANARY not in json.dumps(ledgers, default=str)  # only the ledger holds the note


# --- guest expiry -------------------------------------------------------------------------------
def test_guest_workspace_expires_after_seven_inactive_days(env: Env) -> None:
    env.edit(body(0, [fact("selected_place_id", PLACE_A), fact("keep", "me")]))
    first = env.client.get("/v1/workspace", headers=HA).json()
    env.clock.advance(days=6, hours=23)
    assert env.client.get("/v1/workspace", headers=HA).json()["id"] == first["id"]
    # a read does not extend the guest's window; a write does
    env.edit(body(first["version"], [fact("again", "x")]), key="key-00000002")
    env.clock.advance(days=6, hours=23)
    assert env.client.get("/v1/workspace", headers=HA).json()["id"] == first["id"]
    env.clock.advance(days=1)
    fresh = env.client.get("/v1/workspace", headers=HA).json()
    assert fresh["id"] != first["id"] and fresh["version"] == 0 and fresh["facts"] == []
    assert env.service.plan_version(_principal(env, "guest-a"), 1) is None
    if env.fs is not None:
        env.fs.run_ttl_sweep()
        assert not [n for n, doc in docs(env) if doc.get("owner_id") == "guest-a"
                    and n in {"plan_versions", "plan_heads"}]


def test_registered_workspace_does_not_expire(env: Env) -> None:
    first = env.client.get("/v1/workspace", headers=HREG).json()
    env.clock.advance(days=60)
    assert env.client.get("/v1/workspace", headers=HREG).json()["id"] == first["id"]


# --- telemetry ----------------------------------------------------------------------------------
def test_edit_telemetry_is_enums_and_counts_only(env: Env) -> None:
    env.edit(body(0, [fact("a", "SECRETVALUE"), fact("b")], []))
    env.edit(body(0, [fact("c")]), key="key-00000002")  # conflict
    env.edit(body(1, [fact("planning_area", "SECRETVALUE")]), key="key-00000003")  # rejected
    events = [e for e in env.sink.events if e.event.value == "WORKSPACE_EDIT"]
    assert [(e.upserts, e.removals, e.outcome.value) for e in events] == [
        (2, 0, "APPLIED"), (1, 0, "CONFLICT"), (1, 0, "REJECTED")]
    assert "SECRETVALUE" not in "".join(e.model_dump_json() for e in env.sink.events)


# --- rate limit ---------------------------------------------------------------------------------
def test_routes_are_rate_limited(env: Env) -> None:
    statuses = [env.client.get("/v1/workspace", headers=HA).status_code for _ in range(125)]
    assert statuses[0] == 200 and statuses[-1] == 429
    assert env.client.get("/v1/workspace", headers=HA).json()["reason"] == "RATE_LIMITED"
    assert env.client.get("/v1/workspace", headers=HB).status_code == 200  # per principal


# --- shared state with the orchestrator ---------------------------------------------------------
def test_an_edit_is_visible_to_the_next_turn(env: Env) -> None:
    t1 = env.client.post("/v1/agent/turns", json={"message": "a walk in a park"},
                         headers={**HA, "Idempotency-Key": "turn-000001"})
    assert t1.status_code == 200
    view = env.client.get("/v1/workspace", headers=HA).json()
    r = env.edit(body(view["version"], [fact("kids", "two", hard=True)]))
    assert r.status_code == 200 and r.json()["id"] == view["id"]
    stored = env.orchestrator._workspace_store.get("guest-a", "default", env.clock())  # noqa: SLF001
    assert any(f.key == "kids" and f.hard for f in stored.facts)
