"""Private authority for validated answers.

Only ``validator.py`` may use ``mint_validated`` or ``VALIDATED_AUTHORITY``. A ``ValidatedAnswer``
is the only thing ``render_grounded_block`` and ``DisplayCache`` accept, so an answer that did
not pass the validator (or was forged around it) can neither be rendered nor cached. A test scans
``app/`` for any other use of these names. Python cannot make this unforgeable; it makes forgery
an explicit, greppable act, and ``is_validated`` also rejects an object built with ``__new__``.
"""
from __future__ import annotations

from typing import Any

from .contracts import GroundedAnswer
from .errors import DiscoveryConfigError

VALIDATED_AUTHORITY: object = object()


class ValidatedAnswer:
    """A GroundedAnswer that the validator accepted for a principal, with the hashes it saw."""

    __slots__ = ("answer", "principal_id", "run_id", "content_hash", "display_hash", "_sealed")

    def __init__(self, authority: object, answer: GroundedAnswer, principal_id: str) -> None:
        if authority is not VALIDATED_AUTHORITY:
            raise DiscoveryConfigError("answers can only be validated by the grounded-answer validator")
        for name, value in (("answer", answer), ("principal_id", principal_id), ("run_id", answer.run_id),
                            ("content_hash", answer.content_hash), ("display_hash", answer.display_hash),
                            ("_sealed", authority)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ValidatedAnswer is immutable")

    def __repr__(self) -> str:
        return "ValidatedAnswer(<opaque>)"


def is_validated(candidate: object) -> bool:
    """True only for a wrapper the validator minted (an object built with __new__ has no seal)."""
    if not isinstance(candidate, ValidatedAnswer):
        return False
    try:
        return candidate._sealed is VALIDATED_AUTHORITY  # noqa: SLF001
    except AttributeError:
        return False


def mint_validated(answer: GroundedAnswer, principal_id: str) -> ValidatedAnswer:
    return ValidatedAnswer(VALIDATED_AUTHORITY, answer, principal_id)
