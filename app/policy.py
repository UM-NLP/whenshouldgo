from __future__ import annotations

import re
from dataclasses import dataclass

from .domain import Intent, PrincipalKind

OUT_OF_SCOPE_REPLY = (
    "I can help plan trips and outings. Tell me where you want to go, what you want "
    "to do, or when you need to arrive or return."
)
SAFETY_REPLY = "If there is immediate danger, contact local emergency services. I can help with travel planning once you are safe."


@dataclass(frozen=True)
class Budget:
    message_tokens: int = 2_000
    planner_input_tokens: int = 12_000
    planner_output_tokens: int = 1_200
    run_input_tokens: int = 40_000
    run_output_tokens: int = 4_000
    model_calls: int = 6
    tool_calls: int = 12


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def classify(text: str) -> Intent:
    normalized = text.lower()
    if re.search(r"\b(kill myself|suicide|self-harm|emergency)\b", normalized):
        return Intent.SAFETY
    if re.search(r"\b(code|program|essay|homework|poem|write a story)\b", normalized):
        travel_terms = r"\b(trip|travel|outing|restaurant|drive|route|go to|visit|flight|hotel)\b"
        return Intent.MIXED if re.search(travel_terms, normalized) else Intent.OUT_OF_SCOPE
    travel_terms = r"\b(trip|travel|outing|restaurant|food|eat|drive|route|arrive|return|visit|activity|things to do|hotel|flight|gta|toronto)\b"
    if re.search(travel_terms, normalized):
        return Intent.TRAVEL
    if re.search(r"\b(account|privacy|usage|permission|this app|how does this work)\b", normalized):
        return Intent.APP_HELP
    return Intent.OUT_OF_SCOPE


def tools_for(intent: Intent, kind: PrincipalKind) -> tuple[str, ...]:
    if intent == Intent.TRAVEL:
        return ("maps_grounded_discovery", "route_estimate", "deadline_validator")
    if intent == Intent.APP_HELP:
        return ()
    return ()
