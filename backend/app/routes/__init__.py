"""One APIRouter per area. ``create_app`` includes every router; a feature owns only its module.

``session``, ``consents`` and ``turns`` are live. ``places``, ``actions``, ``workspace``,
``account``, ``history`` and ``saved_places`` hold typed 501 stubs that freeze the API contract;
replace a stub's ``not_implemented`` call (keeping its path and models) when the route is built.
"""
from __future__ import annotations

from fastapi import APIRouter

from . import account, actions, consents, history, places, saved_places, session, turns, workspace

# Order fixes the order of paths in the OpenAPI document.
ROUTERS: tuple[APIRouter, ...] = (
    turns.router, consents.router, session.router, places.router, actions.router,
    workspace.router, account.router, history.router, saved_places.router,
)

__all__ = ["ROUTERS"]
