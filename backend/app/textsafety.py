"""Plain-text policy for model-written text that re-enters a prompt or reaches a tool.

Model output is untrusted, and facts and search text come back into the next FRAME prompt, so
anything that could carry hidden characters, markup, a link, or a precise location is refused.

Every detector runs on a NORMALISED copy: NFKC (folds fullwidth and compatibility forms),
combining marks stripped, and a small table of look-alike characters (Cyrillic/Greek letters,
Unicode minus and dots) folded to ASCII, so those tricks do not slip past an ASCII pattern.

False positives matter as much as bypasses: ordinary outing facts ("30 minute drive", "4 people
on the way", "party of 5 near the park") must pass. The street-address rules therefore need a
capitalised street name before an ambiguous suffix (way, place, drive, court ...), reject only
unambiguous suffixes after a lowercase name, and ignore duration and people words.
"""
from __future__ import annotations

import re
import unicodedata

from .discovery.text import short_field_problem

_FOLD = str.maketrans({
    # look-alikes of ASCII letters (Cyrillic, Greek)
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
    "у": "y", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h",
    "Α": "A", "Β": "B", "Ε": "E", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Χ": "X",
    "ο": "o", "α": "a", "ρ": "p", "ν": "v",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    # minus signs, dots and brackets NFKC leaves alone
    "−": "-", "‒": "-", "–": "-", "—": "-", "‐": "-", "‑": "-",
    "。": ".", "｡": ".", "․": ".", "‧": ".", "·": ".",
    "〈": "<", "〉": ">", "〈": "<", "〉": ">", "‹": "<", "›": ">",
    "⟨": "<", "⟩": ">", "《": "<", "》": ">",
    # prime marks used for minutes and seconds
    "′": "'", "″": '"', "’": "'",
})

# Joiners that are legitimate in fact VALUES (emoji sequences, Persian and Indic spelling).
_JOINERS = frozenset("‌‍")
_FILLERS = frozenset("ㅤᅠᅟ⠀͏  ")


def normalize(value: str) -> str:
    """The text the detectors see: NFKC, combining marks removed, look-alikes folded."""
    folded = unicodedata.normalize("NFKC", value).translate(_FOLD)
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFD", folded) if not unicodedata.combining(ch)
    )
    return unicodedata.normalize("NFKC", stripped).translate(_FOLD)


def hidden_problem(value: str, *, allow_joiners: bool = False) -> bool:
    """Hidden, control, bidi, zero-width, format and filler characters. With ``allow_joiners``
    ZWJ and ZWNJ are allowed (fact values: emoji sequences); every other format character,
    every control character (newline included) and every invisible filler is still refused."""
    if not allow_joiners:
        return bool(short_field_problem(value))
    for ch in value:
        if ch in _JOINERS:
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs") or ch in _FILLERS:
            return True
    return False


# --- links, schemes and markup -------------------------------------------------------------------
_TLDS = r"(?:com|ca|org|net|io|ly|gl|app|edu|gov|info|biz|xyz|dev|co)"
_URL = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)")
_SCHEME = re.compile(r"(?i)\b(?:mailto|tel|javascript|data|file|vbscript|sms|geo|blob|ftp):")
_DOMAIN = re.compile(rf"(?i)\b[\w-]+(?:\.[\w-]+)*\.{_TLDS}\b")
_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# A phone number (labels only: see ``label_problem``; place IDs in fact values can hold long digit
# runs): ten or more digits whatever the separators ("+1 (416) 555-0199", "4165550199"), or
# the seven-digit local form with a dash or dot ("555-0199"). Durations, counts and dates ("30
# minutes", "party of 5", "2026-10-26") have far fewer digits in a row and do not match.
_PHONE = re.compile(r"(?<![\d])(?:\+?\d[\s().-]{0,2}){9,}\d(?!\d)|(?<!\d)\d{3}[-.]\d{4}(?!\d)")
_MARKUP = re.compile(r"(?i)[<>]|&(?:lt|gt);|&#0*(?:60|62);|&#x0*3[ce];|%3[ce]")

# --- coordinates ---------------------------------------------------------------------------------
_DEC = re.compile(r"(?<![\w$€£])[+-]?\d{1,3}[.,]\d{2,}")
_DMS = re.compile(r"\d{1,3}\s*°\s*\d{1,2}\s*['\"]")
_LATLON = re.compile(r"(?i)\b(?:lat|lon|lng|long|latitude|longitude)\b\W{0,3}[+-]?\d")
# Two decimal numbers (two or more decimals, comma or point, not after a currency symbol) within
# this many characters of each other read as a coordinate pair, whatever sits between them.
_COORD_GAP = 20

# --- addresses -----------------------------------------------------------------------------------
# Suffixes that are ordinary words after a lowercase noun ("the way", "a place", "a drive") count
# only after a CAPITALISED street name; the unambiguous ones also count after a lowercase name.
_AMBIGUOUS = ("Way", "Place", "Pl", "Drive", "Dr", "Court", "Ct", "Trail", "Circle", "Square",
              "Sq", "Terrace", "Terr", "Parkway", "Pkwy")
