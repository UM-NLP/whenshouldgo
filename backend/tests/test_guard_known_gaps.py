"""Former false refusals of the placeholder keyword gate, now fixed by the conservative
bilingual guard (docs/agent-evaluation-plan.md: routing >= 99%, false refusals < 1%). Kept as
regression fixtures; the broader corpora live in test_guard_bilingual.py."""
import pytest

from app.domain import DomainClass
from app.guard import classify

FALSE_REFUSALS = [
    ("I want to go to the beach this afternoon", DomainClass.TRAVEL),
    ("Somewhere to hike near Oakville?", DomainClass.TRAVEL),
    ("brunch spot in Leslieville", DomainClass.TRAVEL),
    ("take me to the AGO", DomainClass.TRAVEL),
    ("Kids program at the ROM this afternoon?", DomainClass.TRAVEL),
    ("Is there a dress code at the restaurant tonight?", DomainClass.TRAVEL),
    ("Where is the nearest emergency exit route?", DomainClass.TRAVEL),
]


@pytest.mark.parametrize(("message", "expected"), FALSE_REFUSALS)
def test_outing_requests_are_not_refused(message, expected) -> None:
    assert classify(message) == expected
