"""OpenRouter adapter behind the provider-neutral ``ModelGateway``.

Parameter names were checked against OpenRouter's documentation on 2026-10-15:
- provider routing: https://openrouter.ai/docs/guides/routing/provider-selection
  (``provider.order``, ``provider.only``, ``provider.allow_fallbacks``,
  ``provider.data_collection``, ``provider.zdr``, ``provider.require_parameters``)
- zero data retention: https://openrouter.ai/docs/guides/features/zdr
  (``provider.zdr``; the per-request value can only turn enforcement on)
- API reference: https://openrouter.ai/docs/api-reference/overview
  (endpoint, normalized ``finish_reason`` values, ``usage.prompt_tokens`` /
  ``completion_tokens`` / ``total_tokens``, ``response_format.json_schema``)

No plugins, server tools, ``models`` fallback list or ``route`` field is ever sent: ZDR
does not cover plugins or tools. The adapter never logs; errors carry static messages and
are raised OUTSIDE ``except`` blocks so no exception context can hold prompt, provider
text or the key. The API key reaches the transport only as a ``Credential``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator, model_validator

from ..domain import ContentClass, Frozen, RuntimeConfig, evolve
from .errors import (
    BudgetExceededError,
    ContentFilterError,
    GatewayConfigError,
    GatewayError,
    MalformedOutputError,
    PrivacyViolationError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRejectedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ToolNotAllowedError,
    TransportFailure,
    TransportTimeout,
    TruncationError,
    UsageMismatchError,
)
from .policy import GatewayPolicy, check_request, is_pinned_model_id, self_check
from .pricing import PriceTable
from .schema import MAX_ARGUMENT_DEPTH, MAX_TOOL_ARGUMENT_BYTES, json_depth, outbound_schema, validate
from .types import (
    ZERO_USAGE,
    CallRole,
    FinishReason,
    GenerateRequest,
    ModelTurn,
    RunBudget,
    ToolCall,
    Usage,
)

OPENROUTER_HOST = "openrouter.ai"
OPENROUTER_CHAT_URL = f"https://{OPENROUTER_HOST}/api/v1/chat/completions"
MAX_RESPONSE_BYTES = 262_144
MAX_OUTPUT_CHARS = 65_536
REPAIR_INSTRUCTION = (
    "Your previous reply was invalid or incomplete. Reply again with one complete JSON "
    "object that matches the required schema, as briefly as possible."
)

_FINISH = {
    "stop": FinishReason.STOP,
    "tool_calls": FinishReason.TOOL_CALLS,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
    "error": FinishReason.ERROR,
}
ALLOWED_HEADERS = {"Content-Type": "application/json"}
_API_KEY = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII only
_TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# --- the body allowlist ---------------------------------------------------------------
_TOP_KEYS = {"model", "messages", "max_tokens", "temperature", "stream", "response_format", "provider"}
_PROVIDER_KEYS = {"order", "only", "allow_fallbacks", "data_collection", "zdr", "require_parameters"}
_MESSAGE_KEYS = {"role", "content", "tool_call_id", "tool_calls"}
_ROLES = {"system", "user", "assistant", "tool"}


def _reject_constant(name: str) -> Any:
    raise ValueError("non-finite JSON constant")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate object key")
    return dict(pairs)


def _loads(text: str | bytes) -> Any:
    """json.loads that refuses NaN, Infinity, -Infinity and duplicate object keys, and decodes
    bytes as strict UTF-8 only (json.loads would sniff UTF-16 and UTF-32 and skip a BOM)."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_reject_duplicates)


def _guarded(fn: Callable[..., Any], *args: Any) -> tuple[Any, bool]:
    """Run ``fn`` and report failure as a flag, so the caller can raise OUTSIDE the except
    block. Catches RecursionError and any parser surprise; the exception is discarded."""
    try:
        return fn(*args), True
    except Exception:  # noqa: BLE001 - deliberately broad; nothing from it is kept
        return None, False


