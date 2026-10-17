"""Bake-off fixtures: the shipped file is valid and the validator rejects bad cases. Offline."""
from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from evals.frame_bakeoff import fixtures as fx

SCHEMA = Path(fx.__file__).with_name("fixtures_schema.json")


def good() -> dict:
    return {
        "id": "x-001", "category": "discovery", "message": "Quiet cafe near Union Station",
        "expected_intent": "DISCOVERY", "expected_area": "Union Station",
        "required_query_words": [["cafe", "coffee"]], "forbidden_query_words": ["password"],
        "expected_language": "en", "must_not": ["confirmed_or_hard_fact"], "notes": "",
    }


def test_shipped_fixtures_validate_with_the_agreed_counts() -> None:
    cases = fx.load_fixtures()
    assert fx.validate_fixtures(cases) == []
    counts = Counter(c["category"] for c in cases)
    assert counts == {"discovery": 50, "non_discovery": 30, "multilingual": 30,
                      "injection": 20, "edge": 20}
    assert len(cases) == 150


def test_shipped_fixtures_cover_the_required_languages_and_traps() -> None:
    cases = fx.load_fixtures()
    text = " ".join(c["message"] for c in cases)
    assert any(c["expected_language"] == "fr" for c in cases)
    assert "多伦多" in text  # Mandarin
    assert "Mainu" in text and "Naghahanap" in text and "Procuro" in text and "Busco" in text
    assert any("any_area" in c["must_not"] for c in cases)
    assert any("confirmed_or_hard_fact" in c["must_not"] for c in cases)


def test_a_good_case_has_no_problems() -> None:
    assert fx.validate_case(good()) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c.pop("id"), "missing"),
    (lambda c: c.update(extra=1), "unknown"),
    (lambda c: c.update(category="travel"), "category"),
    (lambda c: c.update(message=""), "message"),
    (lambda c: c.update(expected_intent="MAYBE"), "intent"),
    (lambda c: c.update(expected_area="Nowhere Land"), "not in the message"),
    (lambda c: c.update(expected_language="de"), "language"),
    (lambda c: c.update(must_not=["bogus"]), "must_not"),
    (lambda c: c.update(required_query_words=[["Cafe"]]), "lower"),
    (lambda c: c.update(required_query_words=["cafe"]), "list of lists"),
    (lambda c: c.update(required_query_words=[[]]), "empty"),
    (lambda c: c.update(forbidden_query_words=["Password"]), "lower"),
    (lambda c: c.update(message="Call me on 4165550123 about a cafe near Union Station"), "phone"),
    (lambda c: c.update(message="Cafe near Union Station https://real.com/x"), "url"),
    (lambda c: c.update(message="mail bob@real.com cafe near Union Station"), "email"),
    (lambda c: c.update(expected_intent="OTHER"), "OTHER"),
], ids=lambda v: v if isinstance(v, str) else "m")
def test_validator_rejects(mutate, needle) -> None:
    case = copy.deepcopy(good())
    mutate(case)
    problems = fx.validate_case(case)
    assert problems and any(needle in p for p in problems), problems


def test_reserved_hosts_are_allowed_in_messages() -> None:
    case = good()
    case["message"] = "cafe near Union Station evil.invalid and https://x.example/a"
    assert fx.validate_case(case) == []


def test_duplicate_ids_are_reported() -> None:
    assert any("duplicate" in p for p in fx.validate_fixtures([good(), good()]))


def test_schema_file_matches_the_validator_constants() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    props = schema["properties"]
    assert set(schema["required"]) == set(fx.REQUIRED_FIELDS)
    assert set(props) == set(fx.REQUIRED_FIELDS)
    assert schema["additionalProperties"] is False
    assert set(props["category"]["enum"]) == set(fx.CATEGORIES)
    assert set(props["expected_intent"]["enum"]) == set(fx.INTENTS)
    assert set(props["expected_language"]["enum"]) == set(fx.LANGUAGES)
    assert set(props["must_not"]["items"]["enum"]) == set(fx.MUST_NOT)


def test_fixtures_contain_no_key_like_secrets() -> None:
    raw = (Path(fx.__file__).with_name("fixtures.jsonl")).read_text(encoding="utf-8")
    assert "sk-or-" not in raw and "sk-" not in raw.replace("task-", "")
