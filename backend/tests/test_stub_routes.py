"""Item 4: every planned route (places/details and routes/check are now live: test_places_details_routes) exists as a typed 501 NOT_IMPLEMENTED stub with its final contract.

History and saved-place routes answer a typed 404 DISABLED first, while ``history_enabled`` is off.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import domain as d
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.composition import pinned_runtime_config
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService

TOKENS = {
    "tok-a": VerifiedIdentity("guest-a", "anonymous"),
    "tok-off": VerifiedIdentity("off-1", "google.com"),
}
H = {"Authorization": "Bearer tok-a"}
SAVE = {"place_id": "ChIJabc", "user_label": "Home", "label_kind": "HOME",
        "position": {"latitude": 43.653, "longitude": -79.383}}

# (account link, export and delete are live: test_account_*.py)
# method, path template, request model | None, success response model, history-gated, body, query
STUBS = [
    # actions/propose|confirm|cancel and trips/{id}:activate are live: test_actions_routes.py
    ("GET", "/v1/history/records", None, "HistoryRecordList", True, None),
    ("POST", "/v1/history/records", "HistoryRecordCreate", "OutingRecord", True,
     {"workspace_id": "ws1", "plan_version": 1, "place_id": "ChIJabc", "intent_tag": "OTHER"}),
    ("DELETE", "/v1/history/records", None, "HistoryDeleteResult", True, None),
    ("GET", "/v1/saved-places", None, "SavedPlaceList", True, None),
    ("PUT", "/v1/saved-places", "SavedPlaceUpsert", "SavedPlace", True, SAVE),
    ("DELETE", "/v1/saved-places", None, "SavedPlaceDeleteResult", True, None),
]
IDS = [f"{m} {p}" for m, p, *_ in STUBS]


def make(history: bool = False, monkeypatch: pytest.MonkeyPatch | None = None):
    quotas = QuotaService()
    config = None
    if history:
        assert monkeypatch is not None
        monkeypatch.setattr(d, "APPROVED_HISTORY_NOTICE_VERSIONS", frozenset({"history-notice-1"}))
        config = d.evolve(pinned_runtime_config(), history_enabled=True,
                          history_notice_version="history-notice-1")
    app = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=quotas.policies, disabled_uids={"off-1"}),
        orchestrator=TurnOrchestrator(quotas, dev_scaffold=True, runtime_config=config),
    )
    return TestClient(app), quotas


def call(client, method, path, body, token="tok-a"):
    url = path.replace("{id}", "abc12345")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.request(method, url, json=body, headers=headers)


@pytest.mark.parametrize("method,path,req,resp,gated,body", STUBS, ids=IDS)
def test_requires_auth(method, path, req, resp, gated, body) -> None:
    client, _ = make()
    response = call(client, method, path, body, token=None)
    assert response.status_code == 401
    assert response.json()["code"] == "POLICY"


@pytest.mark.parametrize("method,path,req,resp,gated,body", STUBS, ids=IDS)
def test_typed_501_when_reachable(method, path, req, resp, gated, body, monkeypatch) -> None:
    client, _ = make(history=gated, monkeypatch=monkeypatch)
    response = call(client, method, path, body)
    assert response.status_code == 501, response.text
    assert response.json() == {"schema_version": 1, "kind": "error", "code": "PROVIDER",
                               "message": "not implemented", "reason": "NOT_IMPLEMENTED"}


@pytest.mark.parametrize("method,path,req,resp,gated,body", STUBS, ids=IDS)
def test_disabled_principal_is_refused(method, path, req, resp, gated, body, monkeypatch) -> None:
    client, _ = make(history=gated, monkeypatch=monkeypatch)
    response = call(client, method, path, body, token="tok-off")
    assert response.status_code == 403 and response.json()["code"] == "POLICY"


@pytest.mark.parametrize("method,path,req,resp,gated,body", STUBS, ids=IDS)
def test_rate_limited_with_the_existing_limiter(method, path, req, resp, gated, body, monkeypatch) -> None:
    client, _ = make(history=gated, monkeypatch=monkeypatch)
    statuses = [call(client, method, path, body).status_code for _ in range(125)]
    assert statuses[0] == 501 and statuses[-1] == 429
    response = call(client, method, path, body)
    assert response.json()["reason"] == "RATE_LIMITED" and "retry-after" in response.headers


@pytest.mark.parametrize("method,path,req,resp,gated,body", [s for s in STUBS if s[5]], ids=[
    IDS[i] for i, s in enumerate(STUBS) if s[5]])
def test_identity_is_never_read_from_the_body(method, path, req, resp, gated, body, monkeypatch) -> None:
    client, _ = make(history=gated, monkeypatch=monkeypatch)
    response = call(client, method, path, {**body, "uid": "someone-else", "owner_id": "x"})
    assert response.status_code == 422


@pytest.mark.parametrize("method,path,req,resp,gated,body", [s for s in STUBS if s[4]],
                         ids=[IDS[i] for i, s in enumerate(STUBS) if s[4]])
def test_history_routes_are_disabled_first_and_write_nothing(method, path, req, resp, gated, body) -> None:
    client, quotas = make()
    before_usage = dict(quotas._usage)  # noqa: SLF001
    for payload in (body, {"junk": 1}):  # DISABLED wins even over body validation
        response = call(client, method, path, payload)
        assert response.status_code == 404, response.text
        assert response.json() == {"schema_version": 1, "kind": "error", "code": "VALIDATION",
                                   "message": "feature disabled", "reason": "DISABLED"}
    assert dict(quotas._usage) == before_usage  # noqa: SLF001
    assert client.app.state.consents.ledger("guest-a") == ()
    # the limiter was not consumed either: the next gated call is still 404, never 429
    assert all(call(client, method, path, body).status_code == 404 for _ in range(130))
    # auth still comes first
    assert call(client, method, path, body, token=None).status_code == 401


def test_non_history_stubs_work_while_history_is_off() -> None:
    client, _ = make()
    for method, path, *_rest, gated, body in STUBS:
        if not gated:  # never the history flag's typed 404 DISABLED
            assert call(client, method, path, body).status_code != 404


def test_capabilities_history_flag_comes_from_the_server(monkeypatch) -> None:
    client, _ = make()
    caps = client.get("/v1/me/capabilities", headers=H).json()
    assert caps["history_enabled"] is False and caps["can_export"] is False and caps["can_delete"] is False
    client, _ = make(history=True, monkeypatch=monkeypatch)
    caps = client.get("/v1/me/capabilities", headers=H).json()
    assert caps["history_enabled"] is True and caps["can_export"] is False and caps["can_delete"] is False
    assert client.post("/v1/session:bootstrap", headers=H).json()["capabilities"]["history_enabled"] is True


@pytest.mark.parametrize("method,path,req,resp,gated,body", STUBS, ids=IDS)
def test_openapi_declares_the_final_contract(method, path, req, resp, gated, body) -> None:
    schema = create_app().openapi()
    operation = schema["paths"][path][method.lower()]
    assert operation["responses"]["501"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorBody")
    assert ("404" in operation["responses"]) is gated
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ok.endswith("/" + resp)
    if req:
        assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/" + req)
    else:
        assert "requestBody" not in operation
    auth = [p for p in operation["parameters"] if p["name"] == "authorization"]
    assert auth and auth[0]["required"] is True
    assert "401" in operation["responses"] and "429" in operation["responses"]
    assert {"NOT_IMPLEMENTED", "DISABLED"} <= set(schema["components"]["schemas"]["ErrorReason"]["enum"])


def test_pending_action_effect_in_the_contract_is_only_save_place_added() -> None:
    schema = create_app().openapi()["components"]["schemas"]["PendingActionEffect"]
    assert set(schema["enum"]) == {"SET_REMINDER", "LAUNCH_HANDOFF", "SAVE_PLACE"}


def test_maps_derived_models_are_marked_display_only_in_the_schema() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    assert schemas["PlaceCard"]["properties"]["display_name"]["x-display-only"] is True
    assert schemas["RouteCheck"]["properties"]["duration_minutes"]["x-display-only"] is True
