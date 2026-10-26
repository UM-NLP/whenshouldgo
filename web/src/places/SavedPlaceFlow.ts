// Saving Home, Work or a custom place. This is a side effect, so it always goes through the server's
// PendingAction protocol: propose (preview, nonce, expiry, key), then an explicit Confirm. Only after
// the server confirms does the client finish the save (device store for guests, server upsert for
// registered users). The position is read inside the user's tap, rounded to 3 decimals, and held in
// memory until then; it never goes in a URL, a log or a key.
import { useCallback, useRef, useState } from "react";

import type { BoardApi, BoardFailure } from "../board/boardApi";
import { planVersionOf, secondsLeft } from "../board/viewModels";
import { positionTargetId, SAVED_PLACE_NOTICE_VERSION } from "../history/contractAdapter";
import type { Phase1Api } from "../history/historyApi";
import type { Position, RepoMode, SavedRepo } from "../history/types";
import { GeoError, geoErrorText } from "../lib/geo";
import { newIdempotencyKey } from "../lib/idempotency";
import { round3, type SavedPlaceKind } from "../storage";
import type { ActionState } from "../useBoard";
import type { LocalSession } from "../useLocalSession";
import type { LocationConsent } from "../useLocationConsent";

export const DEFAULT_LABEL: Record<SavedPlaceKind, string> = { HOME: "Home", WORK: "Work", CUSTOM: "My place" };

export type SaveStep =
  | { step: "idle" }
  | { step: "needs_location" }
  | { step: "geo"; text: string }
  | { step: "needs_notice" }
  | { step: "needs_consent" }
  | { step: "unsaveable" }
  | { step: "working" }
  | { step: "action"; state: ActionState }
  | { step: "saved"; kind: SavedPlaceKind }
  | { step: "finish_failed" }
  | { step: "failed"; failure: BoardFailure };

export interface SaveFlow {
  state: SaveStep;
  /** Called from a user's tap. `label` defaults to Home or Work. */
  start(kind: SavedPlaceKind, label?: string): void;
  acceptNotice(): void;
  declineNotice(): void;
  grantConsent(): void;
  confirm(): void;
  cancel(): void;
  retryFinish(): void;
  dismiss(): void;
}

interface Held {
  kind: SavedPlaceKind;
  label: string;
  position: Position;
  placeId: string;
}

export function useSaveFlow(opts: {
  mode: RepoMode;
  boardApi: BoardApi;
  api: Phase1Api;
  saved: SavedRepo | null;
  location: LocationConsent;
  session: LocalSession | null;
  onSaved(): void;
}): SaveFlow {
  const [state, setState] = useState<SaveStep>({ step: "idle" });
  const held = useRef<Held | null>(null);
  const optsRef = useRef(opts);
  optsRef.current = opts;

  const propose = useCallback(async () => {
    const h = held.current;
    if (!h) return;
    const { boardApi } = optsRef.current;
    setState({ step: "working" });
    const ws = await boardApi.getWorkspace();
    if (!ws.ok) return setState({ step: "failed", failure: ws.failure });
    const outcome = await boardApi.propose({
      effect: "SAVE_PLACE",
      target: h.placeId,
      userLabel: h.label,
      workspaceId: ws.data.id,
      planVersion: planVersionOf(ws.data),
      idempotencyKey: newIdempotencyKey(),
      labelKind: h.kind,
    });
    if (!outcome.ok) return setState({ step: "failed", failure: outcome.failure });
    setState({ step: "action", state: { action: outcome.data, label: h.label, phase: "preview" } });
  }, []);

  // Prerequisites in order: where it is stored must be agreed first (guest notice, or the SAVED_PLACE consent).
  const advance = useCallback(async () => {
    const { mode, session, api } = optsRef.current;
    if (mode === "device") {
      const notice = session?.notice.state;
      if (!session || notice === "none" || notice === "declined") return setState({ step: "unsaveable" });
      if (notice === "unset" || notice === "loading") return setState({ step: "needs_notice" });
    } else {
      setState({ step: "working" });
      const has = await api.hasConsent("SAVED_PLACE");
      if (!has.ok) return setState({ step: "failed", failure: has.failure });
      if (!has.data) return setState({ step: "needs_consent" });
    }
    await propose();
  }, [propose]);

  const start = useCallback(
    (kind: SavedPlaceKind, label?: string) => {
      const { location } = optsRef.current;
      held.current = null;
      if (location.phase !== "on") {
        location.start(); // the existing consent flow; the user taps again once it is on
        return setState({ step: "needs_location" });
      }
      setState({ step: "working" });
      // Read inside this tap, rounded to about 100 m, kept in memory only.
      void location.readPosition().then(
        (raw) => {
          held.current = {
            kind,
            label: (label?.trim() || DEFAULT_LABEL[kind]).slice(0, 60),
            position: { latitude: round3(raw.latitude), longitude: round3(raw.longitude) },
            placeId: positionTargetId(),
          };
          void advance();
        },
        (error: unknown) => setState({ step: "geo", text: geoErrorText(error instanceof GeoError ? error.kind : "unavailable") }),
      );
    },
    [advance],
  );

  const finish = useCallback(async () => {
    const h = held.current;
    const { saved, onSaved } = optsRef.current;
    if (!h || !saved) return setState({ step: "finish_failed" });
    const done = await saved.saveConfirmed({ placeId: h.placeId, label: h.label, kind: h.kind, position: h.position });
    if (!done.ok) return setState({ step: "finish_failed" });
    held.current = null;
    onSaved();
    setState({ step: "saved", kind: h.kind });
  }, []);

  const updateAction = (patch: Partial<ActionState>) =>
    setState((s) => (s.step === "action" ? { step: "action", state: { ...s.state, ...patch } } : s));

  return {
    state,
    start,
    acceptNotice: () => {
      const { session } = optsRef.current;
      void (async () => {
        try {
          await session?.store.setNotice(true);
          session?.notice.accept();
          await propose();
        } catch {
          setState({ step: "unsaveable" });
        }
      })();
    },
    declineNotice: () => {
      optsRef.current.session?.notice.decline();
      held.current = null;
      setState({ step: "unsaveable" });
    },
    grantConsent: () => {
      void (async () => {
        setState({ step: "working" });
        const granted = await optsRef.current.api.grantConsent("SAVED_PLACE", SAVED_PLACE_NOTICE_VERSION);
        if (!granted.ok) return setState({ step: "failed", failure: granted.failure });
        await propose();
      })();
    },
    confirm: () => {
      const s = state;
      if (s.step !== "action" || s.state.phase === "confirming") return;
      if (secondsLeft(s.state.action.expires_at, Date.now()) <= 0) return;
      updateAction({ phase: "confirming", failure: undefined });
      void (async () => {
        const outcome = await optsRef.current.boardApi.confirm(s.state.action);
        if (!outcome.ok) return updateAction({ phase: "failed", failure: outcome.failure });
        updateAction({ phase: "done" });
        await finish();
      })();
    },
    cancel: () => {
      const s = state;
      if (s.step !== "action") return;
      void (async () => {
        const outcome = await optsRef.current.boardApi.cancel(s.state.action.id);
        held.current = null;
        if (outcome.ok || outcome.failure.kind === "conflict") updateAction({ phase: "cancelled" });
        else updateAction({ phase: "failed", failure: outcome.failure });
      })();
    },
    retryFinish: () => void finish(),
    dismiss: () => {
      held.current = null;
      setState({ step: "idle" });
    },
  };
}
