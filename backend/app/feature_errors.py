"""Typed refusals shared by the dark Phase 1 services (history and saved places, decision 25).

The text is static: never a label, a place ID, a position or a token."""
from __future__ import annotations

from enum import StrEnum


class FeatureFailure(StrEnum):
    DISABLED = "DISABLED"  # history_active is false: nothing is read or written
    GUEST = "GUEST"  # decision 19: a guest's history and saved places stay on the device
    CONSENT_REQUIRED = "CONSENT_REQUIRED"  # HISTORY_STORE or SAVED_PLACE not held
    NOT_FOUND = "NOT_FOUND"  # unknown, expired or another tenant's: indistinguishable
    INVALID = "INVALID"
    CONFLICT = "CONFLICT"  # an idempotency key reused with a different request
    LIMIT = "LIMIT"  # the per-owner cap
    UNAVAILABLE = "UNAVAILABLE"  # the account was deleted while the request was in flight


class FeatureError(Exception):
    def __init__(self, failure: FeatureFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure
