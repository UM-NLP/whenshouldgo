"""Saved places and Home/Work (dark: needs history_active, decisions 24 and 25).

Live over ``app.state.saved_place_service``; None is a typed 503. Every route runs
``require_history_active`` first (404 DISABLED, nothing written). Guests get a typed 403 (saved
places stay on the device); list and edit need the SAVED_PLACE consent, delete never does.

There is NO route that creates a saved place: creation is only the confirmed ``SAVE_PLACE``
PendingAction (``/v1/actions``). ``PUT`` edits the label of an existing entry and refuses a
different kind or position. A saved Home or Work is exposed to the client only and is never applied
silently. The fixed "near home" question and its ask-state are server-authored (``ask_policy.py``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..auth import current_principal
from ..domain import (
    AskCheckRequest,
    AskStateUpdate,
    AskStateView,
    ErrorCode,
    ErrorReason,
    Principal,
    SavedPlace,
    SavedPlaceDeleteResult,
    SavedPlaceList,
    SavedPlacePrompt,
    SavedPlaceUpsert,
)
from ..feature_errors import FeatureError
from ..saved_places import SavedPlaceService
from .common import (
    access_principal,
    error,
    feature_error,
    history_responses,
    require_history_active,
)

router = APIRouter()
_GATE = [Depends(require_history_active)]
_RESPONSES = history_responses()


def _service(request: Request, principal: Principal) -> SavedPlaceService:
    access_principal(request, principal, request.app.state.access_limiter)
    found = getattr(request.app.state, "saved_place_service", None)
    if found is None:
        raise error(503, ErrorCode.PROVIDER, "saved places not configured",
                    ErrorReason.SERVER_NOT_CONFIGURED)
    return found


@router.get("/v1/saved-places", response_model=SavedPlaceList, responses=_RESPONSES,
            dependencies=_GATE)
def list_saved_places(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> SavedPlaceList:
    service = _service(request, principal)
    try:
        return SavedPlaceList(places=service.list(principal))
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.put("/v1/saved-places", response_model=SavedPlace, responses=_RESPONSES,
            dependencies=_GATE)
def put_saved_place(
    body: SavedPlaceUpsert,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> SavedPlace:
    """Edit the label of an EXISTING saved place. Creating or moving one needs a confirmed
    SAVE_PLACE action, so an unknown place ID is a 404 and a different kind or position a 422."""
    service = _service(request, principal)
    try:
        return service.relabel(principal, body)
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.delete("/v1/saved-places", response_model=SavedPlaceDeleteResult, responses=_RESPONSES,
               dependencies=_GATE)
def delete_saved_places(
    request: Request,
    place_id: str | None = Query(default=None, pattern=r"^[A-Za-z0-9_-]{1,300}$"),
    principal: Principal = Depends(current_principal),
) -> SavedPlaceDeleteResult:
    """One saved place (``place_id``) or, without it, all of them."""
    service = _service(request, principal)
    try:
        return SavedPlaceDeleteResult(deleted=service.delete(principal, place_id))
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.post("/v1/saved-places/ask-check", response_model=SavedPlacePrompt, responses=_RESPONSES,
             dependencies=_GATE)
def check_saved_place_ask(
    body: AskCheckRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> SavedPlacePrompt:
    """Whether to show the FIXED "Is where you are now your home? Save it as Home?" question for
    this message. The text is server-authored; the message is neither stored nor logged."""
    service = _service(request, principal)
    try:
        return service.prompt(principal, body.message)
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.put("/v1/saved-places/ask-state", response_model=AskStateView, responses=_RESPONSES,
            dependencies=_GATE)
def put_saved_place_ask_state(
    body: AskStateUpdate,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> AskStateView:
    """Record a decline (30-day cooldown), "don't ask again", or clear them. ``recorded`` is False
    when the server stored nothing (no SAVED_PLACE consent): the client keeps it on the device."""
    service = _service(request, principal)
    try:
        return service.set_ask_state(principal, body)
    except FeatureError as failure:
        raise feature_error(failure) from None
