"""Built-in scripted model for ``run.py --dry-run``: an offline ``httpx.MockTransport`` handler.

It lets the whole harness (real RuntimeConfig, PriceTable, OpenRouterGateway, ResilientGateway
and the real ``HttpxTransport``) run with no network and no key. Replies follow the gold labels
of the fixture whose message was sent, with deterministic faults mixed in (a malformed first
attempt, a truncation, a flipped intent) and two scripted safety/faithfulness failures, so the
scoring report has something to show. Outputs are synthetic and prove nothing about any model.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from typing import Any

import httpx

from app.gateway.openrouter import REPAIR_INSTRUCTION

STATE_PREFIX = "WORKSPACE_STATE_JSON:"
# Scripted failures, by fixture id: a confirmed hard fact on an injection case, and a duration
# used as the area. Everything else is clean unless the hash-driven faults below fire.
SCRIPTED_UNSAFE = frozenset({"inj-001"})
SCRIPTED_BAD_AREA = frozenset({"edge-006"})
FAULT_MALFORMED, FAULT_TRUNCATED, FAULT_FLIPPED = 3, 5, 9  # percent thresholds on a 0..99 hash


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
        repair = messages[-1]["content"] == REPAIR_INSTRUCTION
        if repair:
            messages = messages[:-1]
        message = messages[-1]["content"]
        state = json.loads(messages[1]["content"][len(STATE_PREFIX):])
        with self._lock:
            self.calls += 1
            if repair:
                attempt = self._attempts.get(message, 1)
            else:
                attempt = self._attempts.get(message, 0) + 1
                self._attempts[message] = attempt
        fixture = self._by_message.get(message)
        digest = int(hashlib.sha256(f"{message}#{attempt}".encode()).hexdigest(), 16) % 100
        prompt_tokens = sum(_tokens(m["content"]) for m in messages) + 120  # schema overhead
        finish, content = "stop", self._reply(fixture, state, digest, repair)
        completion = _tokens(content)
        if not repair and digest < FAULT_TRUNCATED and digest >= FAULT_MALFORMED:
            finish, content, completion = "length", content[: len(content) // 3], body["max_tokens"]
        if not repair and digest < FAULT_MALFORMED:
            content = "{not valid json"
            completion = _tokens(content)
        payload = {
            "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                      "total_tokens": prompt_tokens + completion},
            "provider": self._provider,
        }
        return httpx.Response(200, json=payload)

    @staticmethod
    def _reply(fixture: dict[str, Any] | None, state: dict[str, Any], digest: int, repair: bool) -> str:
        patch: dict[str, Any] = {
            "workspace_id": state["workspace_id"], "base_version": state["base_version"],
            "next_action": "ASK", "upsert_facts": [],
        }
        intent = fixture["expected_intent"] if fixture else "OTHER"
        if not repair and FAULT_TRUNCATED <= digest < FAULT_FLIPPED and fixture:
            intent = "OTHER" if intent == "DISCOVERY" else "DISCOVERY"
        if intent == "OTHER":
            return json.dumps({"patch": patch, "intent": "OTHER"})
        patch["next_action"] = "SEARCH"
        groups = fixture["required_query_words"] if fixture else []
        words = [g[0] for g in groups] or ["places"]
        area = fixture["expected_area"] if fixture else None
        language = fixture["expected_language"] if fixture else "en"
        fid = fixture["id"] if fixture else ""
        if fid in SCRIPTED_BAD_AREA:
            area = "30 minute drive"
        query = " ".join(words)[:70]
        patch["upsert_facts"].append({
            "id": "fact-dry-001", "key": "preference", "value": words[0], "source": "USER_PROSE",
            "confirmation": "CONFIRMED" if fid in SCRIPTED_UNSAFE else "UNCONFIRMED",
            "hard": fid in SCRIPTED_UNSAFE, "scope": "trip",
        })
        search: dict[str, Any] = {"text_query": query, "language_code": language}
        if area:
            search["area"] = area
        return json.dumps({"patch": patch, "intent": "DISCOVERY", "tool": "search_places",
                           "search": search}, ensure_ascii=False)
