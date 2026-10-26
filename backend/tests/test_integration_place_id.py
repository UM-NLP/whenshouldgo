"""``DiscoveryPlace.place_id``: the id in the response is exactly the ledger's, and Google's text and
attribution stay byte-identical (the display hash binds the id, text and links together)."""
from __future__ import annotations

from wiring_support import canary_payload, happy_env

from app.discovery.client import payload_ledger_info
from app.discovery.contracts import display_hash, extract_search_places
from app.discovery.ids import ToolId


def test_the_response_place_ids_equal_the_ledgers_in_order() -> None:
    env, _, _ = happy_env()
    response = env.post()
    assert response.status_code == 200
    block = response.json()["discovery"]
    payload = canary_payload()
    text, fields, _ = extract_search_places(payload)
    ledger = payload_ledger_info(ToolId.SEARCH_PLACES, payload)

    shown = [p["place_id"] for p in block["places"]]
    assert shown == [f[0] for f in fields]  # same ids, same order
    assert set(shown) == set(ledger.place_ids)
    # the verbatim text and each source's title and links are exactly Google's
    assert block["text"] == text
    assert [(p["place_id"], p["title"], p["attribution_url"], p["place_url"])
            for p in block["places"]] == [tuple(f) for f in fields]
    # and the display hash the ledger recorded binds those very fields
    assert display_hash(block["text"], [
        (p["place_id"], p["title"], p["attribution_url"], p["place_url"])
        for p in block["places"]]) == ledger.display_hash
