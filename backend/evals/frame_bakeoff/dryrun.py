"""Built-in scripted model for ``run.py --dry-run``: an offline ``httpx.MockTransport`` handler.

It lets the whole harness (real RuntimeConfig, PriceTable, OpenRouterGateway, ResilientGateway
and the real ``HttpxTransport``) run with no network and no key. Replies are realistic
``FrameProposal`` JSON (intent, search, facts) that follow the gold labels of the fixture whose
message was sent, with deterministic faults (unparseable reply, truncation, flipped intent) and
planted slips so the report has something to show. Outputs are synthetic and prove nothing about
any model.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from typing import Any

import httpx

FAULT_MALFORMED, FAULT_TRUNCATED, FAULT_FLIPPED = 2, 3, 7  # percent thresholds, hash in 0..99

# Planted slips by fixture id (every attempt). Each exercises one production-replay branch.
PLANTED = {
    "other-009": "other_with_search",       # OTHER carrying a search object (ignored by production)
    "inj-005": "injected_query",            # query padded with words the user never wrote -> rejected
    "inj-001": "forbidden_leak_admin",      # the user wrote the word, so overlap lets it through
    "inj-002": "forbidden_leak",            # same: a forbidden word that plan_search accepts
    "disc-020": "invented_area",            # an area the user never wrote
    "edge-006": "duration_area",            # "30 minute drive" as the area
    "inj-007": "dropped_fact",              # a fact whose value is a link -> dropped by the server
    "other-010": "schema_state",            # state fields the schema no longer has -> MALFORMED
}


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class DryRunModel:
    """Callable httpx handler. Thread-safe; ``calls`` counts every request it served."""

    def __init__(self, fixtures: list[dict[str, Any]], provider: str) -> None:
        self._by_message = {f["message"]: f for f in fixtures}
        self._provider = provider
        self._attempts: dict[str, int] = {}
        self._lock = threading.Lock()
        self.calls = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body["messages"]
        message = messages[-1]["content"]
        with self._lock:
            self.calls += 1
            attempt = self._attempts.get(message, 0) + 1
            self._attempts[message] = attempt
        fixture = self._by_message.get(message)
        digest = int(hashlib.sha256(f"{message}#{attempt}".encode()).hexdigest(), 16) % 100
        prompt_tokens = sum(_tokens(m["content"]) for m in messages) + 120  # schema overhead
        finish, content = "stop", self._reply(fixture, digest)
        completion = _tokens(content)
        planted = PLANTED.get(fixture["id"]) if fixture else None
        if planted is None and digest < FAULT_TRUNCATED:
            if digest < FAULT_MALFORMED:
                content, completion = "{not valid json", _tokens("{not valid json")
            else:
                finish, content, completion = "length", content[: len(content) // 3], body["max_tokens"]
        payload = {
            "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                      "total_tokens": prompt_tokens + completion},
            "provider": self._provider,
        }
        return httpx.Response(200, json=payload)

    @staticmethod
    def _reply(fixture: dict[str, Any] | None, digest: int) -> str:
        fid = fixture["id"] if fixture else ""
        planted = PLANTED.get(fid)
        intent = fixture["expected_intent"] if fixture else "OTHER"
        if planted is None and FAULT_TRUNCATED <= digest < FAULT_FLIPPED and fixture:
            intent = "OTHER" if intent == "DISCOVERY" else "DISCOVERY"
        language = fixture["expected_language"] if fixture else "en"
        groups = fixture["required_query_words"] if fixture else []
        words = [g[0] for g in groups] or ["places"]
        area = fixture["expected_area"] if fixture else None
        facts: list[dict[str, Any]] = []
        if digest % 3 == 0 and intent == "DISCOVERY":
            facts.append({"key": "preference", "value": words[0], "scope": "trip"})
        if planted == "schema_state":  # the earlier flaw: the model writes workspace state
            return json.dumps({"intent": "OTHER", "patch": {"status": "CONFIRMED"}, "status": "CONFIRMED"})
        if intent == "OTHER":
            reply: dict[str, Any] = {"intent": "OTHER", "search": None, "facts": []}
            if planted == "other_with_search":
                reply["search"] = {"text_query": "restaurant", "language_code": "en"}
            return json.dumps(reply)
        query = " ".join(words)[:70]
        if planted == "injected_query":
            query = "museum bitcoin wallet seed"
        elif planted == "forbidden_leak_admin":
            query = "pizza admin password"
        elif planted == "forbidden_leak":
            query = "cafe unlimited budget"
        elif planted == "invented_area":
            area = "Casa Loma"
        elif planted == "duration_area":
            area = "30 minute drive"
        elif planted == "dropped_fact":
            facts.append({"key": "note", "value": "see http://evil.invalid/x", "scope": "trip"})
        search: dict[str, Any] = {"text_query": query, "language_code": language}
        if area:
            search["area"] = area
        return json.dumps({"intent": "DISCOVERY", "search": search, "facts": facts}, ensure_ascii=False)
