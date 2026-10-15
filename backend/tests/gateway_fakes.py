"""Deterministic test doubles (tests only; app code must never import this). No network, no credentials, no real Maps content.

``SyntheticMapsAnswer`` imitates the shape of a Grounding Lite result (an answer, place
IDs and source links) using invented identifiers and invented text only.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Any

from pydantic import StringConstraints

from app.domain import ContentClass, Frozen, PlaceId, RuntimeConfig
from app.gateway.errors import GatewayError
from app.gateway.openrouter import Credential, TransportRequest, TransportResponse
from app.gateway.policy import GatewayPolicy, check_request
from app.gateway.types import (
    CallRole,
    FinishReason,
    GenerateRequest,
    ModelTurn,
    ToolCall,
    Usage,
)


class FakeTransport:
    """Returns scripted responses (or raises scripted exceptions) in order.

    Records ``TransportRequest`` objects only. It never keeps the credential, so a test
    can assert the key appears nowhere in what was recorded.
    """

    def __init__(self, script: Sequence[TransportResponse | BaseException]) -> None:
        self._script = list(script)
        self.requests: list[TransportRequest] = []
        self.credential_types: list[str] = []

    def send(self, request: TransportRequest, credential: Credential) -> TransportResponse:
        self.requests.append(request)
        self.credential_types.append(type(credential).__name__)
        if not self._script:
            raise AssertionError("FakeTransport script exhausted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeGateway:
    """Scripted ``ModelGateway``. It runs the same request checks as the real adapter, so a
    fake can never accept a request the real gateway would refuse."""

    def __init__(self, script: Sequence[ModelTurn | GatewayError], policy: GatewayPolicy) -> None:
        self._script = list(script)
        self._policy = policy
        self.requests: list[GenerateRequest] = []

    def generate(self, request: GenerateRequest) -> ModelTurn:
        check_request(request, self._policy)
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeGateway script exhausted")
        item = self._script.pop(0)
        if isinstance(item, GatewayError):
            raise item
        return item


# --- provider response builders --------------------------------------------------------
def completion_response(
    content: str | None = None,
    *,
    finish_reason: str | None = "stop",
    prompt_tokens: int | None = 100,
    completion_tokens: int | None = 20,
    total_tokens: int | None = ...,  # type: ignore[assignment]
    tool_calls: list[dict[str, Any]] | None = None,
    provider: str | None = None,
    usage: dict[str, Any] | None = None,
) -> TransportResponse:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "choices": [{"finish_reason": finish_reason, "message": message}],
    }
    if usage is not None:
        body["usage"] = usage
    elif prompt_tokens is not None and completion_tokens is not None:
        total = prompt_tokens + completion_tokens if total_tokens is ... else total_tokens
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            **({"total_tokens": total} if total is not None else {}),
        }
    if provider is not None:
        body["provider"] = provider
    return TransportResponse(status_code=200, body=body)


def tool_call_entry(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


# --- synthetic Grounding Lite shape ----------------------------------------------------
class SyntheticPlace(Frozen):
    place_id: PlaceId  # invented, never a real Google place ID
    note: Annotated[str, StringConstraints(max_length=200)]  # invented text


class SyntheticMapsAnswer(Frozen):
    answer: Annotated[str, StringConstraints(max_length=1_000)]
    places: tuple[SyntheticPlace, ...]
    source_uris: tuple[str, ...]  # reserved .invalid host


def synthetic_maps_answer() -> SyntheticMapsAnswer:
    return SyntheticMapsAnswer(
        answer="Synthetic fixture: two invented quiet spots for a relaxed meal.",
        places=(
            SyntheticPlace(place_id="SYNTH_PLACE_0001", note="Invented bistro, fixture only."),
            SyntheticPlace(place_id="SYNTH_PLACE_0002", note="Invented cafe, fixture only."),
        ),
        source_uris=("https://maps.example.invalid/synthetic/1",),
    )


def synthetic_maps_response(**overrides: Any) -> TransportResponse:
    """A provider-shaped response whose content is the synthetic Maps answer."""
    return completion_response(synthetic_maps_answer().model_dump_json(), **overrides)


def synthetic_maps_turn(config: RuntimeConfig, version_of_prices: str = "placeholder-0") -> ModelTurn:
    """A ready ModelTurn for FakeGateway: call B output, tagged MAPS_GROUNDED."""
    return ModelTurn(
        call_role=CallRole.DISCOVER,
        content_class=ContentClass.MAPS_GROUNDED,
        output=synthetic_maps_answer(),
        finish_reason=FinishReason.STOP,
        usage=Usage(input_tokens=100, output_tokens=20),
        latency_ms=1.0,
        estimated_cost_usd=Decimal(0),
        price_table_version=version_of_prices,
        price_is_placeholder=True,
        model_id=config.model_id,
        provider=config.provider,
        runtime_config_version=config.version,
    )


def scripted_tool_turn(
    config: RuntimeConfig, call: ToolCall, role: CallRole = CallRole.DISCOVER
) -> ModelTurn:
    return ModelTurn(
        call_role=role,
        content_class=(
            ContentClass.MAPS_GROUNDED if role == CallRole.DISCOVER else ContentClass.USER
        ),
        tool_calls=(call,),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=Usage(input_tokens=100, output_tokens=10),
        latency_ms=1.0,
        estimated_cost_usd=Decimal(0),
        price_table_version="placeholder-0",
        price_is_placeholder=True,
        model_id=config.model_id,
        provider=config.provider,
        runtime_config_version=config.version,
    )
