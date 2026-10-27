"""Item 10: export, cascade delete, tombstones, the gated user and tenant isolation.

Over the in-memory stores and the Firestore adapters on the in-process fake.
"""
from __future__ import annotations

import json
import re

import pytest
from fs_backend import MEMORY_FAKE_EMULATOR
from account_support import H, MAPS_CANARY, Env, guest, user_fact, workspace
from maps_simulator import wrap_unvalidated as W
from test_discovery_contracts import make_answer

from app import domain as d
from app import telemetry as t
from app.account import AccountError, ExportPart, ExportWorkspace
from app.composition import pinned_runtime_config
from app.domain import ConsentPurpose

COORDINATE = re.compile(r"-?\d{1,3}\.\d{3,}\s*[,;]\s*-?\d{1,3}\.\d{3,}")
TURN = {"message": "Plan a quiet cafe visit this evening"}


@pytest.fixture(params=MEMORY_FAKE_EMULATOR)
def env(request: pytest.FixtureRequest) -> Env:
    e = Env(request.param)
    e.seed("g1", turns=3, with_maps=True, consent=True)
    e.seed("g2", turns=2, goal="g2 private goal", consent=True)
    e.stores.display_cache.put("g1", "k1", W(make_answer(), "g1"))
    e.stores.display_cache.put("g2", "k1", W(make_answer(), "g2"))
    return e


def export(env: Env, who: str = "g1"):
    return env.http.post("/v1/me:export", headers=H[who])


# --- export --------------------------------------------------------------------------------------------
def test_export_holds_the_users_own_data(env: Env) -> None:
    body = export(env).json()
    assert body["kind"] == "export_bundle" and body["tier"] == "guest"
    assert body["usage"]["successful_turns"] == 3
    [workspace_out] = body["workspaces"]
    assert workspace_out["goal"] == "a quiet cafe near the park"
    assert [f["value"] for f in workspace_out["facts"]] == ["no loud places"]
    assert [c["purpose"] for c in body["consents"]] == ["LOCATION_FOREGROUND"]
    assert body["saved_places"] == [] and body["outings"] == [] and body["history_active"] is False


def test_export_content_scan_no_maps_content_tokens_keys_or_coordinates(env: Env) -> None:
    env.http.post("/v1/agent/turns", json=TURN, headers={**H["g1"], "Idempotency-Key": "idem-0001"})
    raw = export(env).text
    assert MAPS_CANARY not in raw  # a provider-derived fact is not exported
    assert '"evidence"' not in raw and "ev1" not in raw and '"k1"' not in raw  # no evidence, no cache
    assert "tok-" not in raw and "idem-0001" not in raw and "committed_keys" not in raw
    assert not COORDINATE.search(raw)
    answer = make_answer()
    assert answer.text[:40] not in raw  # the display cache (grounded text) is excluded
    json.loads(raw)  # a valid single document


def test_export_never_includes_another_tenant(env: Env) -> None:
    raw = export(env, "g1").text
    assert "g2 private goal" not in raw and '"g2"' not in raw
    other = export(env, "g2").json()
    assert other["workspaces"][0]["goal"] == "g2 private goal"
    assert other["usage"]["successful_turns"] == 2


def test_export_includes_merged_workspaces_beside_the_default() -> None:
    e = Env("memory")
    e.seed("g1", goal="guest goal")
    e.seed("r1", goal="registered goal")
    migration = e.start_migration(e.collide()).json()
    e.run_migration(migration["id"], item_ids=["workspace-default"])
    goals = sorted(w["goal"] for w in export(e, "r1").json()["workspaces"])
    assert goals == ["guest goal", "registered goal"]


