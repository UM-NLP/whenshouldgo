"""Travel-domain gate (docs/voice-and-domain-controls.md).

The keyword classifier is a deterministic placeholder. GUARD-01 replaces it with the
bounded classifier call; tests/test_guard_known_gaps.py records its false refusals.
"""
from __future__ import annotations

import re

from .domain import DomainClass, PrincipalKind

OUT_OF_SCOPE_REPLY = (
    "I can help plan trips and outings. Tell me where you want to go, what you want "
    "to do, or when you need to arrive or return."
)
SAFETY_REPLY = "If there is immediate danger, contact local emergency services. I can help with travel planning once you are safe."


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def classify(text: str) -> DomainClass:
    normalized = text.lower()
    if re.search(r"\b(kill myself|suicide|self-harm|emergency)\b", normalized):
        return DomainClass.SAFETY
    if re.search(r"\b(code|program|essay|homework|poem|write a story)\b", normalized):
        travel_terms = r"\b(trip|travel|outing|restaurant|drive|route|go to|visit|flight|hotel)\b"
        return DomainClass.MIXED if re.search(travel_terms, normalized) else DomainClass.OUT_OF_SCOPE
    travel_terms = r"\b(trip|travel|outing|restaurant|food|eat|drive|route|arrive|return|visit|activity|things to do|hotel|flight|gta|toronto)\b"
    if re.search(travel_terms, normalized):
        return DomainClass.TRAVEL
    if re.search(r"\b(account|privacy|usage|permission|this app|how does this work)\b", normalized):
        return DomainClass.APP_HELP
    return DomainClass.OUT_OF_SCOPE


def tools_for(domain: DomainClass, kind: PrincipalKind) -> tuple[str, ...]:
    if domain == DomainClass.TRAVEL:
        return ("maps_grounded_discovery", "route_estimate", "deadline_validator")
    return ()
