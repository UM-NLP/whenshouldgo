"""Plain-text policy for model-written text that re-enters a prompt or reaches a tool.

Model output is untrusted, and facts and search text come back into the next FRAME prompt, so
anything that could carry hidden characters, markup, a link, instructions through markup, or a
precise location is refused: hidden and control characters (including newlines), angle-bracket
markup, urls, coordinates, street addresses and postal codes.
"""
from __future__ import annotations

import re

from .discovery.text import short_field_problem
from .domain import _COORDINATES, _POSTAL_CODE, _STREET_ADDRESS

_URL = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)")
_MARKUP = re.compile(r"[<>]")


def plain_text_problem(value: str) -> bool:
    """True when ``value`` is not plain, location-free text."""
    return bool(
        short_field_problem(value)
        or _URL.search(value)
        or _MARKUP.search(value)
        or _COORDINATES.search(value)
        or _STREET_ADDRESS.search(value)
        or _POSTAL_CODE.search(value)
    )
