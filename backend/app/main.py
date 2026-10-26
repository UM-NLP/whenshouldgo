from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import anyio
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import IdentityVerifier, PrincipalResolver
from .consent import ConsentStore, InMemoryConsentStore, OperationLimiter
from .stores.protocols import IdempotencyStore, WorkspaceStore
from .costs import CostLedger
from .discovery import DisplayCache, GroundingLiteClient
from .domain import ErrorCode, ErrorReason, RuntimeConfig
from .gateway import ModelGateway
from .orchestrator import TurnOrchestrator
from .quota import QuotaService
from .routes import ROUTERS
from .routes.common import app_check_gate
from .routes.common import error_response as _error_response
from .routes.turns import TurnRequest  # noqa: F401  (kept importable from here)
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

_STATUS_CODES = {
    401: ErrorCode.POLICY,
    403: ErrorCode.POLICY,
    404: ErrorCode.VALIDATION,
    405: ErrorCode.VALIDATION,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION,
    429: ErrorCode.QUOTA,
}


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
    enable_docs: bool = False,
    workspace_store: WorkspaceStore | None = None,
    idempotency_store: IdempotencyStore | None = None,
    account_guard: Callable[[str], None] | None = None,
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
             dev_scaffold or None, workspace_store, idempotency_store, account_guard)
    if orchestrator is not None and any(part is not None for part in parts):
        raise ValueError("pass either an orchestrator or the parts to build one, not both")
    # /docs, /redoc and /openapi.json are off unless explicitly enabled; ``app.openapi()`` (used by
    # scripts/export_openapi.py) still works.
    docs = {} if enable_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    application = FastAPI(title="WhenShouldGo API", version="0.1.0", **docs)
    application.state.orchestrator = orchestrator or TurnOrchestrator(
        quotas, trace_sink, gateway=gateway, discovery_client=discovery_client,
        display_cache=display_cache, runtime_config=runtime_config, cost_ledger=cost_ledger,
        clock=clock, dev_scaffold=dev_scaffold, workspace_store=workspace_store,
        idempotency_store=idempotency_store, account_guard=account_guard,
    )
    application.state.sink = (
        trace_sink or getattr(application.state.orchestrator, "sink", None) or get_default_sink()
    )
    application.add_middleware(_TraceMiddleware)
    application.state.verifier = verifier
    # The audited runtime config (None when the orchestrator carries none): read-only, server-owned.
    application.state.runtime_config = runtime_config or getattr(orchestrator, "runtime_config", None)
    application.state.consents = consent_store or InMemoryConsentStore(
        clock, config=application.state.runtime_config)
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
        version = detail.get("current_version") if isinstance(detail, dict) else None
        _trace_error(request, code, _phase_for(exc.status_code), exc.status_code)
        return _error_response(exc.status_code, code, message, headers, reason,
                               version if isinstance(version, int) else None)

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

    for router in ROUTERS:
        application.include_router(router, dependencies=[Depends(app_check_gate)])

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
