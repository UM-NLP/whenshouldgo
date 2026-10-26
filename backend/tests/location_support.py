"""Shared builders for the location tests. Holds no tests. Canary coordinates are a point in the
South Atlantic, not a place anyone lives."""
from __future__ import annotations

from typing import Any

from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, canary_payload, frame_turn

from app.frame import SearchArgs

RAW_LAT, RAW_LON = 12.3456, -65.4321
ROUNDED_LAT, ROUNDED_LON = 12.346, -65.432
LOCATION = {"latitude": RAW_LAT, "longitude": RAW_LON}
CANARIES = ("12.3456", "65.4321", "12.346", "65.432")
NO_PLACE_MESSAGE = "Something fun for the kids on Saturday morning"
LOCATION_BIAS = {"circle": {"center": {"latitude": ROUNDED_LAT, "longitude": ROUNDED_LON},
                            "radius_meters": 5000}}
HEADERS_A = {"Authorization": "Bearer tok-a"}
HEADERS_B = {"Authorization": "Bearer tok-b"}


def no_area_turn(config, text_query: str = "fun kids activity", **kw):
    return frame_turn(config, search_args=SearchArgs(text_query=text_query, **kw))


def env_with(*turns, **kw) -> tuple[Env, ScriptedGateway, RecordingClient]:
    config = make_config()
    gateway = ScriptedGateway([t(config) for t in turns], config)
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config, **kw)
    maps.inner._now = env.clock  # noqa: SLF001
    return env, gateway, maps


def grant(env: Env, headers: dict[str, str] = HEADERS_A, **body: Any):
    payload = {"purpose": "LOCATION_FOREGROUND", "notice_version": "location-notice-1", **body}
    return env.client.post("/v1/me/consents", json=payload, headers=headers)


def turn(env: Env, message: str = NO_PLACE_MESSAGE, *, location: Any = LOCATION, key: str = "k1",
         headers: dict[str, str] = HEADERS_A):
    body: dict[str, Any] = {"message": message}
    if location is not None:
        body["location"] = location
    return env.client.post("/v1/agent/turns", json=body,
                           headers={**headers, "Idempotency-Key": key})
