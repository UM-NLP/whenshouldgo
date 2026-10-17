"""Shared builders for the composition and smoke tests. Holds no tests. Synthetic only."""
from __future__ import annotations

import json
from typing import Any

from gateway_fakes import completion_response
from maps_simulator import FakeMcpTransport, mcp_ok
from wiring_support import CANARY, canary_payload

from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.composition import GOOGLE_MAPS_KEY_VAR, OPENROUTER_KEY_VAR, build_runtime

OR_CANARY = "sk-or-CANARY-7c1e5d9a42b8f0aa"
GM_CANARY = "AIzaCANARY7c1e5d9a42b8f0aa00"
ENV = {OPENROUTER_KEY_VAR: OR_CANARY, GOOGLE_MAPS_KEY_VAR: GM_CANARY}
TOKEN = "tok-composition"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "k-1"}
MESSAGE = {"message": "Plan a quiet Toronto restaurant this evening"}


def verifier() -> StaticTokenVerifier:
    return StaticTokenVerifier({TOKEN: VerifiedIdentity("guest-comp", "anonymous")})


class ProviderModel:
    """A fake provider that answers FRAME as a DISCOVERY search and names its provider the way
    OpenRouter does (display name), which the production gateway requires."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.credentials: list[str] = []

    def send(self, request: Any, credential: Any) -> Any:
        self.requests.append(request)
        self.credentials.append(credential.reveal())
        answer = {"intent": "DISCOVERY", "search": {"text_query": "quiet restaurant", "area": "Toronto"}}
        return completion_response(json.dumps(answer), prompt_tokens=250, completion_tokens=60,
                                   provider="DeepInfra")


def build(env: dict[str, str] | None = None, **overrides: Any):
    kwargs: dict[str, Any] = dict(
        identity_verifier=verifier(), allow_in_memory_state=True,
        model_transport=ProviderModel(),
        discovery_transport=FakeMcpTransport([mcp_ok(canary_payload())]))
    kwargs.update(overrides)
    return build_runtime(dict(ENV if env is None else env), **kwargs)


__all__ = [n for n in dir() if not n.startswith("_")]
