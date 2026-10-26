import { useCallback, useEffect, useRef, useState } from "react";

import type {
  BoardApi,
  BoardFailure,
  FactEdit,
  PendingAction,
  PendingActionEffect,
  PlaceCard,
  RouteCheck,
  Workspace,
} from "./board/boardApi";
import { logOutcomeLocally, saveReminder } from "./board/reminders";
import { handoffUrlFrom, MAX_CANDIDATES, secondsLeft } from "./board/viewModels";
import { GeoError, geoErrorText, type Coordinates } from "./lib/geo";
import { newIdempotencyKey } from "./lib/idempotency";
import type { OutcomeValue } from "./board/boardApi";
import type { LocalSession } from "./useLocalSession";
import type { LocationConsent } from "./useLocationConsent";

export type Loadable<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; data: T }
  | { status: "failed"; failure: BoardFailure };

export type RouteState = Loadable<RouteCheck> | { status: "needs_consent" } | { status: "geo"; text: string };

export interface Candidate {
  placeId: string;
  /** The user's own label. Never Maps content. */
  label: string;
}
export interface CandidateCheck {
  card: Loadable<PlaceCard>;
  route: RouteState;
}

export type ActionPhase = "preview" | "confirming" | "done" | "cancelled" | "failed";
export type ReminderSave = "saved" | "needs_notice" | "not_saved";
export interface ActionState {
  action: PendingAction;
  label: string;
  phase: ActionPhase;
  failure?: BoardFailure;
  /** Handoff: the pinned URL the server returned at confirm time (memory only). */
  handoffUrl?: string | null;
  /** Reminder: chosen due time and what happened to it. */
  dueMinutes?: number;
  dueAt?: number;
  reminder?: ReminderSave;
}
export interface FollowUp {
  placeId: string;
  label: string;
}
interface PendingEdit {
  key: string;
  baseVersion: number;
  upsert: FactEdit[];
  remove: string[];
}

export interface Board {
  workspace: Loadable<Workspace>;
  /** True when an edit lost to a newer version. */
  conflict: boolean;
  editing: boolean;
  editFailure: BoardFailure | null;
  candidates: Candidate[];
  checks: Record<string, CandidateCheck>;
  candidateNotice: string | null;
  actions: ActionState[];
  proposeFailure: BoardFailure | null;
  followUps: FollowUp[];
  reminderToken: number;
  activation: Loadable<null>;
  reload(): void;
  edit(upsert: FactEdit[], remove: string[]): void;
  retryEdit(): void;
  checkPlace(placeId: string): void;
  checkRoute(placeId: string): void;
  removeCandidate(placeId: string): void;
  renameCandidate(placeId: string, label: string): void;
  propose(effect: PendingActionEffect, placeId: string): void;
  setDue(actionId: string, minutes: number): void;
  confirm(actionId: string): void;
  cancel(actionId: string): void;
  acceptNotice(actionId: string): void;
  declineNotice(actionId: string): void;
  addFollowUp(followUp: FollowUp): void;
  finishFollowUp(placeId: string, outcome: OutcomeValue | null): void;
  activate(): void;
}

const LOADING = { status: "loading" } as const;

