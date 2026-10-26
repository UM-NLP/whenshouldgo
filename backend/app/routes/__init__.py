"""One APIRouter per area. ``create_app`` includes every router; a feature owns only its module.

``session``, ``consents``, ``turns``, ``places``, ``actions``, ``workspace``, ``account``, the
outcome route and the history and saved-place routes are live (each answers a typed 503 until its
service is attached). The history and saved-place routes are DARK (decision 25): behind
``require_history_active`` they answer a typed 404 DISABLED before any work until ``history_enabled``
is on AND an approved notice version is configured.
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
