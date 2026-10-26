import { IDBFactory } from "fake-indexeddb";

import { createLocalStore, type LocalStore } from "../storage";

/** A store on its own empty in-memory IndexedDB. */
export function freshStore(factory: IDBFactory = new IDBFactory()): LocalStore {
  return createLocalStore(factory, "test-db");
}
