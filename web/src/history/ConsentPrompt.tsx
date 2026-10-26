import { useCallback, useEffect, useState, type ReactNode } from "react";

import { boardFailureText } from "../board/viewModels";
import type { BoardFailure } from "../board/boardApi";
import { NOTICE_VERSIONS } from "./contractAdapter";
import { HISTORY_NOTICE_TEXT, HISTORY_PLACEHOLDER_FLAG, SAVED_PLACE_NOTICE_TEXT, SAVED_PLACE_PLACEHOLDER_FLAG } from "./copy";
import type { ConsentPurpose, Phase1Api } from "./historyApi";

export type ConsentState =
  | { status: "loading" }
  | { status: "granted" }
  | { status: "needed" }
  | { status: "working" }
  | { status: "failed"; failure: BoardFailure };

export interface ConsentControl {
  state: ConsentState;
  grant(): void;
  withdraw(): void;
  /** The server said the consent is not held (for example withdrawn elsewhere): show the prompt again. */
  lost(): void;
}

/** The server's consent ledger for one purpose. Registered users only; guests never call it. */
export function useConsent(api: Phase1Api, purpose: ConsentPurpose, enabled: boolean): ConsentControl {
  const [state, setState] = useState<ConsentState>({ status: "loading" });
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    void api.hasConsent(purpose).then((o) => {
      if (!live) return;
      setState(o.ok ? { status: o.data ? "granted" : "needed" } : { status: "failed", failure: o.failure });
    });
    return () => {
      live = false;
    };
  }, [api, purpose, enabled]);
  const grant = useCallback(() => {
    setState({ status: "working" });
    void api.grantConsent(purpose, NOTICE_VERSIONS[purpose] ?? "").then((o) => {
      setState(o.ok ? { status: "granted" } : { status: "failed", failure: o.failure });
    });
  }, [api, purpose]);
  const withdraw = useCallback(() => {
    setState({ status: "working" });
    void api.withdrawConsent(purpose).then((o) => {
      setState(o.ok ? { status: "needed" } : { status: "failed", failure: o.failure });
    });
  }, [api, purpose]);
  const lost = useCallback(() => setState({ status: "needed" }), []);
  return { state, grant, withdraw, lost };
}

const NOTICES: Partial<Record<ConsentPurpose, { flag: string; text: string; title: string; agree: string }>> = {
  HISTORY_STORE: { flag: HISTORY_PLACEHOLDER_FLAG, text: HISTORY_NOTICE_TEXT, title: "Save my outing history", agree: "Agree and save my history" },
  SAVED_PLACE: { flag: SAVED_PLACE_PLACEHOLDER_FLAG, text: SAVED_PLACE_NOTICE_TEXT, title: "Save places to my account", agree: "Agree and save places" },
};

/** The consent card, or `children` once the purpose is granted. */
export function ConsentGate({ purpose, control, children }: { purpose: ConsentPurpose; control: ConsentControl; children: ReactNode }) {
  const notice = NOTICES[purpose];
  const { state } = control;
  if (state.status === "granted") return <>{children}</>;
  if (state.status === "loading") {
    return (
      <p className="loading" role="status">
        Checking your settings...
      </p>
    );
  }
  if (state.status === "failed") {
    return (
      <p className="notice notice-error" role="alert">
        {boardFailureText(state.failure, "action")}
      </p>
    );
  }
  if (!notice) return null;
  return (
    <div className="notice" role="dialog" aria-label={notice.title}>
      <p className="placeholder-flag">{notice.flag}</p>
      <p>{notice.text}</p>
      <div className="notice-actions">
        <button type="button" onClick={control.grant} disabled={state.status === "working"}>
          {state.status === "working" ? "Working..." : notice.agree}
        </button>
      </div>
    </div>
  );
}