def test_history_data_is_exported_only_while_history_is_active(env: Env, monkeypatch) -> None:
    saved = d.SavedPlace(place_id="ChIJabc", user_label="Home", label_kind=d.SavedPlaceKind.HOME,
                         position=d.RoundedPosition(latitude=43.6532, longitude=-79.3832),
                         created_at=env.clock())
    env.service.register_export(  # type: ignore[union-attr]
        "saved_places", lambda uid: ExportPart(saved_places=(saved,)), requires_history=True)
    env.service.register_export(  # type: ignore[union-attr]
        "board", lambda uid: ExportPart(workspaces=(ExportWorkspace(
            id="board-1", status=d.WorkspaceStatus.CAPTURED, timezone="UTC", goal="from a plan store"),)))
    off = export(env).json()
    assert off["saved_places"] == [] and [w["id"] for w in off["workspaces"]][-1] == "board-1"
    monkeypatch.setattr(d, "APPROVED_HISTORY_NOTICE_VERSIONS", frozenset({"history-notice-1"}))
    config = d.evolve(pinned_runtime_config(), history_enabled=True,
                      history_notice_version="history-notice-1")
    env.service._history_gate = lambda: d.history_active(config)  # type: ignore[union-attr]  # noqa: SLF001
    on = export(env).json()
    assert on["history_active"] is True
    assert [p["place_id"] for p in on["saved_places"]] == ["ChIJabc"]
    assert on["saved_places"][0]["position"] == {"latitude": 43.653, "longitude": -79.383}  # rounded


def test_a_contributor_cannot_return_another_tenants_outing(env: Env, monkeypatch) -> None:
    class Outing:
        owner_id = "somebody-else"

    env.service.register_export("bad", lambda uid: ExportPart.model_construct(  # type: ignore[union-attr]
        workspaces=(), plan_versions=(), outcomes=(), saved_places=(), outings=(Outing(),)))
    with pytest.raises(ValueError):
        env.service.export(guest("g1"))  # type: ignore[union-attr]


# --- gated user ---------------------------------------------------------------------------------------------
def test_export_and_delete_work_while_gated_and_consume_nothing() -> None:
    e = Env("memory", guest_turns=3)
    e.seed("g1", turns=3)
    usage = e.http.get("/v1/me/usage", headers=H["g1"]).json()
    assert usage["meter_state"] == "GATED"
    caps = e.http.get("/v1/me/capabilities", headers=H["g1"]).json()
    assert caps["can_start_turn"] is False and caps["can_export"] and caps["can_delete"]
    blocked = e.http.post("/v1/agent/turns", json=TURN, headers={**H["g1"], "Idempotency-Key": "idem-1"})
    assert blocked.json()["allowance_exhausted"] is True
    before = e.quotas.usage_for("g1")
    assert export(e).status_code == 200
    assert (e.quotas.usage_for("g1").successful_turns, e.quotas.usage_for("g1").reserved_keys) == (
        before.successful_turns, before.reserved_keys)
    receipt = e.http.post("/v1/me:delete", headers=H["g1"])
    assert receipt.status_code == 200 and receipt.json()["completed"] is True
    assert e.quotas.usage_for("g1").successful_turns == 0  # deleted, nothing was consumed to do it


# --- delete cascade ----------------------------------------------------------------------------------------------
def populate(env: Env) -> None:
    for who in ("g1", "g2"):
        env.http.post("/v1/agent/turns", json=TURN, headers={**H[who], "Idempotency-Key": f"idem-{who}"})
    intent = env.start_intent("g1")  # an account record's intent and its key
    env.http.post("/v1/account/link-intents", json={"provider": "GOOGLE"},
                  headers={**H["g1"], "Idempotency-Key": "link-key-1"})
    assert intent


