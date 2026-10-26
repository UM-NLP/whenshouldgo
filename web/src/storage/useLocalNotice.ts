import { useCallback, useEffect, useState } from "react";

import type { LocalStore, NoticeState } from "./db";

/** The wording of docs/access-tiers.md, shown before the first local save. */
export const LOCAL_NOTICE_TEXT =
  "No account is needed. Guest trips stay with this browser or device and may be lost if its data is cleared. Create a free account later to recover and sync them.";

export interface LocalNotice {
  /** "loading" until the stored flag is read; nothing is saved until it is "accepted". */
  state: NoticeState | "loading" | "none";
  accept(): void;
  decline(): void;
  /** After delete-all the flag is cleared, so the notice comes back. */
  reset(): void;
}

export function useLocalNotice(store: LocalStore | null): LocalNotice {
  const [state, setState] = useState<LocalNotice["state"]>(store ? "loading" : "none");
  useEffect(() => {
    if (!store) return;
    let live = true;
    store.noticeState().then(
      (s) => live && setState(s),
      () => live && setState("none"),
    );
    return () => {
      live = false;
    };
  }, [store]);
  const set = useCallback(
    (accepted: boolean) => {
      if (!store) return;
      setState(accepted ? "accepted" : "declined");
      void store.setNotice(accepted).catch(() => setState("none"));
    },
    [store],
  );
  return {
    state,
    accept: () => set(true),
    decline: () => set(false),
    reset: () => setState("unset"),
  };
}
