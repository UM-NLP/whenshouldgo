export * from "./schema";
export {
  createLocalStore,
  clampRetention,
  NoticeRequiredError,
  DEFAULT_RETENTION_DAYS,
  MAX_RETENTION_DAYS,
  MIN_RETENTION_DAYS,
  type ExportBundle,
  type LocalStore,
  type NoticeState,
} from "./db";
export { LOCAL_NOTICE_TEXT, useLocalNotice, type LocalNotice } from "./useLocalNotice";

import { createLocalStore, type LocalStore } from "./db";

/** The app's store, or null where IndexedDB does not exist (the app then works in memory only). */
export function defaultLocalStore(): LocalStore | null {
  return typeof indexedDB === "undefined" ? null : createLocalStore();
}
