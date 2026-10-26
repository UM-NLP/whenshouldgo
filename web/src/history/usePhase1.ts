import { useCallback, useMemo, useRef, useState } from "react";

import type { TurnResponse } from "../api/client";
import type { AccountApi } from "../account/accountApi";
import type { BoardApi, BoardFailure } from "../board/boardApi";
import type { Coordinates } from "../lib/geo";
import { createAskStore, detectNearKind, QUESTION_TEXT, shouldAsk } from "../places/SavedAskPolicy";
import { useSaveFlow, type SaveFlow } from "../places/SavedPlaceFlow";
import type { LocalSession } from "../useLocalSession";
import type { LocationConsent } from "../useLocationConsent";
import { askSignalFromTurn, createServerHistoryRepo, createServerSavedRepo, type AskKind } from "./contractAdapter";
import { createDeviceHistoryRepo, createDeviceSavedRepo } from "./deviceRepos";
import { UNAVAILABLE_PHASE1_API, type Phase1Api } from "./historyApi";
import type { HistoryRepo, RepoMode, SavedEntry, SavedRepo } from "./types";

export interface Phase1Deps {
  api: Phase1Api;
  account: AccountApi;
}

export type AskAnswer = "yes" | "later" | "never";

/** The fixed question on screen: the label it is about and its text (server text for registered users). */
export interface AskPrompt {
  kind: AskKind;
  text: string;
}

export interface Phase1 {
  mode: RepoMode;
  history: HistoryRepo | null;
  saved: SavedRepo | null;
  /** Bumped after any change so the lists reload. */
  version: number;
  bump(): void;
  /** The fixed Home/Work question to show now, or null. */
  question: AskPrompt | null;
  /** Guests: the local keyword rule over the user's own message. Registered: a no-op (the turn carries the server's prompt). */
  observeMessage(text: string): void;
  observeTurn(turn: TurnResponse): void;
  answer(choice: AskAnswer): void;
  flow: SaveFlow;
  startingArea: SavedEntry | null;
  /** An explicit user tap. Needs the existing location consent; otherwise it opens that consent flow. */
  armStartingArea(entry: SavedEntry): void;
  clearStartingArea(): void;
  /** One-shot: the rounded saved position for the next send, then forgotten. */
  consumeStartingArea(): Coordinates | null;
  startingAreaNeedsLocation: boolean;
}

function toBoardFailure(kind: string): BoardFailure {
  return (["unavailable", "conflict", "consent_required", "quota", "rate_limited", "auth", "provider", "network", "invalid"].includes(kind)
    ? { kind }
    : { kind: "unknown" }) as BoardFailure;
}

/**
 * Phase 1 behind the server capability. Returns null (and makes no request) unless `enabled` is true,
 * and `enabled` must come from the server's Capabilities.history_enabled, never from client state or env.
 */
