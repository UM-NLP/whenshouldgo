from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import IdentityVerifier, PrincipalResolver, current_principal
from .domain import ErrorBody, ErrorCode, Principal, PrincipalKind, TurnResponse
from .orchestrator import IdempotencyKeyReused, TurnOrchestrator
from .quota import ReservationConflict


class TurnRequest(BaseModel):
    # Identity is never accepted from the body; extra fields are rejected (422).
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)


_STATUS_CODES = {
    401: ErrorCode.POLICY,
    403: ErrorCode.POLICY,
    404: ErrorCode.VALIDATION,
    405: ErrorCode.VALIDATION,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION,
    429: ErrorCode.QUOTA,
}
_ERROR_RESPONSES = {
    status: {"model": ErrorBody, "description": str(code)}
    for status, code in ((401, "POLICY"), (403, "POLICY"), (409, "CONFLICT"), (422, "VALIDATION"), (429, "QUOTA"))
}


def _error(status: int, code: ErrorCode, message: str) -> HTTPException:
    return HTTPException(
        status_code=status, detail=ErrorBody(code=code, message=message).model_dump(mode="json")
    )


def _error_response(
    status: int, code: ErrorCode, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = ErrorBody(code=code, message=message[:500]).model_dump(mode="json")
    return JSONResponse(status_code=status, content=body, headers=headers)


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

    @application.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("kind") == "error":
            code = ErrorCode(detail["code"])
            message = str(detail["message"])
        else:
            # Only server-authored strings reach here (auth failures, routing errors).
            code = _STATUS_CODES.get(exc.status_code, ErrorCode.PROVIDER)
            message = detail if isinstance(detail, str) else "request failed"
        headers = dict(exc.headers) if exc.headers else None
        return _error_response(exc.status_code, code, message, headers)

    @application.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        # Redacted on purpose: pydantic details echo the rejected input.
        return _error_response(422, ErrorCode.VALIDATION, "request failed validation")

    @application.exception_handler(Exception)
    async def _unhandled(_: Request, __: Exception) -> JSONResponse:
        return _error_response(500, ErrorCode.PROVIDER, "internal error")

    @application.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/v1/agent/turns", response_model=TurnResponse, responses=_ERROR_RESPONSES)
    def agent_turn(
        body: TurnRequest,
        request: Request,
        principal: Principal = Depends(current_principal),
        idempotency_key: str = Header(min_length=1, max_length=200),
    ) -> TurnResponse:
        if principal.kind == PrincipalKind.DISABLED:
            raise _error(403, ErrorCode.POLICY, "account unavailable")
        turn_orchestrator: TurnOrchestrator = request.app.state.orchestrator
        try:
            result = turn_orchestrator.run_turn(principal, body.message, idempotency_key)
        except ReservationConflict as error:
            raise _error(409, ErrorCode.CONFLICT, "request already in progress") from error
        except IdempotencyKeyReused as error:
            raise _error(
                409, ErrorCode.CONFLICT, "idempotency key reused with a different request"
            ) from error
        try:
            return TurnResponse(
                reply=result.reply,
                domain=result.domain,
                allowed_tools=result.tools,
                workspace_id=result.workspace.id if result.workspace else None,
                allowance_exhausted=result.allowance_exhausted,
                policy_version=principal.policy_version,
            )
        except ValidationError as error:
            raise _error(500, ErrorCode.VALIDATION, "turn result violated the response contract") from error

    return application


app = create_app()
