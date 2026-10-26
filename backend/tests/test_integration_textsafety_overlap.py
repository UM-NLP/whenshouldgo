"""A number followed by a lowercase word must not hide a later street address."""
from __future__ import annotations

import pytest

from app.textsafety import plain_text_problem

BYPASSES = [
    "CANARYNOTE-zebra-quartz-77 at 123 Queen Street West",  # the reviewed original
    "note 77 at 123 Queen Street",
    "zebra 5 near 12 Main St",
    "77 at 123 Queen Street West",
    "table 4 for 18 Elm Avenue",
    "ref 9 then 1500 Bloor Road",
    "42 by 7 Dundas Street",
    "2 to 456 King Street East",
]

FINE = [
    "30 minute drive",
    "4 people on the way",
    "party of 5 near the park",
    "2 hours drive",
    "15 min walk",
    "table for 4 near the water",
    "rating above 4.5",
    "5 stars preferred",
    "about 20 minutes by transit",
    "trente minutes à pied, 2 enfants",
    "dîner pour 4 personnes près du parc",
    "budget 40 dollars, 3 hours, 2 adults",
    "no more than 3 stops on the way",
]


@pytest.mark.parametrize("text", BYPASSES)
def test_a_later_street_address_is_refused(text: str) -> None:
    assert plain_text_problem(text), text


@pytest.mark.parametrize("text", FINE)
def test_ordinary_numbers_still_pass(text: str) -> None:
    assert not plain_text_problem(text), text
