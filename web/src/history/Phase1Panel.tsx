import { useState } from "react";

import type { BoardApi } from "../board/boardApi";
import { SavedPlaceFlowView } from "../places/SavedPlaceFlowView";
import { SavedPlaces } from "../places/SavedPlaces";
import type { LocalStore } from "../storage";
import { ConsentGate, useConsent } from "./ConsentPrompt";
import { HistoryView } from "./HistoryView";
import { RetentionPicker } from "./RetentionPicker";
import { HISTORY_HEADING } from "./copy";
import type { Phase1Api } from "./historyApi";
import type { Phase1 } from "./usePhase1";

function ServerHistory({ p1, api, boardApi }: { p1: Phase1; api: Phase1Api; boardApi: BoardApi }) {
  const consent = useConsent(api, "HISTORY_STORE", true);
  return (
    <ConsentGate purpose="HISTORY_STORE" control={consent}>
      {p1.history ? <HistoryView repo={p1.history} boardApi={boardApi} version={p1.version} onChanged={p1.bump} /> : null}
      <div className="notice-actions">
        <button type="button" className="secondary" onClick={consent.withdraw}>
          Stop saving my history
        </button>
      </div>
    </ConsentGate>
  );
}

/**
 * History and saved places. Rendered only when the server's history_enabled capability is true (the
 * caller passes a non-null `p1` only then). Collapsed by default; nothing loads until it is opened.
 */
export function Phase1Panel({ p1, api, boardApi, store }: { p1: Phase1; api: Phase1Api; boardApi: BoardApi; store: LocalStore | null }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="board phase1" aria-label={HISTORY_HEADING}>
      <h2>{HISTORY_HEADING}</h2>
      {open ? null : (
        <button type="button" className="secondary" onClick={() => setOpen(true)}>
          Show my history and saved places
        </button>
      )}
      {open ? (
        <>
          <RetentionPicker mode={p1.mode} store={store} onChanged={p1.bump} />
          {p1.mode === "server" ? (
            <ServerHistory p1={p1} api={api} boardApi={boardApi} />
          ) : p1.history ? (
            <HistoryView repo={p1.history} boardApi={boardApi} version={p1.version} onChanged={p1.bump} />
          ) : (
            <p className="muted">This browser is not saving anything on this device, so there is no history to show.</p>
          )}
          {p1.saved ? (
            <SavedPlaces repo={p1.saved} flow={p1.flow} version={p1.version} onChanged={p1.bump} onUseAsStart={p1.armStartingArea} />
          ) : null}
          {p1.startingAreaNeedsLocation ? (
            <p className="notice" role="status">
              Turn on location sharing above, then tap again to use your saved place as the starting area.
            </p>
          ) : null}
          <button type="button" className="secondary" onClick={() => setOpen(false)}>
            Hide
          </button>
        </>
      ) : null}
      <SavedPlaceFlowView flow={p1.flow} mode={p1.mode} />
    </section>
  );
}
