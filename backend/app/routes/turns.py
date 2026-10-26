"""POST /v1/agent/turns."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..access import AccountUnavailable, usage_view
from ..auth import current_principal
from ..domain import (
    ConsentPurpose,
    ErrorCode,
    ErrorReason,
    GateReason,
    MeterState,
    Principal,
    PrincipalKind,
    SavedPlacePrompt,
    TurnResponse,
    UsagePeriod,
    UsageView,
    history_active,
)
from ..failures import TurnFailure
from ..location import LocationInput
from ..orchestrator import IdempotencyKeyReused, TurnOrchestrator
from ..quota import ReservationConflict
from .common import ERROR_RESPONSES, error, paid_route_gate

router = APIRouter()


class TurnRequest(BaseModel):
    # Identity is never accepted from the body; extra fields are rejected (422). Rejected input is
    # never echoed in an error (it can be a location).
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    message: str = Field(min_length=1, max_length=20_000)
    # Optional, foreground, consented: see app/location.py. Rounded on parse; hidden from repr and
    # from validation errors; never part of the idempotency fingerprint.
    location: LocationInput | None = Field(default=None, repr=False)


def _saved_place_prompt(request: Request, principal: Principal, message: str) -> SavedPlacePrompt | None:
    """The server-authored home/work question (decision 24) for a registered user while history is
    active; None otherwise. It can never fail a turn that already succeeded."""
    if principal.kind != PrincipalKind.REGISTERED_FREE:
        return None
    if not history_active(getattr(request.app.state, "runtime_config", None)):
        return None
    service = getattr(request.app.state, "saved_place_service", None)
    if service is None:
        return None
    try:
        prompt = service.prompt(principal, message)
    except Exception:  # noqa: BLE001 - a prompt is a courtesy: never an error on the turn
        return None
    return prompt if prompt.ask else None


@router.post("/v1/agent/turns", response_model=TurnResponse, responses=ERROR_RESPONSES)
def agent_turn(
    body: TurnRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    idempotency_key: str = Header(min_length=1, max_length=200),
) -> TurnResponse:
    if principal.kind == PrincipalKind.DISABLED:
        raise error(403, ErrorCode.POLICY, "account unavailable")
    paid_route_gate(request, principal, general=False)
    turn_orchestrator: TurnOrchestrator = request.app.state.orchestrator
    if body.location is not None and not request.app.state.consents.has_active(
        principal.uid, ConsentPurpose.LOCATION_FOREGROUND
    ):
        # Before any quota, model or tool work: nothing is consumed or stored.
        raise error(403, ErrorCode.POLICY, "location consent required",
                    ErrorReason.LOCATION_CONSENT_REQUIRED)
    try:
        result = turn_orchestrator.run_turn(
            principal, body.message, idempotency_key,
            location=body.location.rounded() if body.location is not None else None)
    except ReservationConflict as exc:
        raise error(409, ErrorCode.CONFLICT, "request already in progress") from exc
    except IdempotencyKeyReused as exc:
        raise error(
            409, ErrorCode.CONFLICT, "idempotency key reused with a different request"
        ) from exc
    except TurnFailure as failure:
        # Typed, static message; the reservation was already released by the orchestrator.
        raise error(failure.status, failure.code, failure.message, failure.reason,
                    failure.retry_after) from None
    try:
        try:
            usage = usage_view(turn_orchestrator.quotas, principal)
        except AccountUnavailable:
            # The turn is already counted: never a 500. Fail closed with a gated, empty view
            # (the client re-reads /v1/me/usage).
            usage = UsageView(
                period=UsagePeriod.LIFETIME, user_turns_used=0, user_turns_limit=0,
                activated_trips_used=0, activated_trips_limit=0, meter_state=MeterState.GATED,
                gate_reason=GateReason.TURNS_LIMIT)
        return TurnResponse(
            reply=result.reply,
            domain=result.domain,
            allowed_tools=result.tools,
            workspace_id=result.workspace.id if result.workspace else None,
            allowance_exhausted=result.allowance_exhausted,
            policy_version=turn_orchestrator.quotas.policies.get(principal.policy_version).version,
            usage=usage,
            discovery=result.discovery,
            warnings=result.warnings,
            saved_place_prompt=_saved_place_prompt(request, principal, body.message),
        )
    except ValidationError as exc:
        raise error(500, ErrorCode.VALIDATION, "turn result violated the response contract") from exc
