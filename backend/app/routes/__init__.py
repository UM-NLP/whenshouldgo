"""One APIRouter per area. ``create_app`` includes every router; a feature owns only its module.

``session``, ``consents``, ``turns``, ``places``, ``actions``, ``workspace``, ``account`` and the
outcome route are live (each answers a typed 503 until its service is attached). The history and
saved-place routes are dark typed 501 stubs behind ``require_history_active``; replace a stub's
``not_implemented`` call (keeping its path and models) when the route is built.
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
