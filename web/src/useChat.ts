import { useCallback, useRef, useState } from "react";

import type { Api, TurnResponse, UsageView } from "./api/client";
import { retryFor, type Failure, type Retry } from "./api/failures";
import { GeoError, type Coordinates, type GeoErrorKind } from "./lib/geo";
import { newIdempotencyKey } from "./lib/idempotency";
import type { LocationConsent } from "./useLocationConsent";

export type ChatItem =
  | { id: string; role: "user"; text: string }
  | { id: string; role: "assistant"; response: TurnResponse };

export type PendingProblem =
  | { type: "api"; failure: Failure; retry: Retry }
  | { type: "geo"; kind: GeoErrorKind };

/** The one send in flight or waiting for a retry. It lives in component memory only. */
interface Pending {
  key: string;
  text: string;
  /** Held in memory so a retry carries the same request; dropped on success or when stopped. */
  location: Coordinates | null;
}

export interface Chat {
  items: ChatItem[];
  sending: boolean;
  problem: PendingProblem | null;
  send(text: string): void;
  retry(): void;
}

export function useChat(
  api: Api,
  location: LocationConsent,
  onUsage: (usage: UsageView) => void = () => undefined,
): Chat {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<PendingProblem | null>(null);
  const pending = useRef<Pending | null>(null);
  const counter = useRef(0);
  const nextId = () => `item-${(counter.current += 1)}`;

  const attempt = useCallback(
    async (turn: Pending, readLocation: boolean) => {
      setSending(true);
      setProblem(null);
      let coordinates = turn.location;
      if (readLocation && location.phase === "on" && coordinates === null) {
        try {
          coordinates = await location.readPosition();
        } catch (error) {
          setSending(false);
          setProblem({ type: "geo", kind: error instanceof GeoError ? error.kind : "unavailable" });
          return;
        }
      }
      turn.location = coordinates;
      const outcome = await api.sendTurn({
        message: turn.text,
        idempotencyKey: turn.key,
        location: coordinates,
      });
      setSending(false);
      if (outcome.ok) {
        pending.current = null;
        onUsage(outcome.data.usage);
        setItems((current) => [...current, { id: nextId(), role: "assistant", response: outcome.data }]);
        return;
      }
      if (outcome.failure.kind === "location_consent_required") {
        location.markOff();
        turn.location = null;
      }
      setProblem({ type: "api", failure: outcome.failure, retry: retryFor(outcome.failure) });
    },
    [api, location, onUsage],
  );

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || sending) return;
      const turn: Pending = { key: newIdempotencyKey(), text: trimmed, location: null };
      pending.current = turn;
      setItems((current) => [...current, { id: nextId(), role: "user", text: trimmed }]);
      void attempt(turn, true);
    },
    [attempt, sending],
  );

  const retry = useCallback(() => {
    const turn = pending.current;
    if (!turn || sending) return;
    // A retry reuses the key (the server replays or reruns safely); an expired result needs a new one.
    if (problem?.type === "api" && problem.retry === "new_key") turn.key = newIdempotencyKey();
    void attempt(turn, true);
  }, [attempt, problem, sending]);

  return { items, sending, problem, send, retry };
}
