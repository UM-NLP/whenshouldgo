"""Versioned price table (docs/model-hosting-decision.md, "Gateway").

Real prices are NOT set. ``placeholder_price_table`` returns zero prices flagged
``placeholder=True``; ``self_check(..., production=True)`` refuses it.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from ..domain import Frozen, Ident, VersionText
from .errors import GatewayConfigError
from .types import Usage

_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.00000001")
UsdAmount = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=8)]


class ModelPrice(Frozen):
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    provider: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    input_usd_per_million: UsdAmount
    output_usd_per_million: UsdAmount


class UnitPrice(Frozen):
    """Per-request price of a non-model unit (Grounding Lite, Places, Routes)."""

    name: Ident
    usd_per_request: UsdAmount


class PriceTable(Frozen):
    schema_version: int = Field(default=1, ge=1)
    kind: str = "price_table"
    version: VersionText
    placeholder: bool  # True until real, reviewed prices are entered
    model_prices: tuple[ModelPrice, ...]
    unit_prices: tuple[UnitPrice, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> PriceTable:
        keys = [(p.model_id, p.provider) for p in self.model_prices]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate model price")
        names = [u.name for u in self.unit_prices]
        if len(names) != len(set(names)):
            raise ValueError("duplicate unit price")
        return self

    def lookup(self, model_id: str, provider: str) -> ModelPrice:
        for price in self.model_prices:
            if price.model_id == model_id and price.provider == provider:
                return price
        raise GatewayConfigError("no price entry for the pinned model and provider")

    def estimate(self, model_id: str, provider: str, usage: Usage) -> Decimal:
        price = self.lookup(model_id, provider)
        cost = (
            price.input_usd_per_million * usage.input_tokens
            + price.output_usd_per_million * usage.output_tokens
        ) / _MILLION
        return cost.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def placeholder_price_table(
    model_id: str, provider: str, version: str = "placeholder-0"
) -> PriceTable:
    """Zero-price table for exactly one (model, provider) pair. It admits nothing else,
    carries ``placeholder=True`` and is refused when the gateway runs with production=True.
    """
    # TODO(pricing): replace with reviewed prices (model, Grounding Lite, Places, Routes)
    # before pilot traffic. Zero prices must never reach production.
    return PriceTable(
        version=version,
        placeholder=True,
        model_prices=(
            ModelPrice(
                model_id=model_id,
                provider=provider,
                input_usd_per_million=Decimal(0),
                output_usd_per_million=Decimal(0),
            ),
        ),
        unit_prices=tuple(
            UnitPrice(name=name, usd_per_request=Decimal(0))
            for name in ("grounding_lite", "places", "routes")
        ),
    )
