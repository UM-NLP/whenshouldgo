"""Item 12.2, 12.3 and 12.5: saved places and Home/Work, built DARK, over the real composition.

Create ONLY through a confirmed SAVE_PLACE PendingAction. PUT /v1/saved-places edits the label of an
existing entry; it can never create one. The position is the user's consented foreground location,
rounded at parse time, held only in the pending record until confirm.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import pytest
from history_support import (
    LABEL_CANARY,
    PLACE,
    PLACE2,
    RAW_LAT,
    RAW_LON,
    ROUNDED,
    HEnv,
    events_of,
)

from app import telemetry as t
from app.domain import ConsentPurpose

SAVED = ConsentPurpose.SAVED_PLACE
RAW = (str(RAW_LAT), str(RAW_LON), "43.65322", "79.38318")
ROUNDED_RE = re.compile(r"-?\d{1,3}\.\d{4,}")


@pytest.fixture(params=["memory", "fake-firestore"])
def env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> HEnv:
    return HEnv(request.param, monkeypatch)


@pytest.fixture
def ready(env: HEnv) -> HEnv:
    assert env.consent("a", SAVED).status_code == 200
    return env


def listed(env: HEnv, who: str = "a") -> list[dict]:
    response = env.call("GET", "/v1/saved-places", who)
    assert response.status_code == 200, response.text
    return response.json()["places"]


# --- creation only through a confirmed action ----------------------------------------------------
def test_a_confirmed_action_saves_the_place_with_a_rounded_position(ready: HEnv) -> None:
    result = ready.save_via_action("a", user_label=LABEL_CANARY)
    assert result["action"]["effect"] == "SAVE_PLACE" and result["action"]["confirmed_at"]
    [place] = listed(ready)
    assert place["place_id"] == PLACE and place["user_label"] == LABEL_CANARY
    assert place["label_kind"] == "HOME" and place["kind"] == "saved_place"
    assert place["position"] == ROUNDED  # rounded to 3 decimals server-side at parse time
    assert [e.label_kind.value for e in events_of(ready, t.SavedPlaceSaved)] == ["HOME"]


def test_put_can_never_create_a_saved_place(ready: HEnv) -> None:
    body = {"place_id": PLACE, "user_label": "Home", "label_kind": "HOME", "position": ROUNDED}
    for kind in ("HOME", "WORK", "CUSTOM"):
        response = ready.call("PUT", "/v1/saved-places", "a", {**body, "label_kind": kind})
        assert response.status_code == 404, response.text
    assert listed(ready) == []
    assert ready.history_text().strip() == ""


def test_put_edits_the_label_only(ready: HEnv) -> None:
    ready.save_via_action("a")
    base = {"place_id": PLACE, "user_label": "Mum's flat", "label_kind": "HOME", "position": ROUNDED}
    ok = ready.call("PUT", "/v1/saved-places", "a", base)
    assert ok.status_code == 200 and ok.json()["user_label"] == "Mum's flat"
    assert ok.json()["position"] == ROUNDED and ok.json()["label_kind"] == "HOME"
    # changing the kind or the position would create a Home/Work without confirmation: refused
    assert ready.call("PUT", "/v1/saved-places", "a", {**base, "label_kind": "WORK"}).status_code == 422
    moved = {"latitude": 44.0, "longitude": -80.0}
    assert ready.call("PUT", "/v1/saved-places", "a", {**base, "position": moved}).status_code == 422
    assert listed(ready)[0]["user_label"] == "Mum's flat" and listed(ready)[0]["position"] == ROUNDED
    assert ready.call("PUT", "/v1/saved-places", "a",
                      {**base, "user_label": "12 Queen Street"}).status_code == 422
    unrounded = {**base, "position": {"latitude": RAW_LAT, "longitude": RAW_LON}}
    assert ready.call("PUT", "/v1/saved-places", "a", unrounded).status_code == 200  # rounds on parse
    assert not ROUNDED_RE.search(ready.history_text())


def test_delete_one_all_and_idempotently(ready: HEnv) -> None:
    ready.save_via_action("a", key="save-key-0001")
    ready.save_via_action("a", key="save-key-0002", target=PLACE2, user_label="Gym",
                          label_kind="CUSTOM")
    assert len(listed(ready)) == 2
    one = ready.call("DELETE", f"/v1/saved-places?place_id={PLACE2}", "a")
    assert one.json() == {"schema_version": 1, "kind": "saved_place_delete_result", "deleted": 1}
    assert ready.call("DELETE", f"/v1/saved-places?place_id={PLACE2}", "a").json()["deleted"] == 0
    assert ready.call("DELETE", "/v1/saved-places", "a").json()["deleted"] == 1
    assert listed(ready) == []
    assert sorted(e.deleted_count for e in events_of(ready, t.SavedPlaceDeleted)) == [1, 1]


def test_a_new_home_replaces_the_old_one_only_through_confirmation(ready: HEnv) -> None:
    ready.save_via_action("a", key="save-key-0001")
    proposed = ready.propose_save("a", key="save-key-0002", target=PLACE2, user_label="New home")
    assert [p["place_id"] for p in listed(ready)] == [PLACE]  # nothing changes until confirm
    action = proposed.json()
    ready.call("POST", f"/v1/actions/{action['id']}/confirm", "a", {"nonce": action["nonce"]})
    assert [p["place_id"] for p in listed(ready)] == [PLACE2]


# --- preconditions -----------------------------------------------------------------------------
def test_propose_needs_a_position_and_registered_consent(env: HEnv) -> None:
    # no SAVED_PLACE consent yet
    response = env.propose_save("a")
    assert response.status_code == 403 and response.json()["reason"] == "CONSENT_REQUIRED"
    env.consent("a", SAVED)
    no_position = env.propose_save("a", key="save-key-0002", position=None)
    assert no_position.status_code == 422
    assert env.propose_save("a", key="save-key-0003").status_code == 200


def test_a_guest_cannot_propose_or_save(env: HEnv) -> None:
    env.consent("g", SAVED)  # a guest's ledger may hold the consent; it must still be refused
    response = env.propose_save("g")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "POLICY" and response.json()["reason"] == "GUEST_NOT_ALLOWED"
    for method, path, body in (("GET", "/v1/saved-places", None), ("DELETE", "/v1/saved-places", None)):
        assert env.call(method, path, "g", body).status_code == 403
    assert env.history_text().strip() == ""


def test_position_is_refused_on_other_effects(ready: HEnv) -> None:
    ws = ready.workspace()
    body = {"workspace_id": ws["id"], "plan_version": 1, "effect": "LAUNCH_HANDOFF",
            "target": PLACE, "position": {"latitude": RAW_LAT, "longitude": RAW_LON}}
    assert ready.call("POST", "/v1/actions/propose", "a", body, "key-12345678").status_code == 422


def test_withdrawing_the_consent_between_propose_and_confirm_saves_nothing(ready: HEnv) -> None:
    action = ready.propose_save("a").json()
    ready.call("DELETE", f"/v1/me/consents/{SAVED.value}", "a")
    confirmed = ready.call("POST", f"/v1/actions/{action['id']}/confirm", "a",
                           {"nonce": action["nonce"]})
    assert confirmed.status_code == 404  # the withdrawal deleted the pending record
    assert ready.history_text().strip() == ""
    assert "43.653" not in ready.stores_text()


def test_the_commit_rechecks_the_consent_even_without_the_cascade(ready: HEnv) -> None:
    action = ready.propose_save("a").json()
    assert ready.app.state.consents.withdraw("reg-a", SAVED) is True  # the ledger only
    confirmed = ready.call("POST", f"/v1/actions/{action['id']}/confirm", "a",
                           {"nonce": action["nonce"]})
    assert confirmed.status_code == 403 and confirmed.json()["reason"] == "CONSENT_REQUIRED"
    assert ready.history_text().strip() == ""
    # the proposal stays pending (nothing was saved) until it expires or is cancelled
    assert ready.call("POST", f"/v1/actions/{action['id']}/cancel", "a").status_code == 200
    assert "43.653" not in ready.stores_text()


def test_the_pending_action_is_a_normal_confirmation_protocol(ready: HEnv) -> None:
    action = ready.propose_save("a").json()
    assert action["nonce"] and action["expires_at"] and action["idempotency_key"]
    assert "position" not in action and "43.65" not in str(action)  # the preview shows no position
    wrong = ready.call("POST", f"/v1/actions/{action['id']}/confirm", "a", {"nonce": "x" * 20})
    assert wrong.status_code == 403
    assert listed(ready) == []
    # another tenant cannot confirm it
    ready.consent("b", SAVED)
    other = ready.call("POST", f"/v1/actions/{action['id']}/confirm", "b", {"nonce": action["nonce"]})
    assert other.status_code == 404
    assert listed(ready) == []


def test_confirm_twice_saves_once(ready: HEnv) -> None:
    action = ready.propose_save("a").json()
    url = f"/v1/actions/{action['id']}/confirm"
    first = ready.call("POST", url, "a", {"nonce": action["nonce"]})
    second = ready.call("POST", url, "a", {"nonce": action["nonce"]})
    assert first.status_code == second.status_code == 200
    assert len(listed(ready)) == 1 and len(events_of(ready, t.SavedPlaceSaved)) == 1


def test_expired_action_is_refused_and_the_position_is_purged(ready: HEnv) -> None:
    action = ready.propose_save("a").json()
    assert "43.653" in ready.stores_text()  # held in the pending record until confirm or expiry
    ready.clock.advance(minutes=16)
    response = ready.call("POST", f"/v1/actions/{action['id']}/confirm", "a", {"nonce": action["nonce"]})
    assert response.status_code == 409
    assert listed(ready) == []
    ready.runtime.purge_expired()
    if ready.fs is not None:
        ready.fs.run_ttl_sweep()
    assert "43.653" not in ready.stores_text()  # purged on expiry


def test_cancel_purges_the_position(ready: HEnv) -> None:
    action = ready.propose_save("a").json()
    assert "43.653" in ready.stores_text()
    assert ready.call("POST", f"/v1/actions/{action['id']}/cancel", "a").status_code == 200
    assert "43.653" not in ready.stores_text()


def test_confirm_moves_the_position_into_the_saved_place_only(ready: HEnv) -> None:
    ready.save_via_action("a")
    text = ready.stores_text()
    assert text.count("43.653") >= 1
    if ready.fs is not None:
        holders = {p.split("/")[0] for p, doc in ready.fs.documents() if "43.653" in repr(doc)}
        assert holders == {"saved_places"}
    else:
        from history_support import deep_text

        assert "43.653" not in deep_text(ready.runtime.action_service._store.items)
        assert "43.653" in deep_text(ready.runtime.saved_place_service.places.items)


# --- canaries ----------------------------------------------------------------------------------
def test_position_canary_never_stored_unrounded_or_logged(
    ready: HEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ready.save_via_action("a", user_label=LABEL_CANARY)
    ready.propose_save("a", key="save-key-0009", target=PLACE2)  # left pending
    ready.call("PUT", "/v1/saved-places", "a", {
        "place_id": PLACE, "user_label": "Renamed", "label_kind": "HOME",
        "position": {"latitude": RAW_LAT, "longitude": RAW_LON}})
    ready.call("POST", "/v1/me:export", "a")
    logs = caplog.text + " ".join(r.getMessage() for r in caplog.records)
    for text in (ready.stores_text(), logs, ready.events_text()):
        for needle in RAW:
            assert needle not in text
    # the rounded value exists only in saved places and the pending record
    assert "43.653" not in logs and "43.653" not in ready.events_text()


def test_label_canary_never_in_logs_or_telemetry(ready: HEnv, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ready.save_via_action("a", user_label=LABEL_CANARY)
    ready.save_history("a", user_label=LABEL_CANARY)
    ready.outcome("a", user_label=LABEL_CANARY)
    ready.consent("a", ConsentPurpose.HISTORY_STORE)
    ready.call("DELETE", "/v1/saved-places", "a")
    logs = caplog.text + " ".join(r.getMessage() for r in caplog.records)
    assert LABEL_CANARY not in logs and LABEL_CANARY not in ready.events_text()
    assert PLACE not in ready.events_text() and PLACE not in logs


def test_no_maps_content_in_any_stored_document_or_response(ready: HEnv) -> None:
    ready.save_via_action("a", user_label=LABEL_CANARY)
    responses = [ready.call("GET", "/v1/saved-places", "a").text,
                 ready.call("POST", "/v1/me:export", "a").text]
    forbidden = re.compile(r"display_name|formatted_address|rating|opening_hours|\breview|photo|"
                           r"website|http|price_level|\btypes\b")
    for text in (ready.stores_text(), *responses):
        assert not forbidden.search(text), forbidden.search(text)


# --- tenant isolation ---------------------------------------------------------------------------
def test_tenant_isolation_matrix(ready: HEnv) -> None:
    ready.consent("b", SAVED)
    ready.save_via_action("a", user_label=LABEL_CANARY)
    assert listed(ready, "b") == []
    assert ready.call("DELETE", f"/v1/saved-places?place_id={PLACE}", "b").json()["deleted"] == 0
    assert ready.call("DELETE", "/v1/saved-places", "b").json()["deleted"] == 0
    base = {"place_id": PLACE, "user_label": "hijack", "label_kind": "HOME", "position": ROUNDED}
    assert ready.call("PUT", "/v1/saved-places", "b", base).status_code == 404
    assert listed(ready, "a")[0]["user_label"] == LABEL_CANARY
    ready.save_via_action("b", key="save-key-0001", user_label="B home")  # same key, other tenant
    assert [p["user_label"] for p in listed(ready, "b")] == ["B home"]
    assert [p["user_label"] for p in listed(ready, "a")] == [LABEL_CANARY]


def test_a_disabled_principal_is_refused(ready: HEnv) -> None:
    assert ready.call("GET", "/v1/saved-places", "off").status_code == 403


def test_identity_is_never_read_from_the_body(ready: HEnv) -> None:
    ready.save_via_action("a")
    body = {"place_id": PLACE, "user_label": "x", "label_kind": "HOME", "position": ROUNDED}
    assert ready.call("PUT", "/v1/saved-places", "a", {**body, "owner_id": "reg-b"}).status_code == 422


def test_listing_needs_the_consent_but_deleting_never_does(env: HEnv) -> None:
    assert env.call("GET", "/v1/saved-places", "a").json()["reason"] == "CONSENT_REQUIRED"
    assert env.call("DELETE", "/v1/saved-places", "a").status_code == 200  # never blocked


# --- consent withdrawal, export, deletion --------------------------------------------------------
def test_withdrawing_the_saved_place_consent_deletes_places_and_ask_state(ready: HEnv) -> None:
    ready.save_via_action("a", user_label=LABEL_CANARY)
    ready.call("PUT", "/v1/saved-places/ask-state", "a", {"label_kind": "WORK", "decision": "DECLINED"})
    ready.propose_save("a", key="save-key-0007", target=PLACE2)  # a pending action with a position
    assert ready.call("DELETE", f"/v1/me/consents/{SAVED.value}", "a").status_code == 204
    assert ready.history_text().strip() == ""
    assert LABEL_CANARY not in ready.stores_text()
    assert ready.call("GET", "/v1/saved-places", "a").json()["reason"] == "CONSENT_REQUIRED"


def test_withdrawing_history_does_not_touch_saved_places(ready: HEnv) -> None:
    ready.consent("a", ConsentPurpose.HISTORY_STORE)
    ready.save_via_action("a")
    ready.save_history("a")
    ready.call("DELETE", f"/v1/me/consents/{ConsentPurpose.HISTORY_STORE.value}", "a")
    assert len(listed(ready)) == 1


def test_export_contains_saved_places_with_rounded_positions_only(ready: HEnv) -> None:
    ready.save_via_action("a", user_label=LABEL_CANARY)
    bundle = ready.call("POST", "/v1/me:export", "a").json()
    assert bundle["history_active"] is True and len(bundle["saved_places"]) == 1
    assert bundle["saved_places"][0]["position"] == ROUNDED
    assert not ROUNDED_RE.search(str(bundle))
    assert ready.call("POST", "/v1/me:export", "b").json()["saved_places"] == []


# --- the fixed 'near home' question ---------------------------------------------------------------
def ask(env: HEnv, message: str, who: str = "a"):
    return env.call("POST", "/v1/saved-places/ask-check", who, {"message": message})


def test_ask_check_returns_the_fixed_question_only(ready: HEnv) -> None:
    body = ask(ready, "a cafe near home").json()
    assert body == {"schema_version": 1, "kind": "saved_place_prompt", "ask": True,
                    "label_kind": "HOME",
                    "question": "Is where you are now your home? Save it as Home?"}
    assert ask(ready, "a cafe in Toronto").json()["ask"] is False
    assert ask(ready, "un resto près du bureau").json()["label_kind"] == "WORK"


def test_ask_check_is_stateless_about_the_message(ready: HEnv, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ask(ready, f"near home {LABEL_CANARY}")
    assert LABEL_CANARY not in ready.stores_text() and LABEL_CANARY not in caplog.text
    assert LABEL_CANARY not in ready.events_text()


def test_no_question_once_home_is_saved(ready: HEnv) -> None:
    ready.save_via_action("a")
    assert ask(ready, "near home").json()["ask"] is False
    assert ask(ready, "near work").json()["ask"] is True


def test_decline_starts_a_thirty_day_cooldown_and_dont_ask_again_is_permanent(ready: HEnv) -> None:
    declined = ready.call("PUT", "/v1/saved-places/ask-state", "a",
                          {"label_kind": "HOME", "decision": "DECLINED"})
    assert declined.status_code == 200 and declined.json()["recorded"] is True
    assert declined.json()["cooldown_until"].startswith(
        (ready.clock.now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"))
    assert ask(ready, "near home").json()["ask"] is False
    assert ask(ready, "near work").json()["ask"] is True  # per label
    ready.clock.advance(days=29)
    assert ask(ready, "near home").json()["ask"] is False
    ready.clock.advance(days=2)
    assert ask(ready, "near home").json()["ask"] is True
    never = ready.call("PUT", "/v1/saved-places/ask-state", "a",
                       {"label_kind": "HOME", "decision": "DONT_ASK_AGAIN"})
    assert never.json()["dont_ask_again"] is True
    ready.clock.advance(days=400)
    assert ask(ready, "near home").json()["ask"] is False
    again = ready.call("PUT", "/v1/saved-places/ask-state", "a",
                       {"label_kind": "HOME", "decision": "ASK_AGAIN"})
    assert again.json()["dont_ask_again"] is False and again.json()["cooldown_until"] is None
    assert ask(ready, "near home").json()["ask"] is True


def test_ask_state_is_per_tenant(ready: HEnv) -> None:
    ready.consent("b", SAVED)
    ready.call("PUT", "/v1/saved-places/ask-state", "a", {"label_kind": "HOME", "decision": "DONT_ASK_AGAIN"})
    assert ask(ready, "near home", "b").json()["ask"] is True


def test_ask_state_without_the_consent_is_not_stored(env: HEnv) -> None:
    response = env.call("PUT", "/v1/saved-places/ask-state", "a",
                        {"label_kind": "HOME", "decision": "DECLINED"})
    assert response.status_code == 200 and response.json()["recorded"] is False
    assert env.history_text().strip() == ""
    assert ask(env, "near home").json()["ask"] is True  # the client keeps its own cooldown


def test_ask_state_is_for_home_and_work_only_and_guests_are_refused(ready: HEnv) -> None:
    custom = ready.call("PUT", "/v1/saved-places/ask-state", "a",
                        {"label_kind": "CUSTOM", "decision": "DECLINED"})
    assert custom.status_code == 422
    assert ask(ready, "near home", "g").status_code == 403
    assert ready.call("PUT", "/v1/saved-places/ask-state", "g",
                      {"label_kind": "HOME", "decision": "DECLINED"}).status_code == 403


def test_the_turn_response_carries_the_fixed_prompt_for_a_registered_user(ready: HEnv) -> None:
    response = ready.call("POST", "/v1/agent/turns", "a", {"message": "a quiet cafe near home"},
                          key="turn-key-0001")
    assert response.status_code == 200, response.text
    prompt = response.json().get("saved_place_prompt")
    assert prompt == {"schema_version": 1, "kind": "saved_place_prompt", "ask": True,
                      "label_kind": "HOME",
                      "question": "Is where you are now your home? Save it as Home?"}
    quiet = ready.call("POST", "/v1/agent/turns", "a", {"message": "a quiet cafe in Toronto"},
                       key="turn-key-0002").json()
    assert quiet.get("saved_place_prompt") is None


def test_the_turn_never_prompts_a_guest_or_while_dark(
    env: HEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    guest = env.call("POST", "/v1/agent/turns", "g", {"message": "a quiet cafe near home"},
                     key="turn-key-0001")
    assert guest.status_code == 200 and guest.json().get("saved_place_prompt") is None
    dark = HEnv("memory", monkeypatch, enabled=False)
    response = dark.call("POST", "/v1/agent/turns", "a", {"message": "a quiet cafe near home"},
                         key="turn-key-0001")
    assert response.status_code == 200 and response.json().get("saved_place_prompt") is None
