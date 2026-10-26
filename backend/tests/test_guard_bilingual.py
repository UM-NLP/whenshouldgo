"""The bilingual conservative domain guard: corpora accuracy and property tests.

The guard is a deterministic PRE-FILTER, not an allowlist of travel words: it refuses only
clearly off-topic messages, because the model never writes reply text in Phase 0 (a miss goes to
FRAME, which can only emit a patch and typed search arguments).
"""
from __future__ import annotations

import itertools
import unicodedata

import pytest
from guard_corpora import (
    ADVERSARIAL,
    APP_HELP_EN,
    APP_HELP_FR,
    MIXED_EN,
    MIXED_FR,
    OFF_TOPIC_EN,
    OFF_TOPIC_FR,
    SAFETY_EN,
    SAFETY_FR,
    TRAVEL_EN,
    TRAVEL_FR,
)

from app.domain import DomainClass
from app.guard import classify, estimate_tokens

D = DomainClass


def misses(messages, expected):
    return [m for m in messages if classify(m) != expected]


def test_corpus_sizes_meet_the_minimums() -> None:
    assert len(TRAVEL_EN) >= 60 and len(TRAVEL_FR) >= 60
    assert len(OFF_TOPIC_EN) >= 30 and len(OFF_TOPIC_FR) >= 30
    assert len(SAFETY_EN) + len(SAFETY_FR) >= 20
    assert len(APP_HELP_EN) + len(APP_HELP_FR) >= 20
    assert len(MIXED_EN) + len(MIXED_FR) >= 30
    assert len(ADVERSARIAL) >= 20


@pytest.mark.parametrize("corpus", [TRAVEL_EN, TRAVEL_FR], ids=["en", "fr"])
def test_outing_messages_route_to_travel(corpus) -> None:
    assert misses(corpus, D.TRAVEL) == []


@pytest.mark.parametrize("corpus", [OFF_TOPIC_EN, OFF_TOPIC_FR], ids=["en", "fr"])
def test_clearly_off_topic_messages_are_out_of_scope(corpus) -> None:
    assert misses(corpus, D.OUT_OF_SCOPE) == []


@pytest.mark.parametrize("corpus", [SAFETY_EN, SAFETY_FR], ids=["en", "fr"])
def test_safety_messages_route_to_safety(corpus) -> None:
    assert misses(corpus, D.SAFETY) == []


@pytest.mark.parametrize("corpus", [APP_HELP_EN, APP_HELP_FR], ids=["en", "fr"])
def test_app_help_messages_route_to_app_help(corpus) -> None:
    assert misses(corpus, D.APP_HELP) == []


@pytest.mark.parametrize("corpus", [MIXED_EN, MIXED_FR], ids=["en", "fr"])
def test_mixed_messages_are_never_refused_and_continue_as_travel(corpus) -> None:
    assert misses(corpus, D.TRAVEL) == []


def test_adversarial_messages_route_as_expected() -> None:
    wrong = [(m, e, classify(m)) for m, e in ADVERSARIAL if classify(m) != D[e]]
    assert wrong == []


def test_false_refusal_rate_on_supported_outings_is_zero() -> None:
    supported = TRAVEL_EN + TRAVEL_FR + MIXED_EN + MIXED_FR
    refused = [m for m in supported if classify(m) in (D.OUT_OF_SCOPE, D.SAFETY)]
    assert refused == []


# --- property-style tests ------------------------------------------------------------------
ALL = (TRAVEL_EN + TRAVEL_FR + OFF_TOPIC_EN + OFF_TOPIC_FR + SAFETY_EN + SAFETY_FR
       + APP_HELP_EN + APP_HELP_FR + MIXED_EN + MIXED_FR)


def test_case_invariance() -> None:
    bad = [m for m in ALL if classify(m.upper()) != classify(m) or classify(m.lower()) != classify(m)]
    assert bad == []


def test_nfkc_invariance() -> None:
    def fullwidth(text: str) -> str:
        return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)
    bad = [m for m in ALL if classify(fullwidth(m)) != classify(m)]
    assert bad == []


def test_accent_folding_invariance() -> None:
    def strip(text: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    bad = [m for m in ALL if classify(strip(m)) != classify(m)]
    assert bad == []


def test_zero_width_characters_do_not_change_the_route() -> None:
    bad = [m for m in ALL if classify("​".join(m)) != classify(m)]
    assert bad == []


PLACE_NAMES = [
    "Code Street Cafe", "Shakespeare Theatre", "Math Museum", "Homework Cafe", "Python Pub",
    "The Essay Room", "Poem Bakery", "Hack Space Bar", "Crypto Lounge", "Joke Shop Cafe",
    "Translate Bistro", "Story Books Cafe", "Therapy Coffee", "Prompt Bar", "Summary Diner",
    "Legal Seafood", "Medical Arts Cafe", "Algebra Pizza", "Le Café du Code",
    "Théâtre Shakespeare", "Musée des Maths", "Bistro Devoirs", "Poème Café",
]
TEMPLATES = [
    "Take me to {}", "Find a table at {}", "brunch near {}", "I want to go to {} this afternoon",
    "Is {} open tonight?", "Where can we eat close to {}?", "Je veux aller au {}",
    "emmène-moi à {}", "un café près de {}", "Trouve-moi une table à {} ce soir",
]


@pytest.mark.parametrize("place", PLACE_NAMES)
def test_an_off_topic_word_inside_a_place_name_never_refuses_the_outing(place) -> None:
    refused = [t.format(place) for t in TEMPLATES if classify(t.format(place)) != D.TRAVEL]
    assert refused == []


def test_vague_short_and_unusual_messages_default_to_travel() -> None:
    for message in ["ok", "hmm", "Saturday", "près du lac", "downtown?", "3 people", "oui", "yes please"]:
        assert classify(message) == D.TRAVEL


def test_empty_and_whitespace_do_not_crash() -> None:
    assert classify("") in set(D) and classify("   \n\t") in set(D)


def test_token_estimate_and_size_limit_semantics_are_kept() -> None:
    assert estimate_tokens("") == 1 and estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2


def test_safety_wins_over_travel_and_off_topic() -> None:
    assert classify("I want to kill myself, also find a pizza place") == D.SAFETY
    assert classify("Write Python code, I can't breathe") == D.SAFETY
    assert classify("Where is the nearest emergency exit route?") == D.TRAVEL


@pytest.mark.parametrize(("a", "b"), list(itertools.islice(zip(TRAVEL_EN, TRAVEL_FR), 5)))
def test_classification_is_a_pure_function(a, b) -> None:
    assert classify(a) == classify(a) and classify(b) == classify(b)


HELD_OUT_OUTINGS = [
    "Plan a day trip to Niagara Falls", "what's the weather like for a walk today", "I'm bored",
    "Where can I find a good massage therapist near me", "Can I bring my dog on the TTC",
    "Who is open late near me", "Is there a fire pit at Woodbine Beach",
    "Quel temps fera-t-il pour une randonnée samedi",
    "Je cherche un resto avec une bonne politique de confidentialité",
    "Où est le meilleur endroit pour voir le feu d'artifice",
    "Je veux voir un film ce soir au cinéma", "How much is parking near the ROM",
    "what is 2 hours from Toronto by car", "Can you recommend a good book cafe",
]


@pytest.mark.parametrize("message", HELD_OUT_OUTINGS)
def test_held_out_outing_phrasings_are_not_refused(message) -> None:
    assert classify(message) == D.TRAVEL
