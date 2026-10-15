"""Private authority for tool results and the run ledger.

Only ``client.py`` (and test doubles that import this module on purpose) may use
``AUTHORITY`` or ``mint``. A ``ToolResult`` must carry an ``IssuedToken`` built from it,
and the run ledger can be written only with it. A test scans ``app/`` and fails if any
other module touches these names. Python cannot make this unforgeable; it makes forgery an
explicit, greppable act that the ledger-anchored validator then still has to survive.
"""
from __future__ import annotations

from typing import Any

from .errors import DiscoveryConfigError

AUTHORITY: object = object()


class IssuedToken:
    """Proof that the client produced a result. Opaque: no content, no secrets."""

    __slots__ = ("call_id",)
    call_id: str

    def __init__(self, authority: object, call_id: str) -> None:
        if authority is not AUTHORITY:
            raise DiscoveryConfigError("tool results can only be issued by the discovery client")
        object.__setattr__(self, "call_id", call_id)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("IssuedToken is immutable")

    def __repr__(self) -> str:
        return "IssuedToken(<opaque>)"


def mint(call_id: str) -> IssuedToken:
    return IssuedToken(AUTHORITY, call_id)
