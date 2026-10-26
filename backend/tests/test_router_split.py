"""Item 1: main.py is split into app/routes/ (one APIRouter per area); behaviour is unchanged."""
from __future__ import annotations

import importlib
import inspect

import pytest
from fastapi import APIRouter

from app.main import create_app

MODULES = ("session", "consents", "turns", "places", "actions", "workspace", "account", "history",
           "saved_places")


@pytest.mark.parametrize("name", MODULES)
def test_each_area_module_exposes_one_router(name: str) -> None:
    module = importlib.import_module(f"app.routes.{name}")
    assert isinstance(module.router, APIRouter)


def test_existing_routes_keep_their_paths_methods_and_names() -> None:
    # FastAPI wraps included routers lazily, so read the operation ids from the OpenAPI document
    # (operationId = function name + path + method: it changes if either moves or is renamed).
    paths = create_app().openapi()["paths"]
    expected = {
        ("/v1/agent/turns", "post"): "agent_turn_v1_agent_turns_post",
        ("/v1/me/consents", "post"): "grant_consent_v1_me_consents_post",
        ("/v1/me/consents", "get"): "list_consents_v1_me_consents_get",
        ("/v1/me/consents/{purpose}", "delete"): "withdraw_consent_v1_me_consents__purpose__delete",
        ("/v1/session:bootstrap", "post"): "bootstrap_v1_session_bootstrap_post",
        ("/v1/me/usage", "get"): "my_usage_v1_me_usage_get",
        ("/v1/me/capabilities", "get"): "my_capabilities_v1_me_capabilities_get",
    }
    for (path, method), operation_id in expected.items():
        assert paths[path][method]["operationId"] == operation_id


def test_main_declares_only_healthz_itself() -> None:
    import app.main as main

    source = inspect.getsource(main)
    assert source.count("@application.get(") == 1 and "@application.post(" not in source
    assert "@application.delete(" not in source