export function usePhase1(opts: {
  enabled: boolean;
  tier: "guest" | "registered" | null;
  deps: Phase1Deps | null;
  session: LocalSession | null;
  boardApi: BoardApi;
  location: LocationConsent;
}): Phase1 | null {
  const { enabled, tier, deps, session, boardApi, location } = opts;
  const mode: RepoMode = tier === "registered" ? "server" : "device";
  const store = session?.store ?? null;
  const [version, setVersion] = useState(0);
  const [question, setQuestion] = useState<AskPrompt | null>(null);
  const [startingArea, setStartingArea] = useState<SavedEntry | null>(null);
  const [needsLocation, setNeedsLocation] = useState(false);
  const serverDeclined = useRef(new Set<AskKind>());
  const startingRef = useRef<SavedEntry | null>(null);
  startingRef.current = startingArea;

  const history = useMemo<HistoryRepo | null>(() => {
    if (!deps) return null;
    if (mode === "server") {
      return createServerHistoryRepo(deps.api, async () => {
        const o = await deps.account.exportMe();
        return o.ok ? o : { ok: false, failure: toBoardFailure(o.failure.kind) };
      });
    }
    return store ? createDeviceHistoryRepo(store) : null;
  }, [deps, mode, store]);
  const saved = useMemo<SavedRepo | null>(() => {
    if (!deps) return null;
    if (mode === "server") return createServerSavedRepo(deps.api);
    return store ? createDeviceSavedRepo(store) : null;
  }, [deps, mode, store]);
  const askStore = useMemo(() => createAskStore(store), [store]);

  const bump = useCallback(() => setVersion((v) => v + 1), []);
  const flow = useSaveFlow({
    mode,
    boardApi,
    api: deps?.api ?? UNAVAILABLE_PHASE1_API,
    saved,
    location,
    session,
    onSaved: bump,
  });

  const observeMessage = useCallback(
    (text: string) => {
      if (!enabled || mode !== "device" || !saved) return;
      const kind = detectNearKind(text);
      if (!kind) return;
      void (async () => {
        const list = await saved.list();
        const hasSaved = list.ok && list.data.some((e) => e.kind === kind);
        const state = await askStore.get(kind);
        if (shouldAsk(state, hasSaved, Date.now())) setQuestion((current) => current ?? { kind, text: QUESTION_TEXT[kind] });
      })();
    },
    [enabled, mode, saved, askStore],
  );

  // Registered users: the server decides from its own ask-state and puts its fixed question on the turn.
  // If it could not store a past answer (no SAVED_PLACE consent) the answer is kept on this device, so
  // it is honoured here too.
  const observeTurn = useCallback(
    (turn: TurnResponse) => {
      if (!enabled || mode !== "server") return;
      const signal = askSignalFromTurn(turn);
      if (!signal || serverDeclined.current.has(signal.kind)) return;
      void (async () => {
        if (!shouldAsk(await askStore.get(signal.kind), false, Date.now())) return;
        setQuestion((current) => current ?? { kind: signal.kind, text: signal.question ?? QUESTION_TEXT[signal.kind] });
      })();
    },
    [enabled, mode, askStore],
  );

  const answer = useCallback(
    (choice: AskAnswer) => {
      const asked = question;
      if (!asked) return;
      const kind = asked.kind;
      setQuestion(null);
      if (choice === "yes") return flow.start(kind);
      if (mode === "server" && deps) {
        serverDeclined.current.add(kind); // this visit is honoured whatever the server says
        void (async () => {
          const o = await deps.api.putAskState(kind, choice === "later" ? "DECLINED" : "DONT_ASK_AGAIN");
          if (o.ok && o.data.recorded) return;
          // Not stored on the server (no consent, or an error): keep the choice on the device.
          await (choice === "later" ? askStore.decline(kind, Date.now()) : askStore.dontAskAgain(kind));
        })();
        return;
      }
      void (choice === "later" ? askStore.decline(kind, Date.now()) : askStore.dontAskAgain(kind));
    },
    [question, flow, mode, askStore, deps],
  );

  const armStartingArea = useCallback(
    (entry: SavedEntry) => {
      if (location.phase !== "on") {
        location.start();
        setNeedsLocation(true);
        return;
      }
      setNeedsLocation(false);
      setStartingArea(entry);
    },
    [location],
  );

  const consumeStartingArea = useCallback((): Coordinates | null => {
    const entry = startingRef.current;
    if (!entry || location.phase !== "on") return null;
    setStartingArea(null);
    return { latitude: entry.position.latitude, longitude: entry.position.longitude };
  }, [location.phase]);

  if (!enabled || !deps) return null;
  return {
    mode,
    history,
    saved,
    version,
    bump,
    question,
    observeMessage,
    observeTurn,
    answer,
    flow,
    startingArea,
    armStartingArea,
    clearStartingArea: () => setStartingArea(null),
    consumeStartingArea,
    startingAreaNeedsLocation: needsLocation && location.phase !== "on",
  };
}
