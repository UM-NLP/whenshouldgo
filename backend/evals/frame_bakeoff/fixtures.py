"""Loader and validator for the FRAME bake-off fixtures (``fixtures.jsonl``).

Every case is synthetic and hand-written: no real person, address, phone number or Maps
content (docs/agent-evaluation-plan.md, "Evaluation data rule"). The validator enforces the
format in ``fixtures_schema.json`` and a few synthetic-data checks. Offline, stdlib only.

Gold-label semantics (also used by ``score.py``):

* ``expected_intent``: DISCOVERY or OTHER.
* ``expected_area``: text that appears in the message (compared case-folded), or null. It
  scores *area extraction*; *faithfulness* only needs the model's area to be null or in the message.
* ``required_query_words``: a list of groups. EVERY group must be satisfied; a group is
  satisfied when ANY of its (lower-cased) alternatives occurs in the final search query. An
  alternative made of ASCII letters matches whole words; anything else matches as a substring.
* ``forbidden_query_words``: lower-cased words that must not occur in the final search query.
* ``must_not``: extra invariants, see ``MUST_NOT``.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_FIXTURES = HERE / "fixtures.jsonl"

REQUIRED_FIELDS = (
    "id", "category", "message", "expected_intent", "expected_area", "required_query_words",
    "forbidden_query_words", "expected_language", "must_not", "notes",
)
CATEGORIES = ("discovery", "non_discovery", "multilingual", "injection", "edge")
INTENTS = ("DISCOVERY", "OTHER")
LANGUAGES = ("en", "fr")
MUST_NOT = (
    "confirmed_or_hard_fact",  # a fact upserted with confirmation CONFIRMED or hard true
    "non_search_tool",         # any tool other than search_places
    "search_when_other",       # search arguments on a message that is not a discovery request
    "any_area",                # the model must give no area at all (duration, party size, date...)
)
MAX_MESSAGE_CHARS = 8_000  # the product rejects inputs over 2,000 tokens (about 8,000 characters)

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_URL = re.compile(r"(?:https?://|www\.)([^\s/:?#]+)", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_PHONE = re.compile(r"\d{3}[\s.-]\d{3}[\s.-]\d{4}|\d{7,}")
_RESERVED_HOST_ENDINGS = (".invalid", ".example", ".test", ".localhost")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _reserved(host: str) -> bool:
    return host.lower().rstrip(".").endswith(_RESERVED_HOST_ENDINGS)


def _word_list_problems(name: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(w, str) for w in value):
        return [f"{name} must be a list of strings"]
    return [f"{name} entries must be non-empty and lower case" for w in value
            if not w or w != w.lower()][:1]


def validate_case(case: Any) -> list[str]:
    """Problems with one case (an empty list means valid). Messages never echo the case text."""
    if not isinstance(case, dict):
        return ["case must be a JSON object"]
    problems: list[str] = []
    missing = [f for f in REQUIRED_FIELDS if f not in case]
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    unknown = sorted(set(case) - set(REQUIRED_FIELDS))
    if unknown:
        problems.append(f"unknown fields: {', '.join(unknown)}")
    if missing:
        return problems
    if not isinstance(case["id"], str) or not _ID.fullmatch(case["id"]):
        problems.append("id must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    if case["category"] not in CATEGORIES:
        problems.append(f"category must be one of {CATEGORIES}")
    message = case["message"]
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
        problems.append(f"message must be a non-empty string of at most {MAX_MESSAGE_CHARS} chars")
        message = ""
    if case["expected_intent"] not in INTENTS:
        problems.append(f"expected_intent must be one of {INTENTS}")
    if case["expected_language"] not in LANGUAGES:
        problems.append(f"expected_language must be one of {LANGUAGES}")
    area = case["expected_area"]
    if area is not None:
        if not isinstance(area, str) or not area.strip():
            problems.append("expected_area must be a non-empty string or null")
        elif message and _fold(area) not in _fold(message):
            problems.append("expected_area is not in the message")
    groups = case["required_query_words"]
    if not isinstance(groups, list) or not all(isinstance(g, list) for g in groups):
        problems.append("required_query_words must be a list of lists")
    else:
        for group in groups:
            if not group:
                problems.append("required_query_words has an empty group")
            else:
                problems.extend(_word_list_problems("required_query_words", group))
    problems.extend(_word_list_problems("forbidden_query_words", case["forbidden_query_words"]))
    must = case["must_not"]
    if not isinstance(must, list) or any(m not in MUST_NOT for m in must):
        problems.append(f"must_not entries must be among {MUST_NOT}")
    if not isinstance(case["notes"], str):
        problems.append("notes must be a string")
    if case["expected_intent"] == "OTHER" and (area is not None or groups):
        problems.append("an OTHER case has no expected_area and no required_query_words")
    if message:
        if _PHONE.search(message):
            problems.append("message looks like it holds a phone number")
        if any(not _reserved(h) for h in _URL.findall(message)):
            problems.append("message holds a url outside the reserved .invalid/.example hosts")
        if any(not _reserved(h) for h in _EMAIL.findall(message)):
            problems.append("message holds an email address outside the reserved hosts")
    return problems


def validate_fixtures(cases: list[Any]) -> list[str]:
    """Problems across a fixture set, each prefixed with the case id or position."""
    problems: list[str] = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        label = case.get("id", f"#{index}") if isinstance(case, dict) else f"#{index}"
        problems.extend(f"{label}: {p}" for p in validate_case(case))
        if isinstance(case, dict) and isinstance(case.get("id"), str):
            if case["id"] in seen:
                problems.append(f"{label}: duplicate id")
            seen.add(case["id"])
    return problems


def load_fixtures(path: str | Path | None = None, *, validate: bool = True) -> list[dict[str, Any]]:
    """Read a JSONL fixture file. Raises ``ValueError`` (static text) on a malformed line or an
    invalid set when ``validate`` is true."""
    source = Path(path) if path else DEFAULT_FIXTURES
    cases: list[dict[str, Any]] = []
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError:
            raise ValueError(f"{source.name} line {number} is not valid JSON") from None
    if validate:
        problems = validate_fixtures(cases)
        if problems:
            raise ValueError("invalid fixtures: " + "; ".join(problems[:5]))
    return cases


def category_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Validate the bake-off fixtures.")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_FIXTURES))
    args = parser.parse_args(argv)
    cases = load_fixtures(args.path, validate=False)
    problems = validate_fixtures(cases)
    for problem in problems:
        print(problem)
    print(f"{len(cases)} cases, {len(problems)} problems, counts={category_counts(cases)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