def test_delete_cascade_leaves_nothing_of_the_user_in_any_store(env: Env) -> None:
    populate(env)
    assert "g1" in env.dump()  # sanity: the scan can see the data
    receipt = env.http.post("/v1/me:delete", headers=H["g1"])
    assert receipt.status_code == 200 and receipt.json()["completed"] is True
    assert receipt.json()["kind"] == "delete_receipt"
    left = [label for label, data in env.documents() if "g1" in repr(data) and label != "account"]
    assert left == []
    assert env.stores.workspaces.get("g1", "default", env.clock()) is None
    assert env.consents.ledger("g1") == ()
    assert env.stores.display_cache.get("g1", "k1") is None
    assert env.quotas.usage_for("g1").successful_turns == 0
    assert "g1" in env.admin.deleted
    # the other tenant is untouched
    assert env.stores.workspaces.get("g2", "default", env.clock()) is not None
    assert env.stores.display_cache.get("g2", "k1") is not None
    assert env.quotas.usage_for("g2").successful_turns >= 2
    assert len(env.consents.ledger("g2")) == 1


def test_a_tombstone_holds_no_personal_data_and_blocks_recreation(env: Env) -> None:
    env.http.post("/v1/me:delete", headers=H["g1"])
    tombstone = env.service.store.get_tombstone("g1")  # type: ignore[union-attr]
    assert tombstone is not None and tombstone.completed_at is not None
    assert len(tombstone.uid_hash) == 64 and "g1" not in tombstone.uid_hash
    docs = [repr(data) for label, data in env.documents() if "tombstone" in repr(data).lower()
            or label.startswith("account_tombstones")]
    assert docs and all("g1" not in doc.replace(tombstone.uid_hash, "") for doc in docs)
    # a late request with a still-valid token cannot recreate the user
    for call in (lambda: env.http.post("/v1/session:bootstrap", headers=H["g1"]),
                 lambda: env.http.get("/v1/me/usage", headers=H["g1"]),
                 lambda: env.http.post("/v1/agent/turns", json=TURN,
                                       headers={**H["g1"], "Idempotency-Key": "idem-late"}),
                 lambda: export(env)):
        assert call().status_code == 403
    assert env.quotas.usage_for("g1").successful_turns == 0
    assert env.stores.workspaces.get("g1", "default", env.clock()) is None
    # a late job asks the service before writing
    with pytest.raises(AccountError):
        env.service.assert_active("g1")  # type: ignore[union-attr]
    env.service.assert_active("g2")  # type: ignore[union-attr]


def test_delete_is_idempotent(env: Env) -> None:
    first = env.http.post("/v1/me:delete", headers=H["g1"]).json()
    again = env.http.post("/v1/me:delete", headers=H["g1"])
    assert again.status_code == 200 and again.json() == first
    assert env.admin.deleted.count("g1") == 1


def test_a_failing_deleter_is_reported_retried_and_never_stops_the_others(env: Env) -> None:
    registry = env.service.deleters  # type: ignore[union-attr]
    state = {"fail": True, "calls": 0}

    def late_store(uid: str) -> int:
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("store down")
        return 1

    registry.register("plans", late_store)  # a store registers without editing account.py
    with pytest.raises(ValueError):
        registry.register("plans", late_store)  # never silently replaced
    first = env.http.post("/v1/me:delete", headers=H["g1"]).json()
    assert first["completed"] is False
    assert env.stores.workspaces.get("g1", "default", env.clock()) is None  # others still ran
    assert env.service.store.get_tombstone("g1").completed_at is None  # type: ignore[union-attr]
    assert env.http.get("/v1/me/usage", headers=H["g1"]).status_code == 403  # already closed
    state["fail"] = False
    retry = env.http.post("/v1/me:delete", headers=H["g1"]).json()  # the retry is allowed through
    assert retry["completed"] is True and retry["requested_at"] == first["requested_at"]
    assert state["calls"] == 2
    assert env.http.post("/v1/me:delete", headers=H["g1"]).json()["completed"] is True


def test_a_failing_firebase_deletion_leaves_the_receipt_incomplete(env: Env) -> None:
    env.admin.fail_delete = True
    assert env.http.post("/v1/me:delete", headers=H["g1"]).json()["completed"] is False
    env.admin.fail_delete = False
    assert env.http.post("/v1/me:delete", headers=H["g1"]).json()["completed"] is True
    assert "g1" in env.admin.deleted


