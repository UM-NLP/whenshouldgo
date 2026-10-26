"""Item 12.1 and 12.5: trip history, built DARK (decision 25), over the real composition.

Parametrised over the in-memory stores and the Firestore adapters on the in-process fake.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from history_support import (
    LABEL_CANARY,
    MAPS_CANARY,
    NOTE_CANARY,
    PLACE,
    PLACE2,
    HEnv,
    events_of,
)

from app import domain as d
from app import telemetry as t
from app.domain import ConsentPurpose

COORD = re.compile(r"-?\d{1,3}\.\d{3,}")
HIST = ConsentPurpose.HISTORY_STORE


@pytest.fixture(params=["memory", "fake-firestore"])
def env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> HEnv:
    return HEnv(request.param, monkeypatch)


@pytest.fixture
def ready(env: HEnv) -> HEnv:
    """A registered user with the HISTORY_STORE consent."""
    assert env.consent("a", HIST).status_code == 200
    return env


# --- the dark flag ------------------------------------------------------------------------------
ROUTES = [
    ("GET", "/v1/history/records", None),
    ("POST", "/v1/history/records",
     {"workspace_id": "ws1", "plan_version": 1, "place_id": PLACE, "intent_tag": "OTHER"}),
    ("DELETE", "/v1/history/records", None),
    ("PATCH", "/v1/history/records/out_abc", {"user_label": "x"}),
    ("DELETE", "/v1/history/records/out_abc", None),
    ("GET", "/v1/history/settings", None),
    ("PUT", "/v1/history/settings", {"retention_days": 60}),
    ("GET", "/v1/saved-places", None),
    ("PUT", "/v1/saved-places", {"place_id": PLACE, "user_label": "Home", "label_kind": "HOME",
                                 "position": {"latitude": 1.0, "longitude": 2.0}}),
    ("DELETE", "/v1/saved-places", None),
    ("POST", "/v1/saved-places/ask-check", {"message": "near home"}),
    ("PUT", "/v1/saved-places/ask-state", {"label_kind": "HOME", "decision": "DECLINED"}),
]
IDS = [f"{m} {p}" for m, p, _ in ROUTES]


@pytest.mark.parametrize("method,path,body", ROUTES, ids=IDS)
@pytest.mark.parametrize("who", ["a", "g"])
def test_flag_off_every_route_is_404_disabled_and_writes_nothing(
    method: str, path: str, body, who: str, request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for kind in ("memory", "fake-firestore"):
        env = HEnv(kind, monkeypatch, enabled=False)
        before = env.state_marker()
        for payload in (body, {"junk": 1}):  # DISABLED wins even over body validation
            response = env.call(method, path, who, payload, key="idem-key-0001")
            assert response.status_code == 404, response.text
            assert response.json() == {"schema_version": 1, "kind": "error", "code": "VALIDATION",
                                       "message": "feature disabled", "reason": "DISABLED"}
        assert env.state_marker() == before  # the write-count assertion: nothing written
        if env.fs is not None:
            assert env.fs.documents() == []


@pytest.mark.parametrize("method,path,body", ROUTES, ids=IDS)
def test_flag_on_without_an_approved_notice_fails_closed(
    method: str, path: str, body, monkeypatch: pytest.MonkeyPatch
) -> None:
    for kwargs in ({"approve": False}, {"notice": None}):
        env = HEnv("memory", monkeypatch, **kwargs)
        response = env.call(method, path, "a", body, key="idem-key-0001")
        assert response.status_code == 404 and response.json()["reason"] == "DISABLED"
        assert env.consent("a", HIST).status_code == 404  # nothing is even grantable


def test_flag_off_outcomes_write_no_history_and_no_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    env = HEnv("fake-firestore", monkeypatch, enabled=False)
    before = env.state_marker()
    response = env.outcome("a")
    assert response.status_code == 200 and response.json()["recorded"] is False
    assert env.state_marker() == before


def test_flag_off_the_service_itself_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    env = HEnv("memory", monkeypatch, enabled=False)
    from app.feature_errors import FeatureError, FeatureFailure

    principal = d.Principal("reg-a", d.PrincipalKind.REGISTERED_FREE, "v")
    service = env.runtime.history_service
    with pytest.raises(FeatureError) as err:
        service.list(principal)
    assert err.value.failure == FeatureFailure.DISABLED
    assert service.record_outcome(principal, d.OutcomeCheckin(place_id=PLACE, outcome="WENT"),
                                  "k-12345678") is False
    assert env.state_marker() == HEnv("memory", monkeypatch, enabled=False).state_marker()


# --- who may use it ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,body", [r for r in ROUTES if "history" in r[1]],
                         ids=[IDS[i] for i, r in enumerate(ROUTES) if "history" in r[1]])
def test_a_guest_gets_a_typed_403(env: HEnv, method: str, path: str, body) -> None:
    response = env.call(method, path, "g", body, key="idem-key-0001")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "POLICY" and response.json()["reason"] == "GUEST_NOT_ALLOWED"
    assert env.history_text().strip() == ""


def test_a_guest_cannot_even_hold_the_consent_data_here(env: HEnv) -> None:
    # decision 19: the guest's history stays on the device. A guest outcome stores nothing.
    # Review finding 13: the consent itself is refused for a guest, and nothing is written.
    refused = env.consent("g", HIST)
    assert refused.status_code == 403 and refused.json()["reason"] == "GUEST_NOT_ALLOWED"
    response = env.outcome("g")
    assert response.json()["recorded"] is False  # and no durable record either
    assert env.history_text().strip() == ""


def test_without_the_consent_it_is_a_typed_403(env: HEnv) -> None:
    for method, path, body in (("GET", "/v1/history/records", None),
                               ("GET", "/v1/history/settings", None),
                               ("PUT", "/v1/history/settings", {"retention_days": 60})):
        response = env.call(method, path, "a", body)
        assert response.status_code == 403 and response.json()["reason"] == "CONSENT_REQUIRED"
    response = env.save_history("a")
    assert response.status_code == 403 and response.json()["reason"] == "CONSENT_REQUIRED"
    assert env.outcome("a").json()["recorded"] is False
    assert env.history_text().strip() == ""


def test_a_disabled_principal_is_refused(ready: HEnv) -> None:
    assert ready.call("GET", "/v1/history/records", "off").status_code == 403


def test_identity_is_never_read_from_the_body(ready: HEnv) -> None:
    ws = ready.workspace()
    body = {"workspace_id": ws["id"], "plan_version": 1, "place_id": PLACE, "intent_tag": "OTHER"}
    for extra in ({"owner_id": "reg-b"}, {"uid": "reg-b"}, {"consent_id": "x"},
                  {"retention_expires_at": "2030-01-01T00:00:00Z"}):
        response = ready.call("POST", "/v1/history/records", "a", {**body, **extra}, "key-12345678")
        assert response.status_code == 422


# --- explicit save ------------------------------------------------------------------------------
def test_explicit_save_writes_an_opt_in_record(ready: HEnv) -> None:
    response = ready.save_history("a", user_label=LABEL_CANARY)
    assert response.status_code == 200, response.text
    rec = response.json()
    assert rec["owner_id"] == "reg-a" and rec["place_ref"]["place_id"] == PLACE
    assert rec["user_label"] == LABEL_CANARY and rec["intent_tag"] == "FOOD_DRINK"
    assert rec["strength"] == "HANDOFF" and [e["type"] for e in rec["events"]] == ["SELECTED"]
    expires = ready.clock.now + timedelta(days=90)  # the default retention (decision 27)
    assert rec["retention_expires_at"].startswith(expires.strftime("%Y-%m-%dT%H:%M:%S"))
    consent = [c for c in ready.app.state.consents.active("reg-a") if c.purpose == HIST][0]
    assert rec["consent_id"] == consent.id
    listed = ready.call("GET", "/v1/history/records", "a").json()
    assert listed["kind"] == "history_record_list" and [r["id"] for r in listed["records"]] == [rec["id"]]
    assert [e.strength for e in events_of(ready, t.HistoryRecordWritten)] == [d.OutingStrength.HANDOFF]


def test_explicit_save_is_idempotent_by_key(ready: HEnv) -> None:
    first = ready.save_history("a", key="hist-key-0001").json()
    again = ready.save_history("a", key="hist-key-0001").json()
    assert again == first
    assert len(ready.call("GET", "/v1/history/records", "a").json()["records"]) == 1
    assert len(events_of(ready, t.HistoryRecordWritten)) == 1
    other = ready.save_history("a", key="hist-key-0002")
    assert other.json()["id"] != first["id"]
    clash = ready.save_history("a", key="hist-key-0001", place_id=PLACE2)
    assert clash.status_code == 409 and clash.json()["code"] == "CONFLICT"


def test_explicit_save_needs_the_callers_own_workspace_and_a_plain_label(ready: HEnv) -> None:
    body = {"workspace_id": "someone-elses-ws", "plan_version": 1, "place_id": PLACE,
            "intent_tag": "OTHER"}
    ready.workspace()
    assert ready.call("POST", "/v1/history/records", "a", body, "key-12345678").status_code == 404
    for label in ("12 Queen Street", "43.6532, -79.3831", "see https://example.com"):
        response = ready.save_history("a", key=f"k-{abs(hash(label))}-xx", user_label=label)
        assert response.status_code == 422, label
    assert ready.save_history("a", key="k-plan-0002", plan_version=7).status_code in (404, 422)
    assert ready.history_text().strip() == ""


def test_the_record_holds_no_maps_content_or_coordinates(ready: HEnv) -> None:
    ready.save_history("a", user_label=LABEL_CANARY)
    ready.outcome("a", note=NOTE_CANARY)
    text = ready.history_text()
    assert not COORD.search(text), "no coordinate in any history document"
    for forbidden in ("display_name", "formatted_address", "rating", "opening_hours", "review",
                      "latitude", "longitude", "photo", "website", "http", MAPS_CANARY):
        assert forbidden not in text
    body = ready.call("GET", "/v1/history/records", "a").text
    for forbidden in ("display_name", "address", "rating", "latitude", "http"):
        assert forbidden not in body


# --- outcomes ----------------------------------------------------------------------------------
def test_a_confirmed_outcome_with_consent_writes_a_record(ready: HEnv) -> None:
    response = ready.outcome("a", user_label=LABEL_CANARY, intent_tag="OUTDOOR", note=NOTE_CANARY)
    assert response.status_code == 200 and response.json() == {
        "schema_version": 1, "kind": "outcome_ack", "recorded": True}
    [rec] = ready.call("GET", "/v1/history/records", "a").json()["records"]
    assert rec["place_ref"]["place_id"] == PLACE and rec["user_label"] == LABEL_CANARY
    assert rec["intent_tag"] == "OUTDOOR" and rec["strength"] == "SELF_REPORTED_VISIT"
    assert [e["type"] for e in rec["events"]] == ["VISITED_CONFIRMED"]
    assert rec["feedback"]["note"] == NOTE_CANARY


def test_an_outcome_label_must_be_plain_text(ready: HEnv) -> None:
    for label in ("see https://example.com", "<b>bold</b>", "me@example.com", "12 Queen Street"):
        response = ready.outcome("a", key=f"k-{abs(hash(label))}-xx", user_label=label)
        assert response.status_code == 422, label
    assert ready.history_text().strip() == ""


@pytest.mark.parametrize("outcome,events,strength", [
    ("WENT", ["VISITED_CONFIRMED"], "SELF_REPORTED_VISIT"),
    ("DID_NOT_GO", ["SKIPPED"], "HANDOFF"),
    ("CHANGED_PLAN", ["SKIPPED"], "HANDOFF"),
])
def test_outcome_mapping_never_invents_a_visit(ready: HEnv, outcome, events, strength) -> None:
    ready.outcome("a", outcome=outcome)
    [rec] = ready.call("GET", "/v1/history/records", "a").json()["records"]
    assert [e["type"] for e in rec["events"]] == events and rec["strength"] == strength


def test_outcome_is_idempotent_by_key(ready: HEnv) -> None:
    ready.outcome("a", key="outcome-key-1")
    ready.outcome("a", key="outcome-key-1")
    assert len(ready.call("GET", "/v1/history/records", "a").json()["records"]) == 1
    ready.outcome("a", key="outcome-key-2")
    assert len(ready.call("GET", "/v1/history/records", "a").json()["records"]) == 2


def test_the_tag_comes_from_the_users_own_request_or_the_check_in(ready: HEnv) -> None:
    ready.workspace()
    ready.app.state.workspace_service._workspaces.put(  # noqa: SLF001 - seed the user's own goal
        "reg-a", "default", _with_goal(ready, "a cheap lunch spot"), ready.clock(), None)
    ready.outcome("a", key="outcome-key-1")
    ready.outcome("a", key="outcome-key-2", intent_tag="ERRAND")
    tags = sorted(r["intent_tag"] for r in ready.call("GET", "/v1/history/records", "a").json()["records"])
    assert tags == ["ERRAND", "FOOD_DRINK"]


def _with_goal(env: HEnv, goal: str) -> d.TripWorkspace:
    store = env.runtime.stores.workspaces
    current = store.get("reg-a", "default", env.clock())
    assert current is not None
    return d.evolve(current, goal=goal)


def test_a_sensitive_tag_is_never_derived_only_chosen(ready: HEnv) -> None:
    ready.workspace()
    ready.app.state.workspace_service._workspaces.put(  # noqa: SLF001
        "reg-a", "default", _with_goal(ready, "a playground for my kids"), ready.clock(), None)
    ready.outcome("a")
    [rec] = ready.call("GET", "/v1/history/records", "a").json()["records"]
    assert rec["intent_tag"] == "OTHER"


# --- retention ---------------------------------------------------------------------------------
def test_retention_default_bounds_and_shortening(ready: HEnv) -> None:
    settings = ready.call("GET", "/v1/history/settings", "a").json()
    assert settings == {"schema_version": 1, "kind": "history_settings", "retention_days": 90,
                        "default_retention_days": 90, "min_retention_days": 30,
                        "max_retention_days": 180}
    for bad in (181, 365, 29, 0, -5):
        assert ready.call("PUT", "/v1/history/settings", "a",
                          {"retention_days": bad}).status_code == 422
    assert ready.call("GET", "/v1/history/settings", "a").json()["retention_days"] == 90
    assert ready.call("PUT", "/v1/history/settings", "a",
                      {"retention_days": 180}).json()["retention_days"] == 180
    first = ready.outcome("a", key="outcome-key-1")
    assert first.status_code == 200
    [rec] = ready.call("GET", "/v1/history/records", "a").json()["records"]
    assert rec["retention_expires_at"].startswith(
        (ready.clock.now + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M"))


def test_shortening_purges_immediately_and_rolling_expiry_applies(ready: HEnv) -> None:
    ready.outcome("a", key="outcome-key-1")  # 90 days from now
    ready.clock.advance(days=40)
    ready.outcome("a", key="outcome-key-2", place_id=PLACE2)
    assert len(ready.call("GET", "/v1/history/records", "a").json()["records"]) == 2
    response = ready.call("PUT", "/v1/history/settings", "a", {"retention_days": 30})
    assert response.status_code == 200 and response.json()["retention_days"] == 30
    ids = [r["place_ref"]["place_id"] for r in ready.call("GET", "/v1/history/records", "a").json()["records"]]
    assert ids == [PLACE2]  # the 40-day-old record is gone at once
    assert PLACE not in ready.history_text()
    ready.clock.advance(days=31)  # rolling expiry: the survivor is now past 30 days
    assert ready.call("GET", "/v1/history/records", "a").json()["records"] == []
    assert ready.runtime.history_service.purge() == 1 or ready.fs is not None


def test_purge_removes_expired_documents_and_ttl_agrees(ready: HEnv) -> None:
    ready.outcome("a", key="outcome-key-1")
    ready.clock.advance(days=91)
    assert ready.runtime.history_service.purge() == 1
    assert PLACE not in ready.history_text()
    assert ready.runtime.purge_expired()["history"] == 0
    if ready.fs is not None:
        assert ready.fs.run_ttl_sweep() >= 0
        for path, doc in ready.fs.documents("history_records"):
            assert doc["expires_at"] > ready.clock.now


# --- edit, delete, clear ------------------------------------------------------------------------
def test_edit_label_tag_and_feedback(ready: HEnv) -> None:
    rid = ready.save_history("a", user_label="Cafe").json()["id"]
    edited = ready.call("PATCH", f"/v1/history/records/{rid}", "a", {
        "user_label": "Best cafe", "intent_tag": "OUTDOOR",
        "feedback": {"thumb": "UP", "reason_enum": "WRONG_VIBE", "note": NOTE_CANARY}})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["user_label"] == "Best cafe" and body["intent_tag"] == "OUTDOOR"
    assert body["feedback"] == {"thumb": "UP", "reason_enum": "WRONG_VIBE", "note": NOTE_CANARY}
    assert body["place_ref"]["place_id"] == PLACE and body["owner_id"] == "reg-a"
    cleared = ready.call("PATCH", f"/v1/history/records/{rid}", "a", {"clear_label": True})
    assert cleared.json()["user_label"] is None
    assert ready.call("PATCH", f"/v1/history/records/{rid}", "a",
                      {"user_label": "12 Queen Street"}).status_code == 422
    assert ready.call("PATCH", f"/v1/history/records/{rid}", "a",
                      {"place_id": PLACE2}).status_code == 422  # place and times are not editable
    assert ready.call("PATCH", "/v1/history/records/out_missing", "a",
                      {"user_label": "x"}).status_code == 404


def test_delete_one_and_clear_all(ready: HEnv) -> None:
    ids = [ready.save_history("a", key=f"hist-key-000{i}").json()["id"] for i in range(3)]
    assert ready.call("DELETE", f"/v1/history/records/{ids[0]}", "a").status_code == 200
    assert ready.call("DELETE", f"/v1/history/records/{ids[0]}", "a").status_code == 404
    cleared = ready.call("DELETE", "/v1/history/records", "a")
    assert cleared.json() == {"schema_version": 1, "kind": "history_delete_result", "deleted": 2}
    assert ready.call("GET", "/v1/history/records", "a").json()["records"] == []
    assert sorted(e.deleted_count for e in events_of(ready, t.HistoryRecordDeleted)) == [1, 2]


# --- tenant isolation --------------------------------------------------------------------------
def test_tenant_isolation_matrix(ready: HEnv) -> None:
    assert ready.consent("b", HIST).status_code == 200
    rid = ready.save_history("a", user_label=LABEL_CANARY).json()["id"]
    # b cannot see, edit or delete a's record, and b's list is empty
    assert ready.call("GET", "/v1/history/records", "b").json()["records"] == []
    assert ready.call("PATCH", f"/v1/history/records/{rid}", "b", {"user_label": "x"}).status_code == 404
    assert ready.call("DELETE", f"/v1/history/records/{rid}", "b").status_code == 404
    assert ready.call("DELETE", "/v1/history/records", "b").json()["deleted"] == 0
    # b's settings are b's own
    assert ready.call("PUT", "/v1/history/settings", "b", {"retention_days": 30}).status_code == 200
    assert ready.call("GET", "/v1/history/settings", "a").json()["retention_days"] == 90
    # b saving with a's workspace id is refused
    ws_a = ready.workspace("a")
    body = {"workspace_id": ws_a["id"], "plan_version": 1, "place_id": PLACE, "intent_tag": "OTHER"}
    assert ready.call("POST", "/v1/history/records", "b", body, "key-12345678").status_code == 404
    # the same idempotency key from two tenants never collides
    assert ready.save_history("b", key="hist-key-0001").status_code == 200
    assert len(ready.call("GET", "/v1/history/records", "a").json()["records"]) == 1
    assert [r["owner_id"] for r in ready.call("GET", "/v1/history/records", "b").json()["records"]] == ["reg-b"]


def test_rate_limited_with_the_existing_limiter(ready: HEnv) -> None:
    codes = [ready.call("GET", "/v1/history/records", "a").status_code for _ in range(125)]
    assert codes[0] == 200 and codes[-1] == 429


# --- consent withdrawal and account deletion ----------------------------------------------------
def test_withdrawing_the_history_consent_deletes_the_data(ready: HEnv) -> None:
    ready.save_history("a", user_label=LABEL_CANARY)
    ready.outcome("a", note=NOTE_CANARY)
    ready.call("PUT", "/v1/history/settings", "a", {"retention_days": 60})
    assert LABEL_CANARY in ready.stores_text() and NOTE_CANARY in ready.stores_text()
    response = ready.call("DELETE", f"/v1/me/consents/{HIST.value}", "a")
    assert response.status_code == 204
    text = ready.stores_text()
    assert LABEL_CANARY not in text and NOTE_CANARY not in text and PLACE not in text
    assert ready.history_text().strip() == ""
    # and the data is unreachable again until a fresh consent
    assert ready.call("GET", "/v1/history/records", "a").json().get("reason") == "CONSENT_REQUIRED"
    assert ready.consent("a", HIST).status_code == 200
    assert ready.call("GET", "/v1/history/records", "a").json()["records"] == []
    assert ready.call("GET", "/v1/history/settings", "a").json()["retention_days"] == 90


def test_withdrawal_only_deletes_the_callers_data(ready: HEnv) -> None:
    ready.consent("b", HIST)
    ready.save_history("b", key="hist-key-000b")
    ready.save_history("a")
    ready.call("DELETE", f"/v1/me/consents/{HIST.value}", "a")
    assert len(ready.call("GET", "/v1/history/records", "b").json()["records"]) == 1


def test_a_failed_withdrawal_cascade_is_retryable(ready: HEnv) -> None:
    ready.save_history("a")
    hooks = ready.app.state.consent_withdrawal_hooks[HIST]
    original = list(hooks)

    def boom(uid: str) -> int:
        raise RuntimeError("store down")

    hooks.insert(0, boom)
    assert ready.call("DELETE", f"/v1/me/consents/{HIST.value}", "a").status_code == 503
    hooks.remove(boom)
    assert hooks == original
    # the consent is already withdrawn, yet a retry still runs the deletion
    retry = ready.call("DELETE", f"/v1/me/consents/{HIST.value}", "a")
    assert retry.status_code == 404
    assert ready.history_text().strip() == ""


def test_export_holds_history_only_while_active_and_never_maps_content(ready: HEnv) -> None:
    ready.save_history("a", user_label=LABEL_CANARY)
    ready.outcome("a", key="outcome-key-1", note=NOTE_CANARY)
    bundle = ready.call("POST", "/v1/me:export", "a").json()
    assert bundle["history_active"] is True and len(bundle["outings"]) == 2
    assert {o["owner_id"] for o in bundle["outings"]} == {"reg-a"}
    assert LABEL_CANARY in str(bundle["outings"])
    assert not COORD.search(str(bundle["outings"]))
    other = ready.call("POST", "/v1/me:export", "b").json()
    assert other["outings"] == [] and other["saved_places"] == []


def test_account_deletion_leaves_nothing_in_any_store(ready: HEnv) -> None:
    ready.grant_all("a")
    ready.save_history("a", user_label=LABEL_CANARY)
    ready.outcome("a", note=NOTE_CANARY)
    ready.save_via_action("a", user_label=LABEL_CANARY)
    ready.call("PUT", "/v1/saved-places/ask-state", "a", {"label_kind": "WORK", "decision": "DECLINED"})
    ready.call("PUT", "/v1/history/settings", "a", {"retention_days": 60})
    before = ready.stores_text()
    assert LABEL_CANARY in before
    receipt = ready.call("POST", "/v1/me:delete", "a")
    assert receipt.status_code == 200 and receipt.json()["completed"] is True
    after = ready.stores_text()
    for needle in (LABEL_CANARY, NOTE_CANARY, PLACE, "reg-a", "43.653", "-79.383"):
        assert needle not in after, needle
    if ready.fs is not None:  # a raw-document scan across ALL collections
        for path, doc in ready.fs.documents():
            assert doc.get("owner_id") != "reg-a", path
