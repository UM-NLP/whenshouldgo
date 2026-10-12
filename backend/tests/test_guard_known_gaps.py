"""Known false refusals of the placeholder keyword gate.

These are acceptance fixtures for GUARD-01 (docs/agent-evaluation-plan.md: routing
>= 99%, false refusals < 1%). They are strict xfails: when the classifier is fixed
they start passing and the marker must be removed.
"""
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


@pytest.mark.xfail(strict=True, reason="keyword gate placeholder; fixed by GUARD-01")
@pytest.mark.parametrize(("message", "expected"), FALSE_REFUSALS)
def test_outing_requests_are_not_refused(message, expected) -> None:
    assert classify(message) == expected
