from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .access import AccountUnavailable, capabilities_for, session_bootstrap, usage_view
from .auth import IdentityVerifier, PrincipalResolver, current_principal
from .consent import (
    NOTICE_VERSIONS,
    SUPPORTED_PURPOSES,
    ConsentStore,
    InMemoryConsentStore,
    OperationLimiter,
    WrongNoticeVersion,
)
from .costs import CostLedger
from .discovery import DisplayCache, GroundingLiteClient
from .domain import (
    ConsentPurpose,
    ConsentRecord,
    Capabilities,
    ConsentProcessor,
    ErrorBody,
    ErrorCode,
    ErrorReason,
    Principal,
    PrincipalKind,
    RuntimeConfig,
    SessionBootstrap,
    TurnResponse,
    UsageView,
)
from .failures import TurnFailure
from .gateway import ModelGateway
from .location import LocationInput
from .orchestrator import IdempotencyKeyReused, TurnOrchestrator
from .quota import QuotaService, ReservationConflict
from .telemetry import (
    ErrorEvent,
    Phase,
    TraceSink,
    bind_trace_id,
    current_trace_id,
    ensure_log_guard,
    exception_type_name,
    frames_from,
    get_default_sink,
    trace,
)


class TurnRequest(BaseModel):
    # Identity is never accepted from the body; extra fields are rejected (422). Rejected input is
    # never echoed in an error (it can be a location).
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    message: str = Field(min_length=1, max_length=20_000)
    # Optional, foreground, consented: see app/location.py. Rounded on parse; hidden from repr and
    # from validation errors; never part of the idempotency fingerprint.
    location: LocationInput | None = Field(default=None, repr=False)


class ConsentGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: ConsentPurpose
    notice_version: str = Field(min_length=1, max_length=40)


class ConsentView(BaseModel):
    """An active consent as the client sees it (never a coordinate; withdrawn ones are not listed)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    purpose: ConsentPurpose
    notice_version: str
    processors: tuple[ConsentProcessor, ...]
    granted_at: datetime

    @classmethod
    def of(cls, record: ConsentRecord) -> "ConsentView":
        return cls(id=record.id, purpose=record.purpose, notice_version=record.notice_version,
                   processors=record.processors, granted_at=record.granted_at)


class ConsentList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consents: tuple[ConsentView, ...]


_STATUS_CODES = {
    401: ErrorCode.POLICY,
    403: ErrorCode.POLICY,
    404: ErrorCode.VALIDATION,
    405: ErrorCode.VALIDATION,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION,
    429: ErrorCode.QUOTA,
}
_RETRY_AFTER_HEADER = {
    "Retry-After": {
        "description": "Seconds to wait before retrying.",
        "schema": {"type": "integer", "minimum": 1},
    }
}
_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    status: {"model": ErrorBody, "description": str(code)}
    for status, code in (
        (401, "POLICY"), (403, "POLICY"), (409, "CONFLICT"), (422, "VALIDATION"), (429, "QUOTA"),
        (502, "VALIDATION"), (503, "PROVIDER"), (504, "PROVIDER"),
    )
}
_ERROR_RESPONSES[429]["headers"] = _RETRY_AFTER_HEADER


def _error(
    status: int, code: ErrorCode, message: str, reason: ErrorReason | None = None,
    retry_after: int | None = None,
) -> HTTPException:
    body = ErrorBody(code=code, message=message, reason=reason)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(status_code=status, detail=body.model_dump(mode="json", exclude_none=True),
                         headers=headers)


def _error_response(
    status: int, code: ErrorCode, message: str, headers: dict[str, str] | None = None,
    reason: ErrorReason | None = None,
) -> JSONResponse:
    body = ErrorBody(code=code, message=message[:500], reason=reason)
    return JSONResponse(
        status_code=status, content=body.model_dump(mode="json", exclude_none=True), headers=headers
    )


def _trace_error(
    request_or_scope: object,
    code: ErrorCode,
    phase: Phase,
    status: int | None,
    exception_type: str | None = None,
    frames: tuple[str, ...] = (),
) -> None:
    """Emit an ErrorEvent: enums, status and the exception class name only."""
    app_ref = getattr(request_or_scope, "app", None)
    if app_ref is None and isinstance(request_or_scope, dict):
        app_ref = request_or_scope.get("app")
    sink = getattr(getattr(app_ref, "state", None), "sink", None) or get_default_sink()
    trace(
        ErrorEvent,
        sink,
        trace_id=current_trace_id(),
        code=code,
        phase=phase,
        http_status=status,
        exception_type=exception_type,
        frames=frames,
    )


_CLIENT_GONE: tuple[type[BaseException], ...] = (
    ClientDisconnect,
    ConnectionResetError,
    BrokenPipeError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)


class _TraceMiddleware:
    """Binds a trace id per request and turns unhandled exceptions into a bare 500.

    Answering here (instead of re-raising to the server) keeps tracebacks, which can embed
    request text in exception messages, away from uvicorn's error logger.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status: int | None = None

        async def tracked_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        aborted = False
        with bind_trace_id():
            try:
                await self.app(scope, receive, tracked_send)
            except _CLIENT_GONE:
                # The peer left: not a server fault, and there is nobody to answer.
                return
            except Exception as error:  # noqa: BLE001 - last-resort boundary
                _trace_error(
                    scope, ErrorCode.PROVIDER, Phase.REQUEST, status or 500,
                    exception_type_name(error), frames_from(error),
                )
                if status is not None:
                    aborted = True
                else:
                    await _error_response(500, ErrorCode.PROVIDER, "internal error")(
                        scope, receive, send
                    )
        if aborted:
            # Raised outside the except block so __context__ is None: the original
            # exception (and any user text in its message) is not chained to this one.
            raise RuntimeError("response aborted")


