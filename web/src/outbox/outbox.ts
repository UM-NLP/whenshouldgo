import type { Api, TurnResponse } from "../api/client";
import type { Failure } from "../api/failures";
import type { Coordinates } from "../lib/geo";
import { newIdempotencyKey } from "../lib/idempotency";
import { SCHEMA_VERSION, type LocalStore, type OutboxRecord } from "../storage";

/** Failures worth trying again with the same key: the request did not (or may not have) run. */
const TRANSIENT: ReadonlySet<Failure["kind"]> = new Set(["network", "provider", "busy", "discovery_unavailable"]);
const DEFAULT_RETRY_AFTER_SECONDS = 30;

export interface OutboxItem {
  key: string;
  seq: number;
  text: string;
  createdAt: string;
  /** "stopped" is a typed state the UI surfaces; later items wait behind it, in order. */
  state: "queued" | "stopped";
  stopKind: Failure["kind"] | null;
  /** True when a location was attached this session; it is never persisted. */
  locationDropped: boolean;
  /** Epoch ms before which a rate-limited item is not retried (Retry-After). */
  notBefore: number | null;
}

export interface OutboxDeps {
  store: LocalStore | null;
  api: Pick<Api, "sendTurn">;
  now?: () => number;
  /** Called once per delivered item, before it is removed from the queue. */
  onResult(item: OutboxItem, response: TurnResponse): void;
  onChange?(items: OutboxItem[]): void;
}

export interface Outbox {
  /** Reads persisted items (from a previous session) into the queue. */
  load(): Promise<void>;
  enqueue(input: { text: string; location?: Coordinates | null; key?: string }): Promise<OutboxItem>;
  /** Sends queued items in order with their own keys. Concurrent calls share one run. */
  flush(): Promise<void>;
  items(): OutboxItem[];
  /** Moves a stopped item back to queued; `newKey` for an expired result. */
  resume(key: string, options?: { newKey?: boolean }): OutboxItem | undefined;
  discard(key: string): Promise<void>;
  /** Forgets everything in memory (after delete-all). */
  reset(): void;
  /** Earliest time a rate-limited head item may be tried, or null. */
  nextAttemptAt(): number | null;
}

function toRecord(item: OutboxItem): OutboxRecord {
  // The record has no location field at all: there is nothing to leak.
  return {
    schema_version: SCHEMA_VERSION,
    key: item.key,
    seq: item.seq,
    text: item.text,
    created_at: item.createdAt,
    state: item.state,
    stop_kind: item.stopKind,
    location_dropped: item.locationDropped,
    not_before: item.notBefore,
  };
}

function fromRecord(record: OutboxRecord): OutboxItem {
  return {
    key: record.key,
    seq: record.seq,
    text: record.text,
    createdAt: record.created_at,
    state: record.state,
    stopKind: (record.stop_kind as Failure["kind"] | null) ?? null,
    locationDropped: record.location_dropped,
    notBefore: record.not_before,
  };
}

export function createOutbox(deps: OutboxDeps): Outbox {
  const now = deps.now ?? Date.now;
  let queue: OutboxItem[] = [];
  /** Locations live here only, keyed by idempotency key. Never written anywhere. */
  const locations = new Map<string, Coordinates>();
  let seq = 0;
  let running: Promise<void> | null = null;

  const changed = () => deps.onChange?.(queue.map((i) => ({ ...i })));

  async function persist(item: OutboxItem): Promise<void> {
    try {
      await deps.store?.put("outbox", toRecord(item));
    } catch {
      // No notice accepted, or storage unavailable: the item stays in memory for this session.
    }
  }
  async function unpersist(key: string): Promise<void> {
    try {
      await deps.store?.deleteItem("outbox", key);
    } catch {
      // Best effort; a leftover record is replayed under the same key and is idempotent.
    }
  }

  async function run(): Promise<void> {
    for (;;) {
      const head = queue[0];
      if (!head || head.state === "stopped") return;
      if (head.notBefore !== null && now() < head.notBefore) return;
      const outcome = await deps.api.sendTurn({
        message: head.text,
        idempotencyKey: head.key,
        location: locations.get(head.key) ?? null,
      });
      // The head may have been discarded or reset while the request was in flight.
      if (queue[0]?.key !== head.key) continue;
      if (outcome.ok) {
        queue = queue.slice(1);
        locations.delete(head.key);
        changed();
        try {
          deps.onResult(head, outcome.data);
        } finally {
          await unpersist(head.key);
        }
        continue;
      }
      const failure = outcome.failure;
      if (TRANSIENT.has(failure.kind)) return;
      if (failure.kind === "rate_limited") {
        head.notBefore = now() + (failure.retryAfterSeconds ?? DEFAULT_RETRY_AFTER_SECONDS) * 1000;
      } else {
        head.state = "stopped";
        head.stopKind = failure.kind;
        if (failure.kind === "location_consent_required") locations.delete(head.key);
      }
      changed();
      await persist(head);
      return;
    }
  }

  return {
    async load() {
      let records: OutboxRecord[] = [];
      try {
        records = (await deps.store?.getAll("outbox")) ?? [];
      } catch {
        records = [];
      }
      const known = new Set(queue.map((i) => i.key));
      const loaded = records.filter((r) => !known.has(r.key)).map(fromRecord);
      queue = [...loaded, ...queue].sort((a, b) => a.seq - b.seq);
      seq = Math.max(seq, ...queue.map((i) => i.seq));
      changed();
    },
    async enqueue({ text, location, key }) {
      seq += 1;
      const item: OutboxItem = {
        key: key ?? newIdempotencyKey(),
        seq,
        text,
        createdAt: new Date(now()).toISOString(),
        state: "queued",
        stopKind: null,
        locationDropped: location != null,
        notBefore: null,
      };
      if (location) locations.set(item.key, location);
      queue = [...queue, item];
      changed();
      await persist(item);
      return { ...item };
    },
    flush() {
      if (!running) {
        running = run().finally(() => {
          running = null;
        });
      }
      return running;
    },
    items: () => queue.map((i) => ({ ...i })),
    resume(key, options) {
      const item = queue.find((i) => i.key === key);
      if (!item || item.state !== "stopped") return undefined;
      if (options?.newKey) {
        locations.delete(item.key);
        void unpersist(item.key);
        item.key = newIdempotencyKey();
      }
      item.state = "queued";
      item.stopKind = null;
      item.notBefore = null;
      changed();
      void persist(item);
      return { ...item };
    },
    async discard(key) {
      queue = queue.filter((i) => i.key !== key);
      locations.delete(key);
      changed();
      await unpersist(key);
    },
    reset() {
      queue = [];
      locations.clear();
      changed();
    },
    nextAttemptAt() {
      const head = queue[0];
      return head && head.state === "queued" ? head.notBefore : null;
    },
  };
}
