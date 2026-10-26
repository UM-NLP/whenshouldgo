"""The promoted contracts and the documented responses, read from the generated OpenAPI document."""
from __future__ import annotations

import warnings

import pytest

from app.main import create_app



@pytest.fixture(scope="module")
def schema() -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a "Duplicate Operation ID" is a UserWarning: it must not exist
        return create_app().openapi()


def test_no_operation_id_is_duplicated(schema: dict) -> None:
    ids = [op["operationId"] for ops in schema["paths"].values() for op in ops.values()]
    assert len(ids) == len(set(ids))
    assert len([p for p in schema["paths"] if p.endswith(":activate")]) == 1


def test_no_route_documents_a_501_any_more(schema: dict) -> None:
    # The history and saved-place routes are built (dark behind history_active, decision 25).
    stubs = {path for path, ops in schema["paths"].items()
             for op in ops.values() if "501" in op["responses"]}
    assert stubs == set()


def test_places_and_workspace_routes_document_real_errors(schema: dict) -> None:
    for path in ("/v1/places/details", "/v1/routes/check", "/v1/workspace"):
        for op in schema["paths"][path].values():
            assert {"404", "429", "503"} <= set(op["responses"])


def header_names(schema: dict, path: str, method: str) -> set[str]:
    return {p["name"] for p in schema["paths"][path][method].get("parameters", [])
            if p["in"] == "header"}


@pytest.mark.parametrize(("path", "method"), [
    ("/v1/workspace", "patch"), ("/v1/actions/propose", "post"),
    ("/v1/actions/{id}/confirm", "post"), ("/v1/outcomes", "post")])
def test_idempotency_key_is_declared(schema: dict, path: str, method: str) -> None:
    assert "idempotency-key" in header_names(schema, path, method)


def props(schema: dict, name: str) -> set[str]:
    return set(schema["components"]["schemas"][name]["properties"])


def test_promoted_models_and_fields(schema: dict) -> None:
    assert "due_at" in props(schema, "ProposeActionRequest")
    assert {"reminder", "handoff_url"} <= props(schema, "ConfirmActionResponse")
    assert "current_version" in props(schema, "ErrorBody")
    assert {"latest_plan", "warnings", "plan_versions"} <= props(schema, "WorkspaceView")
    assert {"workspaces", "plan_versions", "outcomes", "usage", "tier", "history_active"} <= props(
        schema, "ExportBundle")
    assert "place_id" in props(schema, "DiscoveryPlace")
    components = schema["components"]["schemas"]
    for name in ("MigrationStartRequest", "MigrationRunRequest", "MigrationView", "ReminderItem",
                 "PreferenceChoice", "MigrationState", "ExportWorkspace", "ExportUsage"):
        assert name in components, name
    assert "AccountExportBundle" not in components and "ConfirmedActionResult" not in components


def test_the_new_error_reasons_are_in_the_contract(schema: dict) -> None:
    reasons = set(schema["components"]["schemas"]["ErrorReason"]["enum"])
    assert {"LINK_INTENT_EXPIRED", "LINK_INTENT_CONSUMED", "ACCOUNT_COLLISION", "WRONG_UID",
            "ANONYMOUS_PROVIDER", "MIGRATION_INTERRUPTED"} <= reasons