def _phase_for(status: int) -> Phase:
    return Phase.AUTH if status in (401, 403) else Phase.REQUEST


def create_app(
    verifier: IdentityVerifier | None = None,
    resolver: PrincipalResolver | None = None,
    orchestrator: TurnOrchestrator | None = None,
    trace_sink: TraceSink | None = None,
    *,
    gateway: ModelGateway | None = None,
    discovery_client: GroundingLiteClient | None = None,
    display_cache: DisplayCache | None = None,
    runtime_config: RuntimeConfig | None = None,
    quotas: QuotaService | None = None,
    clock: Callable[[], datetime] | None = None,
    cost_ledger: CostLedger | None = None,
    dev_scaffold: bool = False,
    consent_store: ConsentStore | None = None,
) -> FastAPI:
    """Build the API. With no verifier, every authenticated route fails closed (401).

    ``gateway``, ``discovery_client``, ``display_cache`` and ``runtime_config`` are injected, never
    read from the environment (a later step adds the composition root). Without a gateway a turn
    that needs the model is a typed 503 (SERVER_NOT_CONFIGURED) unless ``dev_scaffold=True``
    explicitly asks for the development scaffold. A discovery client without a gateway is a
    configuration error (FRAME formulates its arguments). A gateway without a discovery client
    short-circuits discovery-class turns with a typed error before any model call. An explicit
    ``orchestrator`` cannot be combined with injected parts."""
    ensure_log_guard()
    parts = (gateway, discovery_client, display_cache, runtime_config, quotas, clock, cost_ledger,
             dev_scaffold or None)
    if orchestrator is not None and any(part is not None for part in parts):
        raise ValueError("pass either an orchestrator or the parts to build one, not both")
    application = FastAPI(title="WhenShouldGo API", version="0.1.0")
    application.state.orchestrator = orchestrator or TurnOrchestrator(
        quotas, trace_sink, gateway=gateway, discovery_client=discovery_client,
        display_cache=display_cache, runtime_config=runtime_config, cost_ledger=cost_ledger,
        clock=clock, dev_scaffold=dev_scaffold,
    )
    application.state.sink = (
        trace_sink or getattr(application.state.orchestrator, "sink", None) or get_default_sink()
    )
    application.add_middleware(_TraceMiddleware)
    application.state.verifier = verifier
    application.state.consents = consent_store or InMemoryConsentStore(clock)
    application.state.consent_limiter = OperationLimiter(clock)
    application.state.clock = clock or (lambda: datetime.now(UTC))
    # Bootstrap is the tightest (once per page load); reads of the meter and capabilities are cheap.
    application.state.bootstrap_limiter = OperationLimiter(clock, max_operations=30)
    application.state.access_limiter = OperationLimiter(clock, max_operations=120)
    application.state.resolver = resolver or PrincipalResolver(
        policies=application.state.orchestrator.quotas.policies
    )

    @application.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("kind") == "error":
            code = ErrorCode(detail["code"])
            message = str(detail["message"])
        else:
            # Only server-authored strings reach here (auth failures, routing errors).
            code = _STATUS_CODES.get(exc.status_code, ErrorCode.PROVIDER)
            message = detail if isinstance(detail, str) else "request failed"
        headers = dict(exc.headers) if exc.headers else None
        reason = ErrorReason(detail["reason"]) if isinstance(detail, dict) and detail.get("reason") else None
        _trace_error(request, code, _phase_for(exc.status_code), exc.status_code)
        return _error_response(exc.status_code, code, message, headers, reason)

    @application.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, __: RequestValidationError) -> JSONResponse:
        # Redacted on purpose: pydantic details echo the rejected input.
        _trace_error(request, ErrorCode.VALIDATION, Phase.REQUEST, 422)
        return _error_response(422, ErrorCode.VALIDATION, "request failed validation")

    # No @exception_handler(Exception): Starlette would install it on the outermost
    # ServerErrorMiddleware, which _TraceMiddleware (inside it) always pre-empts, so it
    # would be dead code. _TraceMiddleware owns the unhandled-exception path.

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
        if body.location is not None and not request.app.state.consents.has_active(
            principal.uid, ConsentPurpose.LOCATION_FOREGROUND
        ):
            # Before any quota, model or tool work: nothing is consumed or stored.
            raise _error(403, ErrorCode.POLICY, "location consent required",
                         ErrorReason.LOCATION_CONSENT_REQUIRED)
        try:
            result = turn_orchestrator.run_turn(
                principal, body.message, idempotency_key,
                location=body.location.rounded() if body.location is not None else None)
        except ReservationConflict as error:
            raise _error(409, ErrorCode.CONFLICT, "request already in progress") from error
        except IdempotencyKeyReused as error:
            raise _error(
                409, ErrorCode.CONFLICT, "idempotency key reused with a different request"
            ) from error
        except TurnFailure as failure:
            # Typed, static message; the reservation was already released by the orchestrator.
            raise _error(failure.status, failure.code, failure.message, failure.reason,
                         failure.retry_after) from None
        try:
            return TurnResponse(
                reply=result.reply,
                domain=result.domain,
                allowed_tools=result.tools,
                workspace_id=result.workspace.id if result.workspace else None,
                allowance_exhausted=result.allowance_exhausted,
                policy_version=principal.policy_version,
                usage=usage_view(turn_orchestrator.quotas, principal),
                discovery=result.discovery,
                warnings=result.warnings,
            )
        except ValidationError as error:
            raise _error(500, ErrorCode.VALIDATION, "turn result violated the response contract") from error

    def _consent_principal(request: Request, principal: Principal) -> ConsentStore:
        if principal.kind == PrincipalKind.DISABLED:
            raise _error(403, ErrorCode.POLICY, "account unavailable")
        wait = request.app.state.consent_limiter.check(principal.uid)
        if wait is not None:
            raise _error(429, ErrorCode.QUOTA, "too many requests", ErrorReason.RATE_LIMITED, wait)
        return request.app.state.consents

    @application.post("/v1/me/consents", response_model=ConsentView, responses=_ERROR_RESPONSES)
    def grant_consent(
        body: ConsentGrantRequest, request: Request,
        principal: Principal = Depends(current_principal),
    ) -> ConsentView:
        store = _consent_principal(request, principal)
        if body.purpose not in SUPPORTED_PURPOSES or body.notice_version != NOTICE_VERSIONS[body.purpose]:
            raise _error(422, ErrorCode.VALIDATION, "unsupported purpose or notice version")
        try:
            return ConsentView.of(store.grant(principal.uid, body.purpose, body.notice_version))
        except WrongNoticeVersion:
            raise _error(422, ErrorCode.VALIDATION, "unsupported purpose or notice version") from None

    @application.get("/v1/me/consents", response_model=ConsentList, responses=_ERROR_RESPONSES)
    def list_consents(
        request: Request, principal: Principal = Depends(current_principal)
    ) -> ConsentList:
        store = _consent_principal(request, principal)
        return ConsentList(consents=tuple(ConsentView.of(r) for r in store.active(principal.uid)))

    @application.delete("/v1/me/consents/{purpose}", status_code=204, response_class=Response,
                        responses=_ERROR_RESPONSES)
    def withdraw_consent(
        purpose: str, request: Request, principal: Principal = Depends(current_principal)
    ) -> Response:
        store = _consent_principal(request, principal)
        try:
            parsed = ConsentPurpose(purpose)
        except ValueError:
            raise _error(404, ErrorCode.VALIDATION, "no such consent") from None
        if parsed not in SUPPORTED_PURPOSES or not store.withdraw(principal.uid, parsed):
            raise _error(404, ErrorCode.VALIDATION, "no such consent")
        return Response(status_code=204)

    def _access_principal(request: Request, principal: Principal, limiter: OperationLimiter) -> QuotaService:
        if principal.kind == PrincipalKind.DISABLED:
            raise _error(403, ErrorCode.POLICY, "account unavailable")
        wait = limiter.check(principal.uid)
        if wait is not None:
            raise _error(429, ErrorCode.QUOTA, "too many requests", ErrorReason.RATE_LIMITED, wait)
        return request.app.state.orchestrator.quotas

    @application.post("/v1/session:bootstrap", response_model=SessionBootstrap, responses=_ERROR_RESPONSES)
    def bootstrap(request: Request, principal: Principal = Depends(current_principal)) -> SessionBootstrap:
        quotas = _access_principal(request, principal, request.app.state.bootstrap_limiter)
        try:
            return session_bootstrap(quotas, principal, request.app.state.clock())
        except AccountUnavailable:
            raise _error(403, ErrorCode.POLICY, "account unavailable") from None

    @application.get("/v1/me/usage", response_model=UsageView, responses=_ERROR_RESPONSES)
    def my_usage(request: Request, principal: Principal = Depends(current_principal)) -> UsageView:
        quotas = _access_principal(request, principal, request.app.state.access_limiter)
        try:
            return usage_view(quotas, principal)
        except AccountUnavailable:
            raise _error(403, ErrorCode.POLICY, "account unavailable") from None

    @application.get("/v1/me/capabilities", response_model=Capabilities, responses=_ERROR_RESPONSES)
    def my_capabilities(request: Request, principal: Principal = Depends(current_principal)) -> Capabilities:
        quotas = _access_principal(request, principal, request.app.state.access_limiter)
        try:
            return capabilities_for(principal, usage_view(quotas, principal))
        except AccountUnavailable:
            raise _error(403, ErrorCode.POLICY, "account unavailable") from None

    def _openapi() -> dict:
        if application.openapi_schema:
            return application.openapi_schema
        schema = FastAPI.openapi(application)
        # The server answers 401 without an Authorization header, so the schema says it is required
        # (a typed client must send it). The dependency keeps it optional to keep the 401 contract.
        for path in schema["paths"].values():
            for operation in path.values():
                for parameter in operation.get("parameters", []):
                    if parameter["in"] == "header" and parameter["name"] == "authorization":
                        parameter["required"] = True
        return schema

    application.openapi = _openapi  # type: ignore[method-assign]
    return application


app = create_app()
