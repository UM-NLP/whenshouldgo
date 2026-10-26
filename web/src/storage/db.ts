import {
  ALL_STORES,
  KEY_PATH,
  SCHEMA_VERSION,
  assertWritable,
  type RecordMap,
  type StoreName,
} from "./schema";

export const DB_NAME = "wsg-local";
export const DB_VERSION = 1;
export const DEFAULT_RETENTION_DAYS = 90;
export const MIN_RETENTION_DAYS = 30;
export const MAX_RETENTION_DAYS = 180;
const DAY_MS = 86_400_000;

export type NoticeState = "unset" | "accepted" | "declined";

export class NoticeRequiredError extends Error {
  constructor() {
    super("the saved-on-this-device notice has not been accepted");
    this.name = "NoticeRequiredError";
  }
}

/** Migration stubs, keyed by the version they upgrade TO. Add a step here when DB_VERSION grows. */
const MIGRATIONS: Record<number, (db: IDBDatabase, tx: IDBTransaction) => void> = {
  1: (db) => {
    for (const name of ALL_STORES) db.createObjectStore(name, { keyPath: KEY_PATH[name] });
  },
};

export interface ExportBundle {
  schema_version: number;
  exported_at: string;
  note: string;
  stores: { [S in StoreName]: RecordMap[S][] };
}

export interface LocalStore {
  put<S extends StoreName>(store: S, record: RecordMap[S]): Promise<void>;
  get<S extends StoreName>(store: S, key: string): Promise<RecordMap[S] | undefined>;
  getAll<S extends StoreName>(store: S): Promise<RecordMap[S][]>;
  deleteItem(store: StoreName, key: string): Promise<void>;
  /** Everything stored, as plain data. Needs no notice and no entitlement: always available. */
  exportAll(): Promise<ExportBundle>;
  /** Clears every store, including the notice flag. Always available. */
  deleteAll(): Promise<void>;
  noticeState(): Promise<NoticeState>;
  setNotice(accepted: boolean): Promise<void>;
  retentionDays(): Promise<number>;
  setRetentionDays(days: number): Promise<number>;
  /** Removes local outing_log entries older than the retention period. Returns the count. */
  purgeExpired(now?: number): Promise<number>;
  close(): void;
}

function wrap<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("indexeddb request failed"));
  });
}

function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error("indexeddb transaction failed"));
    tx.onabort = () => reject(tx.error ?? new Error("indexeddb transaction aborted"));
  });
}

export function clampRetention(days: number): number {
  if (!Number.isFinite(days)) return DEFAULT_RETENTION_DAYS;
  return Math.min(MAX_RETENTION_DAYS, Math.max(MIN_RETENTION_DAYS, Math.round(days)));
}

export function createLocalStore(factory: IDBFactory = globalThis.indexedDB, name = DB_NAME): LocalStore {
  let opened: Promise<IDBDatabase> | null = null;

  function open(): Promise<IDBDatabase> {
    if (!opened) {
      opened = new Promise<IDBDatabase>((resolve, reject) => {
        const request = factory.open(name, DB_VERSION);
        request.onupgradeneeded = (event) => {
          const tx = request.transaction;
          if (!tx) return;
          for (let v = event.oldVersion + 1; v <= DB_VERSION; v += 1) MIGRATIONS[v]?.(request.result, tx);
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error ?? new Error("indexeddb open failed"));
        request.onblocked = () => reject(new Error("indexeddb open blocked"));
      }).then(async (db) => {
        await purge(db, Date.now());
        return db;
      });
      opened.catch(() => {
        opened = null;
      });
    }
    return opened;
  }

  async function readMeta(db: IDBDatabase, key: string): Promise<string | number | boolean | undefined> {
    const tx = db.transaction("meta", "readonly");
    const record = (await wrap(tx.objectStore("meta").get(key))) as RecordMap["meta"] | undefined;
    return record?.value;
  }

  async function writeMeta(db: IDBDatabase, key: string, value: string | number | boolean): Promise<void> {
    const record: RecordMap["meta"] = { schema_version: SCHEMA_VERSION, key, value };
    assertWritable("meta", record);
    const tx = db.transaction("meta", "readwrite");
    tx.objectStore("meta").put(record);
    await done(tx);
  }

  async function retention(db: IDBDatabase): Promise<number> {
    const value = await readMeta(db, "retention_days");
    return typeof value === "number" ? clampRetention(value) : DEFAULT_RETENTION_DAYS;
  }

  async function purge(db: IDBDatabase, now: number): Promise<number> {
    const cutoff = now - (await retention(db)) * DAY_MS;
    const tx = db.transaction("outing_log", "readwrite");
    const store = tx.objectStore("outing_log");
    const all = (await wrap(store.getAll())) as RecordMap["outing_log"][];
    let removed = 0;
    for (const entry of all) {
      const at = Date.parse(entry.at);
      // An unparseable timestamp is treated as expired: it cannot be shown to be within retention.
      if (Number.isNaN(at) || at < cutoff) {
        store.delete(entry.id);
        removed += 1;
      }
    }
    await done(tx);
    return removed;
  }

  async function noticeOf(db: IDBDatabase): Promise<NoticeState> {
    const value = await readMeta(db, "local_notice");
    return value === "accepted" || value === "declined" ? value : "unset";
  }

  return {
    async put(store, record) {
      assertWritable(store, record);
      const db = await open();
      // Saving user data on the device waits for the notice (access-tiers.md). Meta is exempt.
      if (store !== "meta" && (await noticeOf(db)) !== "accepted") throw new NoticeRequiredError();
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).put(record);
      await done(tx);
    },
    async get(store, key) {
      const db = await open();
      return (await wrap(db.transaction(store, "readonly").objectStore(store).get(key))) as RecordMap[typeof store] | undefined;
    },
    async getAll(store) {
      const db = await open();
      return (await wrap(db.transaction(store, "readonly").objectStore(store).getAll())) as RecordMap[typeof store][];
    },
    async deleteItem(store, key) {
      const db = await open();
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).delete(key);
      await done(tx);
    },
    async exportAll() {
      const db = await open();
      const tx = db.transaction([...ALL_STORES], "readonly");
      const stores: Record<string, unknown[]> = {};
      for (const s of ALL_STORES) stores[s] = await wrap(tx.objectStore(s).getAll());
      return {
        schema_version: SCHEMA_VERSION,
        exported_at: new Date().toISOString(),
        note: "Place IDs, your labels, your own messages and timestamps. Maps details are not stored; open the place in Maps for them.",
        stores: stores as unknown as ExportBundle["stores"],
      };
    },
    async deleteAll() {
      const db = await open();
      const tx = db.transaction([...ALL_STORES], "readwrite");
      for (const s of ALL_STORES) tx.objectStore(s).clear();
      await done(tx);
    },
    async noticeState() {
      return noticeOf(await open());
    },
    async setNotice(accepted) {
      await writeMeta(await open(), "local_notice", accepted ? "accepted" : "declined");
    },
    async retentionDays() {
      return retention(await open());
    },
    async setRetentionDays(days) {
      const db = await open();
      // The user can shorten (down to 30) but never extend past the 180-day ceiling.
      const value = clampRetention(days);
      await writeMeta(db, "retention_days", value);
      await purge(db, Date.now());
      return value;
    },
    async purgeExpired(now = Date.now()) {
      return purge(await open(), now);
    },
    close() {
      void opened?.then((db) => db.close());
      opened = null;
    },
  };
}