export function useBoard(api: BoardApi, location: LocationConsent, session: LocalSession | null): Board {
  const [workspace, setWorkspace] = useState<Loadable<Workspace>>(LOADING);
  const [conflict, setConflict] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editFailure, setEditFailure] = useState<BoardFailure | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [checks, setChecks] = useState<Record<string, CandidateCheck>>({});
  const [candidateNotice, setCandidateNotice] = useState<string | null>(null);
  const [actions, setActions] = useState<ActionState[]>([]);
  const [proposeFailure, setProposeFailure] = useState<BoardFailure | null>(null);
  const [followUps, setFollowUps] = useState<FollowUp[]>([]);
  const [reminderToken, setReminderToken] = useState(0);
  const [activation, setActivation] = useState<Loadable<null>>({ status: "idle" });
  const pendingEdit = useRef<PendingEdit | null>(null);
  const held = useRef<Record<string, { placeId: string; label: string; dueAt: number }>>({});
  const live = useRef(true);
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const candidatesRef = useRef(candidates);
  candidatesRef.current = candidates;
  const workspaceRef = useRef(workspace);
  workspaceRef.current = workspace;
  const locationRef = useRef(location);
  locationRef.current = location;

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const loadWorkspace = useCallback(async () => {
    setWorkspace(LOADING);
    const outcome = await api.getWorkspace();
    if (!live.current) return;
    if (outcome.ok) {
      setWorkspace({ status: "ok", data: outcome.data });
      // A server-proposed action shows up with its preview; the nonce stays in memory.
      setActions((current) => {
        const known = new Set(current.map((a) => a.action.id));
        const added = outcome.data.pending_actions
          .filter((a) => !known.has(a.id) && a.confirmed_at == null)
          .map<ActionState>((action) => ({
            action,
            label: candidatesRef.current.find((c) => c.placeId === action.target)?.label ?? "My place",
            phase: "preview",
            dueMinutes: 60,
            dueAt: Date.now() + 60 * 60 * 1000,
          }));
        return added.length ? [...current, ...added] : current;
      });
    } else setWorkspace({ status: "failed", failure: outcome.failure });
  }, [api]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  const reload = useCallback(() => {
    setConflict(false);
    setEditFailure(null);
    pendingEdit.current = null;
    void loadWorkspace();
  }, [loadWorkspace]);

  const runEdit = useCallback(
    async (edit: PendingEdit) => {
      setEditing(true);
      setEditFailure(null);
      const outcome = await api.editWorkspace({
        baseVersion: edit.baseVersion,
        upsert: edit.upsert,
        remove: edit.remove,
        idempotencyKey: edit.key,
      });
      if (!live.current) return;
      setEditing(false);
      if (outcome.ok) {
        pendingEdit.current = null;
        setConflict(false);
        setWorkspace({ status: "ok", data: outcome.data });
        return;
      }
      if (outcome.failure.kind === "conflict") {
        pendingEdit.current = null;
        setConflict(true);
        return;
      }
      setEditFailure(outcome.failure);
    },
    [api],
  );

  const edit = useCallback(
    (upsert: FactEdit[], remove: string[]) => {
      const current = workspaceRef.current;
      if (current.status !== "ok") return;
      const next: PendingEdit = { key: newIdempotencyKey(), baseVersion: current.data.version, upsert, remove };
      pendingEdit.current = next;
      void runEdit(next);
    },
    [runEdit],
  );

  // A retry reuses the key and the base version, so the server replays rather than repeats.
  const retryEdit = useCallback(() => {
    if (pendingEdit.current) void runEdit(pendingEdit.current);
  }, [runEdit]);

  const patchCheck = useCallback((placeId: string, patch: Partial<CandidateCheck>) => {
    setChecks((current) => ({
      ...current,
      [placeId]: { card: { status: "idle" }, route: { status: "idle" }, ...current[placeId], ...patch },
    }));
  }, []);

  const runRoute = useCallback(
    async (placeId: string, position: Promise<Coordinates> | null) => {
      if (locationRef.current.phase !== "on" || !position) {
        patchCheck(placeId, { route: { status: "needs_consent" } });
        return;
      }
      patchCheck(placeId, { route: LOADING });
      let coordinates: Coordinates;
      try {
        coordinates = await position;
      } catch (error) {
        if (live.current) {
          patchCheck(placeId, { route: { status: "geo", text: geoErrorText(error instanceof GeoError ? error.kind : "unavailable") } });
        }
        return;
      }
      const outcome = await api.routeCheck(placeId, coordinates);
      if (!live.current) return;
      if (outcome.ok) patchCheck(placeId, { route: { status: "ok", data: outcome.data } });
      else if (outcome.failure.kind === "consent_required") {
        locationRef.current.markOff();
        patchCheck(placeId, { route: { status: "needs_consent" } });
      } else patchCheck(placeId, { route: { status: "failed", failure: outcome.failure } });
    },
    [api, patchCheck],
  );

  const readPosition = () => (locationRef.current.phase === "on" ? locationRef.current.readPosition() : null);

  const runCard = useCallback(
    async (placeId: string) => {
      patchCheck(placeId, { card: LOADING });
      const outcome = await api.placeDetails(placeId);
      if (!live.current) return;
      if (!outcome.ok) patchCheck(placeId, { card: { status: "failed", failure: outcome.failure } });
      else if (outcome.data === null) patchCheck(placeId, { card: { status: "failed", failure: { kind: "unknown" } } });
      else patchCheck(placeId, { card: { status: "ok", data: outcome.data } });
    },
    [api, patchCheck],
  );

  const checkPlace = useCallback(
    (placeId: string) => {
      setCandidateNotice(null);
      const list = candidatesRef.current;
      if (!list.some((c) => c.placeId === placeId)) {
        if (list.length >= MAX_CANDIDATES) {
          setCandidateNotice("The board holds up to three places. Remove one to check another.");
          return;
        }
        setCandidates([...list, { placeId, label: `My pick ${list.length + 1}` }]);
      }
      // The position is read inside this tap, kept in memory for this one request.
      const position = readPosition();
      void runCard(placeId);
      void runRoute(placeId, position);
    },
    [runCard, runRoute],
  );

  const checkRoute = useCallback(
    (placeId: string) => void runRoute(placeId, readPosition()),
    [runRoute],
  );

  const removeCandidate = useCallback((placeId: string) => {
    setCandidates((c) => c.filter((x) => x.placeId !== placeId));
    setChecks((c) => {
      const { [placeId]: _gone, ...rest } = c;
      return rest;
    });
    setCandidateNotice(null);
  }, []);

  const renameCandidate = useCallback((placeId: string, label: string) => {
    setCandidates((c) => c.map((x) => (x.placeId === placeId ? { ...x, label: label.slice(0, 60) } : x)));
  }, []);

  const updateAction = useCallback((id: string, patch: Partial<ActionState>) => {
    setActions((current) => current.map((a) => (a.action.id === id ? { ...a, ...patch } : a)));
  }, []);

  const propose = useCallback(
    async (effect: PendingActionEffect, placeId: string) => {
      const current = workspaceRef.current;
      if (current.status !== "ok") return;
      const label = candidatesRef.current.find((c) => c.placeId === placeId)?.label ?? "My place";
      setProposeFailure(null);
      const outcome = await api.propose({
        effect,
        target: placeId,
        userLabel: label,
        workspaceId: current.data.id,
        planVersion: current.data.version,
      });
      if (!live.current) return;
      if (outcome.ok) {
        setActions((a) => [
          ...a.filter((x) => x.action.id !== outcome.data.id),
          { action: outcome.data, label, phase: "preview", dueMinutes: 60, dueAt: Date.now() + 60 * 60 * 1000 },
        ]);
      } else setProposeFailure(outcome.failure);
    },
    [api],
  );

  const persistReminder = useCallback(
    async (state: { placeId: string; label: string; dueAt: number }, actionId: string): Promise<ReminderSave> => {
      const s = sessionRef.current;
      if (!s) return "not_saved";
      if (s.notice.state === "unset") {
        held.current[actionId] = state;
        return "needs_notice";
      }
      if (s.notice.state !== "accepted") return "not_saved";
      try {
        await saveReminder(s.store, { placeId: state.placeId, label: state.label, dueAt: new Date(state.dueAt) });
        setReminderToken((n) => n + 1);
        return "saved";
      } catch {
        return "not_saved";
      }
    },
    [],
  );

  const confirm = useCallback(
    async (actionId: string) => {
      const state = actions.find((a) => a.action.id === actionId);
      if (!state || state.phase === "confirming" || secondsLeft(state.action.expires_at, Date.now()) <= 0) return;
      updateAction(actionId, { phase: "confirming", failure: undefined });
      const outcome = await api.confirm(state.action);
      if (!live.current) return;
      if (!outcome.ok) {
        updateAction(actionId, { phase: "failed", failure: outcome.failure });
        return;
      }
      const effect = outcome.data.action.effect;
      const placeId = state.action.target;
      if (effect === "LAUNCH_HANDOFF") {
        const url = handoffUrlFrom(outcome.data);
        if (url) window.open(url, "_blank", "noopener,noreferrer");
        updateAction(actionId, { phase: "done", handoffUrl: url });
        setFollowUps((f) => (f.some((x) => x.placeId === placeId) ? f : [...f, { placeId, label: state.label }]));
      } else if (effect === "SET_REMINDER") {
        const reminder = await persistReminder(
          { placeId, label: state.label, dueAt: Date.now() + (state.dueMinutes ?? 60) * 60_000 },
          actionId,
        );
        if (live.current) updateAction(actionId, { phase: "done", reminder });
      } else updateAction(actionId, { phase: "done" });
    },
    [actions, api, updateAction, persistReminder],
  );

  const cancel = useCallback(
    async (actionId: string) => {
      const state = actions.find((a) => a.action.id === actionId);
      if (!state) return;
      const outcome = await api.cancel(actionId);
      if (!live.current) return;
      // A cancel that fails still stops the client from confirming; the nonce is dropped at expiry.
      updateAction(actionId, outcome.ok || outcome.failure.kind === "conflict" ? { phase: "cancelled" } : { phase: "failed", failure: outcome.failure });
    },
    [actions, api, updateAction],
  );

  const acceptNotice = useCallback(
    async (actionId: string) => {
      const s = sessionRef.current;
      const pending = held.current[actionId];
      if (!s || !pending) return;
      delete held.current[actionId];
      s.notice.accept();
      try {
        await s.store.setNotice(true);
        await saveReminder(s.store, { placeId: pending.placeId, label: pending.label, dueAt: new Date(pending.dueAt) });
        setReminderToken((n) => n + 1);
        updateAction(actionId, { reminder: "saved" });
      } catch {
        updateAction(actionId, { reminder: "not_saved" });
      }
    },
    [updateAction],
  );

  const declineNotice = useCallback(
    (actionId: string) => {
      delete held.current[actionId];
      sessionRef.current?.notice.decline();
      updateAction(actionId, { reminder: "not_saved" });
    },
    [updateAction],
  );

  const setDue = useCallback(
    (actionId: string, minutes: number) => updateAction(actionId, { dueMinutes: minutes, dueAt: Date.now() + minutes * 60_000 }),
    [updateAction],
  );

  const addFollowUp = useCallback((followUp: FollowUp) => {
    setFollowUps((f) => (f.some((x) => x.placeId === followUp.placeId) ? f : [...f, followUp]));
  }, []);

  const finishFollowUp = useCallback((placeId: string, outcome: OutcomeValue | null) => {
    const found = followUps.find((f) => f.placeId === placeId);
    setFollowUps((f) => f.filter((x) => x.placeId !== placeId));
    const s = sessionRef.current;
    if (outcome && found && s && s.notice.state === "accepted") {
      void logOutcomeLocally(s.store, { placeId, label: found.label, outcome }).catch(() => undefined);
    }
  }, [followUps]);

  const activate = useCallback(async () => {
    const current = workspaceRef.current;
    if (current.status !== "ok") return;
    setActivation(LOADING);
    const outcome = await api.activateTrip(current.data.id, current.data.version);
    if (!live.current) return;
    if (outcome.ok) {
      setActivation({ status: "ok", data: null });
      setWorkspace({ status: "ok", data: { ...current.data, status: outcome.data.status } });
    } else setActivation({ status: "failed", failure: outcome.failure });
  }, [api]);

  return {
    workspace, conflict, editing, editFailure, candidates, checks, candidateNotice, actions, proposeFailure, followUps,
    reminderToken, activation, reload, edit, retryEdit, checkPlace, checkRoute, removeCandidate,
    renameCandidate, propose, setDue, confirm, cancel, acceptNotice, declineNotice, addFollowUp,
    finishFollowUp, activate,
  };
}
