import { useCallback, useEffect, useRef, useState } from "react";

import type { Api, TurnResponse, UsageView } from "./api/client";
import { retryFor, type Failure, type Retry } from "./api/failures";
import { GeoError, type Coordinates, type GeoErrorKind } from "./lib/geo";
import { newIdempotencyKey } from "./lib/idempotency";
import type { LocationConsent } from "./useLocationConsent";
import type { LocalSession } from "./useLocalSession";

export type ChatItem =
  | { id: string; role: "user"; text: string; key?: string }
  | { id: string; role: "assistant"; response: TurnResponse }
  /** A server-authored reply from an earlier session. Its discovery block is never restored. */
  | { id: string; role: "restored"; text: string; hadDiscovery: boolean };

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
  /** Local-first only: put a stopped outbox item back in the queue, or drop it. */
  resumeQueued(key: string, newKey: boolean): void;
  discardQueued(key: string): void;
}

export function useChat(
  api: Api,
  location: LocationConsent,
  onUsage: (usage: UsageView) => void = () => undefined,
  local: LocalSession | null = null,
): Chat {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<PendingProblem | null>(null);
  const pending = useRef<Pending | null>(null);
  const counter = useRef(0);
  const nextId = () => `item-${(counter.current += 1)}`;
  const localRef = useRef(local);
  localRef.current = local;

  const append = useCallback((item: ChatItem) => setItems((current) => [...current, item]), []);
  const addReply = useCallback(
    (response: TurnResponse) => {
      onUsage(response.usage);
      append({ id: nextId(), role: "assistant", response });
      // Only the server-authored reply text is kept; the Google block is not stored.
      if (response.reply) {
        localRef.current?.saveMessage({ role: "assistant", text: response.reply, hadDiscovery: !!response.discovery });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [append, onUsage],
  );

  // Restore the user's own messages (and reply text) once, ahead of anything sent this session.
  const restoredOnce = useRef(false);
  useEffect(() => {
    if (!local?.restored || restoredOnce.current) return;
    restoredOnce.current = true;
    const earlier: ChatItem[] = local.restored.map((m) =>
      m.role === "user"
        ? { id: nextId(), role: "user", text: m.text, ...(m.key ? { key: m.key } : {}) }
        : { id: nextId(), role: "restored", text: m.text, hadDiscovery: m.had_discovery === true },
    );
    setItems((current) => [...earlier, ...current]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [local?.restored]);

  useEffect(() => {
    local?.setDeliveryHandler((_item, response) => addReply(response));
    local?.onCleared(() => {
      setItems([]);
      setProblem(null);
      pending.current = null;
    });
  }, [local, addReply]);

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
        addReply(outcome.data);
        return;
      }
      if (outcome.failure.kind === "location_consent_required") {
        location.markOff();
        turn.location = null;
      }
      setProblem({ type: "api", failure: outcome.failure, retry: retryFor(outcome.failure) });
    },
    [api, location, addReply],
  );

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || sending) return;
      const turn: Pending = { key: newIdempotencyKey(), text: trimmed, location: null };
      append({ id: nextId(), role: "user", text: trimmed, key: turn.key });
      local?.saveMessage({ role: "user", text: trimmed, key: turn.key });
      // Offline, or behind earlier queued turns: queue with the same key; order is preserved.
      if (local && (!local.online || local.outboxItems.length > 0)) {
        void (async () => {
          let coordinates: Coordinates | null = null;
          if (location.phase === "on") {
            // Held in memory only; if it cannot be read the turn is queued without it.
            coordinates = await location.readPosition().catch(() => null);
          }
          await local.outbox.enqueue({ text: trimmed, location: coordinates, key: turn.key });
        })();
        return;
      }
      pending.current = turn;
      void attempt(turn, true);
    },
    [attempt, sending, append, local, location],
  );

  const retry = useCallback(() => {
    const turn = pending.current;
    if (!turn || sending) return;
    // A retry reuses the key (the server replays or reruns safely); an expired result needs a new one.
    if (problem?.type === "api" && problem.retry === "new_key") turn.key = newIdempotencyKey();
    void attempt(turn, true);
  }, [attempt, problem, sending]);

  const resumeQueued = useCallback(
    (key: string, newKey: boolean) => {
      const resumed = local?.outbox.resume(key, { newKey });
      if (resumed) void local?.outbox.flush();
    },
    [local],
  );
  const discardQueued = useCallback((key: string) => void local?.outbox.discard(key), [local]);

  return { items, sending, problem, send, retry, resumeQueued, discardQueued };
}
