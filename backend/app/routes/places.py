"""POST /v1/places/details and POST /v1/routes/check (server-rendered, display-only).

The tenant is the verified principal (never the body). The service lives on
``app.state.places_service``; None (or an unreviewed price table) is a typed 503. Results are
shown to the user and never given to a model, stored, logged or traced beyond enums and counts.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import current_principal
from ..domain import (
    ConsentPurpose,
    ErrorCode,
    ErrorReason,
    PlaceDetailsRequest,
    PlaceDetailsResponse,
    Principal,
    RouteCheckRequest,
    RouteCheckResponse,
)
from ..places import PlacesError, PlacesService
from ..places.errors import PlacesNotConfigured
from .common import access_principal, error, stub_responses

router = APIRouter()
# The 501 entry is kept in the documented responses so the checked-in OpenAPI document does not
# change with this implementation (regenerate with ``npm run api:generate`` to drop it).
_RESPONSES = stub_responses(False)


def _service(request: Request) -> PlacesService:
    service = getattr(request.app.state, "places_service", None)
    if not isinstance(service, PlacesService):
        raise error(503, ErrorCode.PROVIDER, PlacesNotConfigured.default_message,
                    ErrorReason.SERVER_NOT_CONFIGURED)
    return service


def _typed(failure: PlacesError):
    return error(failure.status, failure.code, failure.message, failure.reason, failure.retry_after)


@router.post("/v1/places/details", response_model=PlaceDetailsResponse, responses=_RESPONSES)
def place_details(
    body: PlaceDetailsRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> PlaceDetailsResponse:
    access_principal(request, principal, request.app.state.access_limiter)
    service = _service(request)
    try:
        cards = service.place_details(principal.uid, body.place_ids)
    except PlacesError as failure:
        typed = _typed(failure)
        raise typed from None
    return PlaceDetailsResponse(places=cards)


@router.post("/v1/routes/check", response_model=RouteCheckResponse, responses=_RESPONSES)
def route_check(
    body: RouteCheckRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> RouteCheckResponse:
    access_principal(request, principal, request.app.state.access_limiter)
    if body.location is not None and not request.app.state.consents.has_active(
        principal.uid, ConsentPurpose.LOCATION_FOREGROUND
    ):
        # Exactly as the turn route: before any work, nothing consumed or stored.
        raise error(403, ErrorCode.POLICY, "location consent required",
                    ErrorReason.LOCATION_CONSENT_REQUIRED)
    service = _service(request)
    try:
        route = service.route_check(
            principal.uid, body.place_id, body.mode,
            body.location.rounded() if body.location is not None else None)
    except PlacesError as failure:
        typed = _typed(failure)
        raise typed from None
    return RouteCheckResponse(route=route)