# Public aliases for sibling packages (discovery) that need the same guarded parsing.
guarded = _guarded
strict_loads = _loads


def validate_url(url: str) -> None:
    """https, the exact host openrouter.ai, the chat path; no userinfo, port, query or fragment."""
    if (
        not isinstance(url, str) or not url.isascii() or not url.isprintable()
        or any(ch.isspace() for ch in url)
    ):
        raise GatewayConfigError("endpoint must be https://openrouter.ai/api/v1/chat/completions")
    parts, ok = _guarded(urlsplit, url)
    if not ok or (
        parts.scheme != "https"
        or parts.netloc != OPENROUTER_HOST  # also excludes userinfo, ports and case tricks
        or parts.path != "/api/v1/chat/completions"
        or parts.query
        or parts.fragment
    ):
        raise GatewayConfigError("endpoint must be https://openrouter.ai/api/v1/chat/completions")


def _exact(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def validate_body_shape(body: Any) -> None:
    """Allowlist check of the final request body. Anything not listed here is refused:
    unknown top-level or provider keys (tool_choice, preset, reasoning, include, user,
    session_id, metadata, usage, stop, plugins, models, route, provider.ignore/sort/
    max_price/quantizations ...), and any non-function tool. Runs in the gateway, in
    ``TransportRequest`` and again in ``HttpxTransport.send``."""
    bad = PrivacyViolationError
    if not isinstance(body, dict) or not (_TOP_KEYS <= set(body) <= _TOP_KEYS | {"tools"}):
        raise bad()
    provider = body["provider"]
    if not _exact(provider, _PROVIDER_KEYS):
        raise bad()
    order, only = provider["order"], provider["only"]
    if not (
        isinstance(order, list) and len(order) == 1 and isinstance(order[0], str)
        and _SLUG.fullmatch(order[0]) and only == order
        and provider["allow_fallbacks"] is False and provider["data_collection"] == "deny"
        and provider["zdr"] is True and provider["require_parameters"] is True
    ):  # fmt: skip
        raise bad()
    model = body["model"]
    if not isinstance(model, str) or not is_pinned_model_id(model):
        raise bad()
    max_tokens, temperature = body["max_tokens"], body["temperature"]
    if (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1
        or isinstance(temperature, bool) or temperature != 0
        or body["stream"] is not False
    ):  # fmt: skip
        raise bad()
    fmt = body["response_format"]
    if not _exact(fmt, {"type", "json_schema"}) or fmt["type"] != "json_schema":
        raise bad()
    inner = fmt["json_schema"]
    if not _exact(inner, {"name", "strict", "schema"}) or inner["strict"] is not True:
        raise bad()
    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        raise bad()
    for message in messages:
        if (
            not isinstance(message, dict) or "role" not in message or "content" not in message
            or not set(message) <= _MESSAGE_KEYS or message["role"] not in _ROLES
            or not isinstance(message["content"], str)
        ):  # fmt: skip
            raise bad()
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or not isinstance(message.get("tool_call_id", ""), str):
            raise bad()
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not (_exact(call, {"id", "type", "function"}) and call["type"] == "function"
                    and _exact(function, {"name", "arguments"})):  # fmt: skip
                raise bad()
    for tool in body.get("tools", ()):
        function = tool.get("function") if isinstance(tool, dict) else None
        if not (
            _exact(tool, {"type", "function"}) and tool["type"] == "function"
            and _exact(function, {"name", "description", "parameters"})
        ):  # fmt: skip
            raise bad()


def assert_privacy_body(body: dict[str, Any], request: GenerateRequest, policy: GatewayPolicy) -> None:
    """Gateway-side check: the allowlist shape plus agreement with the audited config."""
    validate_body_shape(body)
    config = request.runtime_config
    provider = body["provider"]
    if (
        provider["order"] != [config.provider]
        or config.provider not in config.provider_allowlist
        or body["model"] != config.model_id
        or body["max_tokens"] != request.max_output_tokens
    ):
        raise PrivacyViolationError()
    allowed = {tool.name for tool in request.allowed_tools} & set(policy.tool_allowlist)
    if any(tool["function"]["name"] not in allowed for tool in body.get("tools", ())):
        raise PrivacyViolationError("tool not in the request allowlist")


# --- credentials and transport --------------------------------------------------------
class Credential:
    """Holds the API key. Its repr and str are redacted; only ``reveal`` returns it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Credential(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> Any:  # never pickle or copy a secret into a trace
        raise TypeError("Credential cannot be serialized")


class TransportRequest(Frozen):
    """What a transport (and any trace of it) sees. It has no credential, and it can be
    built only with an allowlisted body and the exact OpenRouter endpoint."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any] = Field(repr=False)
    timeout_seconds: float = Field(gt=0)

    @field_validator("headers")
    @classmethod
    def _exact_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        # Exactly Content-Type: no credentials, cookies, Host or anything else rides along.
        if headers != ALLOWED_HEADERS:
            raise ValueError("headers must be exactly the allowlist")
        return headers

    @model_validator(mode="after")
    def _endpoint_and_body(self) -> TransportRequest:
        validate_url(self.url)
        validate_body_shape(self.body)
        return self


class TransportResponse(Frozen):
    status_code: int
    body: dict[str, Any] | None = Field(default=None, repr=False)
    size_bytes: int | None = Field(default=None, ge=0)  # filled from the body when omitted

    @model_validator(mode="before")
    @classmethod
    def _measure(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("size_bytes") is None:
            body = data.get("body")
            size, ok = _guarded(lambda: len(json.dumps(body))) if body is not None else (0, True)
            return {**data, "size_bytes": size if ok else MAX_RESPONSE_BYTES + 1}
        return data


class Transport(Protocol):
    def send(self, request: TransportRequest, credential: Credential) -> TransportResponse: ...


class _BearerAuth(httpx.Auth):
    """Adds the Authorization header inside httpx, so no headers dict holds the key."""

    def __init__(self, credential: Credential) -> None:
        self._credential = credential

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers["Authorization"] = f"Bearer {self._credential.reveal()}"
        yield request

    def __repr__(self) -> str:
        return "_BearerAuth(<redacted>)"


class HttpxTransport:
    """Real transport. It builds its own locked-down client (no injection), re-validates
    URL, headers and body itself, never follows redirects or reads proxy settings from the
    environment, asks for identity encoding, reads raw bytes under a size cap and stops at
    the request deadline."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, _transport: Any = None
    ) -> None:
        kwargs: dict[str, Any] = {
            "trust_env": False, "follow_redirects": False, "verify": True,
            "timeout": httpx.Timeout(10.0, connect=5.0),
        }
        if _transport is not None:  # test seam only; see for_tests
            kwargs["transport"] = _transport
        self._client = httpx.Client(**kwargs)
        self._clock = clock

    @classmethod
    def for_tests(
        cls, transport: httpx.BaseTransport, *, clock: Callable[[], float] = time.monotonic
    ) -> HttpxTransport:
        """The only way to inject a network layer: the client settings stay locked."""
        return cls(clock=clock, _transport=transport)

    def send(self, request: TransportRequest, credential: Credential) -> TransportResponse:
        validate_url(request.url)
        validate_body_shape(request.body)
        if request.headers != ALLOWED_HEADERS:
            raise GatewayConfigError("headers must be exactly the allowlist")
        timeout = float(request.timeout_seconds)
        deadline = self._clock() + timeout
        outcome = "ok"
        status = 0
        chunks: list[bytes] = []
        total = 0
        try:
            with self._client.stream(
                "POST", request.url, json=request.body,
                headers={**request.headers, "Accept-Encoding": "identity"},
                auth=_BearerAuth(credential),
                timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            ) as response:
                status = response.status_code
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    outcome = "failure"  # never decode: a small body must not expand past the cap
                else:
                    # A pre-read response only happens with in-memory test transports.
                    source = [response.content] if response.is_stream_consumed else response.iter_raw()
                    for chunk in source:
                        total += len(chunk)
                        if self._clock() > deadline:
                            outcome = "timeout"
                            break
                        if total > MAX_RESPONSE_BYTES:
                            outcome = "oversize"
                            break
                        chunks.append(chunk)
        except httpx.TimeoutException:
            outcome = "timeout"
        except Exception:  # noqa: BLE001 - httpx.InvalidURL, TLS, encoding ...; nothing is kept
            outcome = "failure"
        if outcome == "timeout":
            raise TransportTimeout
        if outcome != "ok":
            raise TransportFailure
        parsed, ok = _guarded(_loads, b"".join(chunks))
        return TransportResponse(
            status_code=status, body=parsed if ok and isinstance(parsed, dict) else None,
            size_bytes=total,
        )


def build_body(
    request: GenerateRequest, policy: GatewayPolicy, *, repair: bool = False
) -> dict[str, Any]:
    config = request.runtime_config
    messages: list[dict[str, Any]] = []
    for message in request.context:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, allow_nan=False),
                    },
                }
                for call in message.tool_calls
            ]
        messages.append(item)
    if repair:
        messages.append({"role": "system", "content": REPAIR_INSTRUCTION})
    body: dict[str, Any] = {
        "model": config.model_id,
        "messages": messages,
        "max_tokens": request.max_output_tokens,
        "temperature": 0,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": request.output_schema.name,
                "strict": True,
                "schema": request.output_schema.json_schema,
            },
        },
        "provider": {
            "order": [config.provider],
            "only": [config.provider],
            "allow_fallbacks": False,
            "data_collection": "deny",
            "zdr": True,
            "require_parameters": True,
        },
    }
    if request.allowed_tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": outbound_schema(tool.parameters),
                },
            }
            for tool in request.allowed_tools
        ]
    return body


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class QuotaLatch:
    """Kill switch: once the provider reports exhausted credit (402) every request stops
    reaching OpenRouter until someone resets it explicitly."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tripped = False

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped

    def trip(self) -> None:
        with self._lock:
            self._tripped = True

    def reset(self) -> None:
        with self._lock:
            self._tripped = False


class OpenRouterGateway:
    """Bound to one audited ``RuntimeConfig``. Validated at construction; a request whose
    config differs is refused. Use a second gateway for an escalation model."""

    def __init__(
        self,
        transport: Transport,
        secret_provider: Callable[[], str],
        config: RuntimeConfig,
        policy: GatewayPolicy,
        price_table: PriceTable,
        *,
        clock: Callable[[], float] = time.monotonic,
        production: bool = False,
        url: str = OPENROUTER_CHAT_URL,
        quota_latch: QuotaLatch | None = None,
    ) -> None:
        self_check(config, price_table, production=production)
        validate_url(url)
        self._transport = transport
        self._secret_provider = secret_provider
        self._config = config
        self._policy = policy
        self._prices = price_table
        self._clock = clock
        self._production = production
        self._url = url
        self.quota_latch = quota_latch or QuotaLatch()

    def _build_body(self, request: GenerateRequest, repair: bool) -> dict[str, Any]:
        return build_body(request, self._policy, repair=repair)

    def _credential(self) -> Credential:
        failed = False
        try:
            raw = self._secret_provider()
        except Exception:  # noqa: BLE001 - never keep the provider's exception (may hold the key)
            raw = None
            failed = True
        key = raw.strip() if isinstance(raw, str) else ""
        raw = None  # do not keep the unstripped value around
        if failed or not _API_KEY.fullmatch(key):
            raise GatewayConfigError("API key is unavailable or malformed")
        return Credential(key)

    def generate(self, request: GenerateRequest) -> ModelTurn:
        check_request(request, self._policy)
        if request.runtime_config != self._config:
            raise GatewayConfigError("request config is not the gateway's audited config")
        if self.quota_latch.tripped:
            raise ProviderQuotaError("provider credit exhausted; kill switch is latched")
        credential = self._credential()
        config = self._config

        started = self._clock()
        deadline = started + request.budget.seconds_remaining
        budget = request.budget
        total = ZERO_USAGE
        sent = [0]  # provider calls actually sent, including a repair
        repairs = 0
        repair = False
        while True:
            attempt_started = self._clock()
            sent_before = sent[0]
            try:
                turn = self._attempt(request, credential, repair, deadline, sent, budget)
            except GatewayError as error:
                spent = error.usage
                if (
                    spent is None
                    and sent[0] > sent_before
                    and not error.rejected_before_processing
                ):
                    # A call may have been processed and no usage came back (timeout,
                    # reset, bad usage): assume the worst. A 429/503 rejection is free.
                    spent = request.worst_case_usage
                if spent is not None:
                    total = total.plus(spent)
                error.usage = total if total != ZERO_USAGE else None
                error.calls = sent[0]
                if isinstance(error, ProviderQuotaError):
                    self.quota_latch.trip()
                budget = budget.consume(
                    calls=1, usage=spent, seconds=self._clock() - attempt_started
                )
                if not (
                    isinstance(error, (MalformedOutputError, TruncationError))
                    and request.allow_repair
                    and repairs < self._policy.max_repairs
                    and self._repair_covered(request, budget, deadline)
                ):
                    raise
                repairs += 1
                repair = True
                continue
            break
        total = total.plus(turn.usage)
        final, ok = _guarded(
            lambda: evolve(
                turn,
                usage=total,
                latency_ms=(self._clock() - started) * 1000,
                estimated_cost_usd=self._prices.estimate(config.model_id, config.provider, total),
                model_calls=sent[0],
                repairs=repairs,
            )
        )
        if not ok:
            raise MalformedOutputError(usage=total, calls=sent[0])
        return final

    def _repair_covered(self, request: GenerateRequest, budget: Any, deadline: float) -> bool:
        need_input = request.input_token_bound + self._policy.repair_instruction_tokens
        return (
            budget.calls_remaining >= 1
            and deadline - self._clock() > 0
            and need_input <= min(budget.per_call_max_input_tokens, budget.input_tokens_remaining)
            and request.max_output_tokens
            <= min(budget.per_call_max_output_tokens, budget.output_tokens_remaining)
        )

    def _attempt(
        self,
        request: GenerateRequest,
        credential: Credential,
        repair: bool,
        deadline: float,
        sent: list[int],
        budget: RunBudget,
    ) -> ModelTurn:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise BudgetExceededError("wall-clock budget exhausted")
        body, built = _guarded(self._build_body, request, repair)
        if not built:  # e.g. a non-finite number in a replayed tool call
            raise GatewayConfigError("request cannot be serialised")
        assert_privacy_body(body, request, self._policy)
        transport_request = TransportRequest(
            url=self._url,
            headers={"Content-Type": "application/json"},
            body=body,
            timeout_seconds=min(remaining, self._policy.request_timeout_cap_seconds),
        )
        failure: type[GatewayError] | None = None
        response: TransportResponse | None = None
        sent[0] += 1
        try:
            response = self._transport.send(transport_request, credential)
        except GatewayError:
            raise  # a transport's own refusal (privacy, config) keeps its type
        except TransportTimeout:
            failure = ProviderTimeoutError
        except Exception:  # noqa: BLE001 - TransportFailure and anything unexpected
            failure = ProviderUnavailableError
        if failure is not None or response is None:
            raise (failure or ProviderUnavailableError)()
        return self._parse(request, response, deadline, budget)

    # --- response handling -----------------------------------------------------------
    def _parse(
        self,
        request: GenerateRequest,
        response: TransportResponse,
        deadline: float,
        budget: RunBudget,
    ) -> ModelTurn:
        body = response.body
        worst = request.worst_case_usage
        if response.status_code != 200:
            raise _map_http_error(response.status_code, http=True)
        if body is None:
            raise MalformedOutputError()
        if (response.size_bytes or 0) > MAX_RESPONSE_BYTES:
            raise MalformedOutputError("provider response is too large", usage=worst)
        if "error" in body:
            code = body["error"].get("code") if isinstance(body["error"], dict) else None
            raise _map_http_error(code if isinstance(code, int) else 502, http=False)

        usage = self._parse_usage(request, body)
        if self._clock() > deadline:
            raise BudgetExceededError("wall-clock budget exhausted", usage=usage)
        # ``budget`` is what is left NOW (after any earlier attempt), not the request's
        # original snapshot, so a repair cannot overspend the run.
        if (
            usage.input_tokens > min(budget.per_call_max_input_tokens, budget.input_tokens_remaining)
            or usage.output_tokens
            > min(budget.per_call_max_output_tokens, budget.output_tokens_remaining)
        ):
            raise BudgetExceededError("reported usage exceeds the budget", usage=usage)
        # TODO(verify): the response ``provider`` field name and its value format (display
        # name versus slug) are NOT confirmed in OpenRouter's docs. Until they are, the
        # comparison stays loose (normalised membership in the allowlist, not equality) and
        # the field is mandatory only in production. Tighten to exact normalised equality
        # with config.provider once the format is verified.
        served_by = body.get("provider")
        if served_by is None and self._production:
            raise PrivacyViolationError("response does not say which provider served it", usage=usage)
        if served_by is not None and (
            not isinstance(served_by, str) or _norm(served_by) != _norm(self._config.provider)
        ):
            raise PrivacyViolationError("response came from a provider off the allowlist", usage=usage)

        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(choice, dict):
            raise MalformedOutputError(usage=usage)
        raw_finish = choice.get("finish_reason")
        finish = _FINISH.get(raw_finish.lower()) if isinstance(raw_finish, str) else None
        if finish is None:
            raise ProviderRejectedError("unknown or missing finish reason", usage=usage)
        if finish == FinishReason.LENGTH:
            raise TruncationError(usage=usage)
        if finish == FinishReason.CONTENT_FILTER:
            raise ContentFilterError(usage=usage)
        if finish == FinishReason.ERROR:
            raise ProviderUnavailableError("provider reported an error finish", usage=usage)

        message = choice.get("message")
        if not isinstance(message, dict):
            raise MalformedOutputError(usage=usage)
        config = self._config
        common: dict[str, Any] = {
            "call_role": request.call_role,
            "content_class": (
                ContentClass.MAPS_GROUNDED
                if request.call_role == CallRole.DISCOVER
                else ContentClass.USER
            ),
            "finish_reason": finish,
            "usage": usage,
            "latency_ms": 0.0,  # replaced by generate() with the end-to-end latency
            "estimated_cost_usd": self._prices.estimate(config.model_id, config.provider, usage),
            "price_table_version": self._prices.version,
            "price_is_placeholder": self._prices.placeholder,
            "model_id": config.model_id,
            "provider": config.provider,
            "runtime_config_version": config.version,
            "escalation": request.escalation,
        }
        raw_calls = message.get("tool_calls")
        if raw_calls:
            if finish != FinishReason.TOOL_CALLS:
                raise MalformedOutputError(usage=usage)
            calls = self._parse_tool_calls(request, raw_calls, usage)
            turn, ok = _guarded(lambda: ModelTurn(tool_calls=calls, **common))
            if not ok:
                raise MalformedOutputError(usage=usage)
            return turn
        if finish == FinishReason.TOOL_CALLS:
            raise MalformedOutputError(usage=usage)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_OUTPUT_CHARS:
            raise MalformedOutputError(usage=usage)
        parsed, ok = _guarded(_loads, content)  # NaN and Infinity are refused here
        if ok:
            output, ok = _guarded(request.output_schema.model.model_validate, parsed)
        if not ok:
            raise MalformedOutputError(usage=usage)
        turn, ok = _guarded(lambda: ModelTurn(output=output, **common))
        if not ok:
            raise MalformedOutputError(usage=usage)
        return turn

    @staticmethod
    def _parse_usage(request: GenerateRequest, body: dict[str, Any]) -> Usage:
        worst = request.worst_case_usage  # charged whenever usage cannot be trusted
        raw = body.get("usage")
        if not isinstance(raw, dict):
            raise UsageMismatchError(usage=worst)
        prompt, completion, total = (
            raw.get("prompt_tokens"),
            raw.get("completion_tokens"),
            raw.get("total_tokens"),
        )
        for value in (prompt, completion):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise UsageMismatchError(usage=worst)
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total != prompt + completion
        ):
            raise UsageMismatchError(usage=worst)
        if prompt == 0 or completion > request.max_output_tokens:
            raise UsageMismatchError(usage=worst)
        return Usage(input_tokens=prompt, output_tokens=completion)

    @staticmethod
    def _parse_tool_calls(
        request: GenerateRequest, raw_calls: Any, usage: Usage
    ) -> tuple[ToolCall, ...]:
        if not isinstance(raw_calls, list) or len(raw_calls) > 20:
            raise MalformedOutputError(usage=usage)
        specs = {tool.name: tool for tool in request.allowed_tools}
        calls: list[ToolCall] = []
        seen_ids: set[str] = set()
        for raw in raw_calls:
            function = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(function, dict):
                raise MalformedOutputError(usage=usage)
            name, call_id, arguments = function.get("name"), raw.get("id"), function.get("arguments")
            if not isinstance(name, str) or name not in specs:
                raise ToolNotAllowedError("model requested a tool outside the allowlist", usage=usage)
            if (
                not isinstance(call_id, str) or not _TOOL_CALL_ID.fullmatch(call_id)
                or call_id in seen_ids
                or not isinstance(arguments, str)
                or len(arguments.encode("utf-8", "replace")) > MAX_TOOL_ARGUMENT_BYTES
            ):  # fmt: skip
                raise MalformedOutputError(usage=usage)
            seen_ids.add(call_id)
            parsed, ok = _guarded(_loads, arguments)  # RecursionError and friends land here
            valid = False
            if ok and isinstance(parsed, dict):
                depth, depth_ok = _guarded(json_depth, parsed)
                if depth_ok and depth <= MAX_ARGUMENT_DEPTH:
                    valid, schema_ok = _guarded(validate, specs[name].parameters, parsed)
                    valid = valid and schema_ok
            if not valid:
                raise MalformedOutputError(usage=usage)
            call, built = _guarded(lambda: ToolCall(id=call_id, name=name, arguments=parsed))
            if not built:
                raise MalformedOutputError(usage=usage)
            calls.append(call)
        if len(calls) > request.budget.tool_calls_remaining:
            raise BudgetExceededError("tool call budget would be exceeded", usage=usage)
        return tuple(calls)


def _map_http_error(status: int, *, http: bool) -> GatewayError:
    """Map a status to a typed error. ``http`` is True only for the real HTTP status line.

    Only a real HTTP 429 is "rejected before processing" (free). A 503 is charged: the
    OpenRouter error page defines it as "no available model provider" or provider_overloaded
    and says nothing about billing. An error object inside a 200 body is never free."""
    error: GatewayError
    if status == 429:
        error = ProviderRateLimitError()
    elif status == 402:
        error = ProviderQuotaError()
    elif status in (408, 504):
        error = ProviderTimeoutError()
    elif status >= 500:
        error = ProviderUnavailableError()
    else:
        error = ProviderRejectedError()
    error.rejected_before_processing = http and status == 429
    return error
