"""Review findings 15 and 16: a reviewed Places price table needs positive prices, and Places needs
a dollar cap on the pilot budget."""
from __future__ import annotations

from decimal import Decimal

import pytest
from composition_support import build
from pydantic import ValidationError

from app import composition
from app.composition import ConfigurationError
from app.places import UNREVIEWED_PRICE_TABLE, PlacesPriceTable


def table(details: str, routes: str, reviewed: bool = True) -> PlacesPriceTable:
    return PlacesPriceTable(
        version="t", reviewed=reviewed, details_sku="d", routes_sku="r",
        details_usd_per_request=Decimal(details), routes_usd_per_request=Decimal(routes))


@pytest.mark.parametrize("details,routes", [("0", "0.005"), ("0.01", "0"), ("0", "0")])
def test_a_reviewed_table_rejects_zero_prices(details: str, routes: str) -> None:
    with pytest.raises(ValidationError):
        table(details, routes)


def test_negative_prices_stay_rejected() -> None:
    with pytest.raises(ValidationError):
        table("-0.01", "0.005")


def test_a_positive_reviewed_table_and_the_unreviewed_default_are_fine() -> None:
    assert table("0.01", "0.005").reviewed is True
    assert UNREVIEWED_PRICE_TABLE.reviewed is False
    assert table("0", "0", reviewed=False).reviewed is False


def test_places_enabled_with_a_token_only_budget_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="PILOT_SPEND_MAX_USD"):
        build(places_price_table=table("0.01", "0.005"),
              spend_budget=composition.SpendBudget(max_tokens=1_000_000))


def test_places_enabled_with_no_budget_at_all_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="PILOT_SPEND_MAX_USD"):
        build(places_price_table=table("0.01", "0.005"))


def test_places_enabled_with_a_dollar_cap_builds() -> None:
    runtime = build(places_price_table=table("0.01", "0.005"),
                    spend_budget=composition.SpendBudget(max_usd=Decimal("50")))
    assert runtime.places_service.enabled is True


def test_a_token_only_budget_is_fine_while_places_is_off() -> None:
    runtime = build(spend_budget=composition.SpendBudget(max_tokens=1_000_000))
    assert runtime.places_service.enabled is False
