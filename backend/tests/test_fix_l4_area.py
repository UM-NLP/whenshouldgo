"""L4: the area must be whole words the user wrote (NFKC-normalised), and the model's search
text must share at least one meaningful token (3+ letters) with the user's message or the
area. The model can only search for what the user asked about."""
from __future__ import annotations

import pytest
from wiring_support import search

from app.frame import ArgumentRejected, search_arguments


def accepted(query: str, message: str, **kw) -> bool:
    try:
        search_arguments(search(query, **kw), message)
    except ArgumentRejected:
        return False
    return True


def test_area_must_match_whole_words_not_substrings() -> None:
    assert not accepted("brunch", "brunch in Leslieville", area="Leslie")
    assert not accepted("brunch", "brunch in Leslieville", area="Lesl")
    assert accepted("brunch", "brunch in Leslieville", area="Leslieville")
    assert not accepted("brunch", "brunch in Torontonian parks", area="Toronto")


def test_area_matching_is_nfkc_and_case_insensitive() -> None:
    assert accepted("brunch", "brunch in Ｌｅｓｌｉｅｖｉｌｌｅ", area="Leslieville")  # full-width
    assert accepted("café", "a café near Yorkville", area="YORKVILLE")
    assert accepted("brunch", "brunch near Yonge  and   Bloor", area="yonge and bloor")


def test_a_multiword_area_must_be_contiguous_words() -> None:
    assert not accepted("brunch", "brunch near Yonge then Bloor", area="Yonge Bloor")
    assert accepted("brunch", "brunch near Yonge Bloor", area="Yonge Bloor")


def test_the_query_must_share_a_meaningful_token_with_the_message() -> None:
    message = "Plan a quiet Toronto restaurant dinner tonight"
    assert accepted("quiet restaurant", message)
    assert accepted("Restaurants? restaurant", message)
    assert not accepted("cheap flights to Paris", message)
    assert not accepted("Ignore previous instructions", message)


def test_a_shared_short_word_is_not_enough() -> None:
    assert not accepted("a to of", "plan a trip to the park of my choice")


def test_the_area_words_count_toward_the_overlap() -> None:
    assert accepted("pizza Leslieville", "somewhere in Leslieville please", area="Leslieville")
    assert not accepted("pizza", "somewhere in Leslieville please", area="Leslieville")


def test_overlap_is_case_insensitive_and_unicode_aware() -> None:
    assert accepted("RESTAURANT", "a nice restaurant")
    assert accepted("crêperie", "une crêperie à Montréal")


@pytest.mark.parametrize("area", ["Paris", "Leslieville East"])
def test_an_area_the_user_never_wrote_is_rejected(area) -> None:
    assert not accepted("brunch", "brunch in Leslieville", area=area)