_CLEAR = ("Street", "St", "Avenue", "Ave", "Road", "Rd", "Boulevard", "Blvd", "Lane", "Ln",
          "Crescent", "Cres", "Highway", "Hwy")
_SUFFIX = "|".join([*_AMBIGUOUS, *_CLEAR, *(w.lower() for w in _CLEAR)])
_NAME = r"[A-ZÀ-Ý][\w'’-]*"
# Words that make "N <word> <suffix>" a duration, a count of people or a time, never a street.
_NOT_AN_ADDRESS = frozenset({
    "minute", "minutes", "min", "mins", "hour", "hours", "hr", "hrs", "day", "days", "week",
    "weeks", "people", "person", "persons", "adult", "adults", "kid", "kids", "child", "children",
    "guest", "guests", "pm", "am", "km", "mile", "miles", "stop", "stops", "block", "blocks",
    "star", "stars", "seat", "seats", "table", "tables", "bed", "beds", "of", "to", "at", "on",
    "the", "a", "an", "in", "by", "for", "and", "or", "more", "than", "about", "around", "near",
})
# A lookahead, so every number is tried as a start: a candidate that begins earlier ("77 at 123
# Queen Street") must not swallow the address that starts later in the same span.
_NUM_NAME_SUFFIX = re.compile(
    rf"(?=\b\d{{1,6}}[A-Za-z]?\s+((?:[\w'’.-]+\s+){{0,2}}[\w'’.-]+)\s+({_SUFFIX})\b\.?)"
)
_NUM_FRENCH = re.compile(
    rf"\b\d{{1,6}}\s*,?\s+(?i:rue|avenue|boulevard|boul\.?|chemin|route)\s+{_NAME}"
)
_NAME_SUFFIX_NUM = re.compile(
    rf"\b{_NAME}(?:\s+{_NAME})?\s+(?:{'|'.join([*_AMBIGUOUS, *_CLEAR])})\.?\s+#?\d{{1,6}}\b"
    r"(?!\s*(?:hours?|hrs?|min|minutes?|pm|am|people|km|m\b|stars?))"
)

# --- postal codes --------------------------------------------------------------------------------
_POSTAL = re.compile(r"\b[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d\b")
_PLACE_WORDS = (
    r"(?:toronto|ontario|ont|gta|mississauga|brampton|markham|vaughan|oakville|burlington|"
    r"scarborough|etobicoke|north york|ottawa|hamilton|montreal|montréal|quebec|qc|canada)"
)
# A bare forward sortation area (M5V) counts only when attached to a place word.
_FSA_WITH_PLACE = re.compile(rf"(?i)\b{_PLACE_WORDS}\b[\s,]+[A-Za-z]\d[A-Za-z]\b")
_FSA_WITH_PROVINCE = re.compile(r"\bON\b[\s,]+[A-Za-z]\d[A-Za-z]\b")


def _address_problem(text: str) -> bool:
    for match in _NUM_NAME_SUFFIX.finditer(text):
        words = match.group(1).split()
        if any(w.strip(".,'").casefold() in _NOT_AN_ADDRESS for w in words):
            continue
        suffix = match.group(2)
        if suffix in _AMBIGUOUS and not all(w[:1].isupper() for w in words):
            continue  # "a 5 minute drive", "4 people on the way": ordinary words
        return True
    return bool(_NUM_FRENCH.search(text) or _NAME_SUFFIX_NUM.search(text))


def _coordinate_problem(text: str) -> bool:
    numbers = list(_DEC.finditer(text))
    for first, second in zip(numbers, numbers[1:], strict=False):
        if second.start() - first.end() <= _COORD_GAP:  # whatever sits between them
            return True
    return bool(_DMS.search(text) or _LATLON.search(text))


def plain_text_problem(value: str, *, allow_joiners: bool = False) -> bool:
    """True when ``value`` is not plain, location-free text. ``allow_joiners`` lets ZWJ/ZWNJ
    through (fact values only); every other hidden or format character is still refused."""
    if hidden_problem(value, allow_joiners=allow_joiners):
        return True
    text = normalize(value)
    return bool(
        _MARKUP.search(text)
        or _URL.search(text)
        or _SCHEME.search(text)
        or _EMAIL.search(text)
        or _DOMAIN.search(text)
        or _coordinate_problem(text)
        or _address_problem(text)
        or _POSTAL.search(text)
        or _FSA_WITH_PLACE.search(text)
        or _FSA_WITH_PROVINCE.search(text)
    )


def label_problem(value: str) -> bool:
    """``plain_text_problem`` plus phone numbers: the policy for a label the user types and the
    server stores or shows back (a saved place, a reminder, an outing, a plan destination)."""
    return plain_text_problem(value) or bool(_PHONE.search(normalize(value)))
