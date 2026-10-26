"""L2: ``deep_text`` is the leak detector, so it must find a canary in every kind of carrier.
This self-test plants one in each and fails if the detector misses it."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

import pytest
from pydantic import BaseModel, Field
from wiring_support import deep_text

CANARY = "CARRIER-CANARY-42"


class Hidden(BaseModel):
    shown: str = ""
    secret: str = Field(default="", repr=False)


class Flavor(enum.Enum):
    LEAK = CANARY


class Plain:
    def __init__(self) -> None:
        self.inner = {"k": [CANARY]}


class Slotted:
    __slots__ = ("a",)

    def __init__(self) -> None:
        self.a = CANARY


@dataclass
class Holder:
    items: tuple


def chained() -> BaseException:
    try:
        try:
            raise ValueError(CANARY)
        except ValueError as cause:
            raise RuntimeError("outer") from cause
    except RuntimeError as outer:
        return outer


def contexted() -> BaseException:
    try:
        try:
            raise ValueError(CANARY)
        except ValueError:
            raise RuntimeError("outer")  # noqa: B904 - the implicit __context__ is the point
    except RuntimeError as outer:
        return outer


CARRIERS = {
    "str": CANARY,
    "bytes": CANARY.encode(),
    "bytearray": bytearray(CANARY.encode()),
    "enum value": Flavor.LEAK,
    "exception args": ValueError(CANARY),
    "exception __cause__": chained(),
    "exception __context__": contexted(),
    "plain object __dict__": Plain(),
    "slotted object": Slotted(),
    "repr-hidden pydantic field": Hidden(secret=CANARY),
    "dict key": {CANARY: 1},
    "set member": {CANARY},
    "dataclass in tuple": Holder((Holder((CANARY,)),)),
    "int-keyed nesting": {1: {2: [(3, CANARY)]}},
}


@pytest.mark.parametrize("name", list(CARRIERS))
def test_deep_text_finds_the_canary_in_every_carrier(name) -> None:
    assert CANARY in deep_text(CARRIERS[name]), name


def test_deep_text_finds_numbers_in_decimal() -> None:
    assert "42" in deep_text(Decimal("0.0042"))


def test_deep_text_survives_cycles() -> None:
    loop: list = []
    loop.append(loop)
    loop.append(CANARY)
    assert CANARY in deep_text(loop)
    node = Plain()
    node.inner = {"self": node, "leak": CANARY}
    assert CANARY in deep_text(node)