def test_deleting_one_account_never_touches_another(env: Env) -> None:
    before = {label: repr(data) for label, data in env.documents() if "g2" in repr(data)}
    env.http.post("/v1/me:delete", headers=H["g1"])
    after = {label: repr(data) for label, data in env.documents() if "g2" in repr(data)}
    assert before.keys() == after.keys()


def test_a_registered_user_with_links_and_migrations_is_fully_deleted() -> None:
    e = Env("fake-firestore")
    e.seed("g1")
    e.seed("r1", turns=2)
    migration = e.start_migration(e.collide()).json()
    e.run_migration(migration["id"], item_ids=["workspace-default"])
    assert e.http.post("/v1/me:delete", headers=H["r1"]).json()["completed"] is True
    leftovers = [path for path, data in e.client.documents() if "r1" in repr(data)]  # type: ignore[union-attr]
    assert leftovers == []


# --- routes, limiter, telemetry, contract ------------------------------------------------------------------
def test_export_and_delete_have_their_own_limiters(env: Env) -> None:
    codes = [export(env).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    assert export(env).json()["reason"] == "RATE_LIMITED"
    assert env.http.get("/v1/me/usage", headers=H["g1"]).status_code == 200  # access limiter apart
    assert env.http.post("/v1/me:delete", headers=H["g2"]).status_code == 200  # another uid


def test_requests_need_auth_and_refuse_disabled(env: Env) -> None:
    assert env.http.post("/v1/me:export").status_code == 401
    assert env.http.post("/v1/me:delete").status_code == 401
    assert export(env, "off").status_code == 403
    assert env.http.post("/v1/me:delete", headers=H["off"]).status_code == 403


def test_telemetry_carries_enums_only(env: Env) -> None:
    export(env)
    env.http.post("/v1/me:delete", headers=H["g1"])
    events = [e for e in env.sink.events
              if isinstance(e, (t.ExportRequested, t.DeleteRequested))]
    assert [type(e).__name__ for e in events] == ["ExportRequested", "DeleteRequested"]
    assert all(e.principal_kind == d.PrincipalKind.GUEST for e in events)
    blob = " ".join(e.model_dump_json() for e in env.sink.events)
    assert "g1" not in blob and "quiet" not in blob


def test_every_stored_document_carries_a_schema_version_and_kind() -> None:
    e = Env("fake-firestore")
    for uid in ("g1", "g2", "r1"):
        e.seed(uid)
    intent = e.start_intent("g1", **{"Idempotency-Key": "key-0001"})
    e.admin.link("g1")
    assert e.complete(intent).status_code == 200
    migration = e.start_migration(e.collide("g2", "r1")).json()
    e.run_migration(migration["id"], item_ids=["workspace-default"])
    e.http.post("/v1/me:delete", headers=H["r2"])
    docs = [(path, data) for path, data in e.client.documents()  # type: ignore[union-attr]
            if path.split("/")[0].startswith("account")]
    assert docs
    for path, data in docs:
        assert data["schema_version"] == 1 and isinstance(data["kind"], str), path


def test_the_export_route_declares_the_promoted_export_bundle() -> None:
    from app.main import create_app
    schema = create_app().openapi()
    ok = schema["paths"]["/v1/me:export"]["post"]["responses"]["200"]["content"][
        "application/json"]["schema"]["$ref"]
    assert ok.endswith("/ExportBundle")
    props = schema["components"]["schemas"]["ExportBundle"]["properties"]
    assert {"consents", "saved_places", "outings", "workspaces", "usage", "outcomes"} <= set(props)
    assert "coordinates" not in json.dumps(props) and user_fact("a", "b") and workspace("x")
    assert ConsentPurpose.LOCATION_FOREGROUND
