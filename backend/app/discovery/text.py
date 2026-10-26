"""Hidden-character policy. Rejection only: grounded text is never rewritten.

* Short fields (attribution titles, ids, link text): reject every control (Cc), format (Cf:
  bidi overrides and isolates, zero-width characters, BOM, soft hyphen) and surrogate (Cs)
  character.
* The grounded summary: reject surrogates, controls other than newline, tab and carriage
  return, and every format character EXCEPT ZWJ (U+200D) and ZWNJ (U+200C). ZWJ builds
  ordinary emoji sequences and ZWNJ is required to spell Persian and several Indic scripts, so
  rejecting them would reject legitimate text. All bidi controls stay rejected.
"""
from __future__ import annotations

import unicodedata

_SUMMARY_ALLOWED_CONTROLS = frozenset("\n\t\r")
# Invisible fillers that render as nothing (or as a line break) but are not Cc/Cf: Hangul
# fillers, braille blank, combining grapheme joiner, line and paragraph separators.
_INVISIBLE_FILLERS = frozenset("ㅤᅟ⠀͏  ")
_SUMMARY_ALLOWED_FORMAT = frozenset("‌‍")


def short_field_problem(text: str) -> bool:
    return any(unicodedata.category(ch) in ("Cc", "Cf", "Cs") or ch in _INVISIBLE_FILLERS for ch in text)


def summary_problem(text: str) -> bool:
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Cs" or ch in _INVISIBLE_FILLERS:
            return True
        if category == "Cc" and ch not in _SUMMARY_ALLOWED_CONTROLS:
            return True
        if category == "Cf" and ch not in _SUMMARY_ALLOWED_FORMAT:
            return True
    return False


def has_lone_surrogate(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cs" for ch in text)
