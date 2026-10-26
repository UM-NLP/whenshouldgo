"""The fixed "Is where you are now your home?" question (decision 24). Deterministic and server-owned.

The assistant asks only when the USER triggers it: the message references home or work ("near
home", "près de chez moi", "next to the office"). It then offers one FIXED question, authored here,
never by a model, and only when ALL of these hold:

* the message references that label (conservative English/French keyword rules; a negation such as
  "not near home" never matches);
* no saved place of that kind exists;
* the user has not declined it in the last 30 days (``ASK_COOLDOWN``);
* the user has not chosen "don't ask again" for it.

Nothing is inferred from visit patterns (decision 20: that is the Phase 2 pattern feature with its
own consent). The message is read once and never stored or logged; the ask-state (when the user
last declined, or "don't ask again") lives on the device for a guest and in a small typed record
for a registered user (``stores/saved_places_store.py``).
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta

from .domain import SavedPlaceKind, SavedPlacePrompt
from .stores.saved_places_store import AskRecord

ASK_COOLDOWN = timedelta(days=30)  # decision 24: at most once per label per 30 days after a decline
MAX_MESSAGE_CHARS = 4_000
HOME, WORK = SavedPlaceKind.HOME, SavedPlaceKind.WORK

# Server-authored text. English only for now (a localized pair is a product decision).
QUESTIONS: dict[SavedPlaceKind, str] = {
    HOME: "Is where you are now your home? Save it as Home?",
    WORK: "Is where you are now your work? Save it as Work?",
}

_EN_PREP = r"(?:near|nearby|close to|closer to|around|next to|(?:in|within) walking distance (?:of|from|to))"
_FR_PREP = (r"(?:pres|proche|autour|a cote|aux alentours)"
            r" (?:du |de la |de l'|de |d')?")
_HOME_EN = r"(?:my |our )?home\b(?!\s+(?:depot|hardware|improvement|decor|goods|store|stores|sense))"
_WORK_EN = (r"(?:(?:my |our )?(?:work|workplace)\b(?!\s+of\b)"
            r"|(?:my |our |the )?office\b)")
_HOME_FR = r"(?:mon |ma |notre |la |le )?(?:chez moi|maison)\b"
_WORK_FR = r"(?:mon |ma |notre |le |la )?(?:lieu de travail|travail|bureau|boulot)\b"

_PATTERNS: dict[SavedPlaceKind, re.Pattern[str]] = {
    HOME: re.compile(rf"\b{_EN_PREP} {_HOME_EN}|\b{_FR_PREP}{_HOME_FR}"),
    WORK: re.compile(rf"\b{_EN_PREP} {_WORK_EN}|\b{_FR_PREP}{_WORK_FR}"),
}
_NEGATION = re.compile(r"\b(?:not|no|never|nowhere|pas|ne|jamais|sans)\b\W+(?:\w+\W+){0,2}$")


def _normalize(message: str) -> str:
    text = unicodedata.normalize("NFKC", message[:MAX_MESSAGE_CHARS]).replace("’", "'")
    text = "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def referenced_kinds(message: str) -> tuple[SavedPlaceKind, ...]:
    """The labels the message refers to (HOME before WORK). Conservative by design."""
    text = _normalize(message)
    found: list[SavedPlaceKind] = []
    for kind in (HOME, WORK):
        for match in _PATTERNS[kind].finditer(text):
            if _NEGATION.search(text[max(0, match.start() - 24):match.start()]):
                continue
            found.append(kind)
            break
    return tuple(found)


def _cooling_down(state: AskRecord | None, now: datetime) -> bool:
    if state is None:
        return False
    if state.dont_ask_again:
        return True
    return state.declined_at is not None and now < state.declined_at + ASK_COOLDOWN


def decide(
    message: str, *, saved: Collection[SavedPlaceKind],
    states: Mapping[SavedPlaceKind, AskRecord], now: datetime,
) -> SavedPlacePrompt:
    """The prompt for ``message``: ``ask`` is True for at most one label."""
    for kind in referenced_kinds(message):
        if kind in saved or _cooling_down(states.get(kind), now):
            continue
        return SavedPlacePrompt(ask=True, label_kind=kind, question=QUESTIONS[kind])
    return SavedPlacePrompt(ask=False)


__all__ = ["ASK_COOLDOWN", "QUESTIONS", "decide", "referenced_kinds"]
