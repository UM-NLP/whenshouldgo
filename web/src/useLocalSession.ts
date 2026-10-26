import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Api, TurnResponse } from "./api/client";
import { createOutbox, type Outbox, type OutboxItem } from "./outbox";
import {
  SCHEMA_VERSION,
  useLocalNotice,
  type ExportBundle,
  type LocalNotice,
  type LocalStore,
  type MessageRecord,
} from "./storage";

const DRAFT_ID = "composer";
const DRAFT_DELAY_MS = 400;

export function useOnline(): boolean {
  const [online, setOnline] = useState(() => (typeof navigator === "undefined" ? true : navigator.onLine !== false));
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return online;
}

export interface LocalSession {
  store: LocalStore;
  notice: LocalNotice;
  online: boolean;
  outbox: Outbox;
  outboxItems: OutboxItem[];
  /** The user's own messages and server-authored replies from earlier sessions, oldest first. */
  restored: MessageRecord[] | null;
  restoredDraft: string | null;
  /** Wired by useChat: where a delivered outbox turn goes. */
  setDeliveryHandler(handler: (item: OutboxItem, response: TurnResponse) => void): void;
  saveMessage(message: { role: "user" | "assistant"; text: string; key?: string; hadDiscovery?: boolean }): void;
  saveDraft(text: string): void;
  exportData(): Promise<ExportBundle>;
  /** Clears everything on the device and in memory. `onCleared` lets the chat drop its items. */
  deleteData(): Promise<void>;
  onCleared(handler: () => void): void;
}

/** Local-first persistence for one chat. Everything degrades to memory-only on any storage error. */
export function useLocalSession(store: LocalStore | null, api: Api): LocalSession | null {
  const notice = useLocalNotice(store);
  const online = useOnline();
  const [outboxItems, setOutboxItems] = useState<OutboxItem[]>([]);
  const [restored, setRestored] = useState<MessageRecord[] | null>(null);
  const [restoredDraft, setRestoredDraft] = useState<string | null>(null);
  const deliver = useRef<(item: OutboxItem, response: TurnResponse) => void>(() => undefined);
  const cleared = useRef<() => void>(() => undefined);
  const seq = useRef(0);
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const accepted = useRef(false);
  accepted.current = notice.state === "accepted";

  const outbox = useMemo(
    () =>
      createOutbox({
        store,
        api,
        onResult: (item, response) => deliver.current(item, response),
        onChange: setOutboxItems,
      }),
    [store, api],
  );

  // Restore once per store: the user's messages, the draft and any queued turns.
  useEffect(() => {
    if (!store) return;
    let live = true;
    void (async () => {
      try {
        const messages = (await store.getAll("messages")).sort((a, b) => a.seq - b.seq);
        const draft = await store.get("drafts", DRAFT_ID);
        seq.current = Math.max(0, ...messages.map((m) => m.seq));
        if (live) {
          setRestored(messages);
          setRestoredDraft(draft?.text ?? "");
        }
      } catch {
        if (live) {
          setRestored([]);
          setRestoredDraft("");
        }
      }
      await outbox.load();
    })();
    return () => {
      live = false;
    };
  }, [store, outbox]);

  // Replay in order when online (same keys), and again when a Retry-After delay ends.
  const itemsKey = outboxItems.map((i) => `${i.key}:${i.state}:${i.notBefore}`).join("|");
  useEffect(() => {
    if (!online || outboxItems.length === 0) return;
    void outbox.flush();
    const at = outbox.nextAttemptAt();
    if (at === null) return;
    const timer = setTimeout(() => void outbox.flush(), Math.max(0, at - Date.now()) + 50);
    return () => clearTimeout(timer);
    // itemsKey captures the relevant parts of outboxItems.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online, itemsKey, outbox]);

  const saveMessage = useCallback<LocalSession["saveMessage"]>(
    (message) => {
      if (!store || !accepted.current) return;
      seq.current += 1;
      const record: MessageRecord = {
        schema_version: SCHEMA_VERSION,
        id: `m-${seq.current}-${Math.random().toString(36).slice(2, 8)}`,
        seq: seq.current,
        role: message.role,
        text: message.text,
        created_at: new Date().toISOString(),
        ...(message.key ? { key: message.key } : {}),
        ...(message.hadDiscovery ? { had_discovery: true } : {}),
      };
      void store.put("messages", record).catch(() => undefined);
    },
    [store],
  );

  const saveDraft = useCallback(
    (text: string) => {
      if (!store || !accepted.current) return;
      if (draftTimer.current) clearTimeout(draftTimer.current);
      const write = () => {
        if (!text.trim()) void store.deleteItem("drafts", DRAFT_ID).catch(() => undefined);
        else void store.put("drafts", { schema_version: SCHEMA_VERSION, id: DRAFT_ID, text, updated_at: new Date().toISOString() }).catch(() => undefined);
      };
      if (text.trim()) draftTimer.current = setTimeout(write, DRAFT_DELAY_MS);
      else write();
    },
    [store],
  );

  return useMemo(() => {
    if (!store) return null;
    return {
      store,
      notice,
      online,
      outbox,
      outboxItems,
      restored,
      restoredDraft,
      setDeliveryHandler: (handler) => {
        deliver.current = handler;
      },
      onCleared: (handler) => {
        cleared.current = handler;
      },
      saveMessage,
      saveDraft,
      exportData: () => store.exportAll(),
      deleteData: async () => {
        if (draftTimer.current) clearTimeout(draftTimer.current);
        await store.deleteAll();
        outbox.reset();
        seq.current = 0;
        notice.reset();
        cleared.current();
      },
    };
  }, [store, notice, online, outbox, outboxItems, restored, restoredDraft, saveMessage, saveDraft]);
}
