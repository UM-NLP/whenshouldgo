from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import IdentityVerifier, PrincipalResolver, current_principal
from .domain import Principal, PrincipalKind
from .orchestrator import IdempotencyKeyReused, TurnOrchestrator
from .quota import ReservationConflict


class TurnRequest(BaseModel):
    # Identity is never accepted from the body; extra fields are rejected (422).
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)


def create_app(
    verifier: IdentityVerifier | None = None,
    resolver: PrincipalResolver | None = None,
    orchestrator: TurnOrchestrator | None = None,
) -> FastAPI:
    """Build the API. With no verifier, every authenticated route fails closed (401)."""
    application = FastAPI(title="WhenShouldGo API", version="0.1.0")
    application.state.orchestrator = orchestrator or TurnOrchestrator()
    application.state.verifier = verifier
    application.state.resolver = resolver or PrincipalResolver(
        policies=application.state.orchestrator.quotas.policies
    )

    @application.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/v1/agent/turns")
    def agent_turn(
        body: TurnRequest,
        request: Request,
        principal: Principal = Depends(current_principal),
        idempotency_key: str = Header(min_length=1, max_length=200),
    ) -> dict:
        if principal.kind == PrincipalKind.DISABLED:
            raise HTTPException(status_code=403, detail="account unavailable")
        turn_orchestrator: TurnOrchestrator = request.app.state.orchestrator
        try:
            result = turn_orchestrator.run_turn(principal, body.message, idempotency_key)
        except ReservationConflict as error:
            raise HTTPException(status_code=409, detail="request already in progress") from error
        except IdempotencyKeyReused as error:
            raise HTTPException(
                status_code=422, detail="idempotency key reused with a different request"
            ) from error
        return {
            "reply": result.reply,
            "domain": result.domain,
            "allowed_tools": result.tools,
            "workspace_id": result.workspace.id if result.workspace else None,
            "allowance_exhausted": result.allowance_exhausted,
            "policy_version": principal.policy_version,
        }

    return application


app = create_app()
