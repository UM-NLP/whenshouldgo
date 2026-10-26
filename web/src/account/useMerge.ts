import { useCallback, useEffect, useRef, useState } from "react";

import type { AccountApi, MigrationView, PreferenceChoice } from "./accountApi";
import { accountFailureText, accountRetryable } from "./copy";
import type { IntentRef } from "./useAccount";

export type MergePhase =
  | { phase: "starting" }
  | { phase: "preview"; view: MigrationView }
  | { phase: "running"; view: MigrationView }
  | { phase: "interrupted"; view: MigrationView }
  | { phase: "done"; view: MigrationView }
  | { phase: "failed"; message: string; retryable: boolean; view: MigrationView | null };

export interface Merge {
  state: MergePhase;
  selected: Set<string>;
  preference: PreferenceChoice;
  toggle(id: string): void;
  setPreference(choice: PreferenceChoice): void;
  /** Starts the run, or resumes an interrupted one: it is the same call. */
  run(): void;
  restart(): void;
}

function pickInitial(view: MigrationView): Set<string> {
  const chosen = view.items.filter((i) => i.selected).map((i) => i.id);
  return new Set(chosen);
}

/**
 * The merge after a collision: show the manifest, let the user pick items and a preference
 * (KEEP_REGISTERED by default), run it, and resume it after an interruption. The server deletes the
 * guest's data only after every chosen item was copied.
 */
export function useMerge(api: AccountApi, intent: IntentRef): Merge {
  const [state, setState] = useState<MergePhase>({ phase: "starting" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [preference, setPreference] = useState<PreferenceChoice>("KEEP_REGISTERED");
  const live = useRef(true);
  const view = useRef<MigrationView | null>(null);

  const apply = useCallback((next: MigrationView) => {
    view.current = next;
    if (next.completed || next.state === "COMPLETED") setState({ phase: "done", view: next });
    else if (next.state === "PREVIEW") setState({ phase: "preview", view: next });
    else setState({ phase: "interrupted", view: next }); // RUNNING or CLEANING without us driving it
  }, []);

  const start = useCallback(async () => {
    setState({ phase: "starting" });
    const outcome = await api.startMigration(intent.id, intent.nonce);
    if (!live.current) return;
    if (!outcome.ok) {
      setState({ phase: "failed", message: accountFailureText(outcome.failure), retryable: accountRetryable(outcome.failure), view: null });
      return;
    }
    setSelected(pickInitial(outcome.data));
    if (outcome.data.preference) setPreference(outcome.data.preference);
    apply(outcome.data);
  }, [api, intent, apply]);

  useEffect(() => {
    live.current = true;
    void start();
    return () => {
      live.current = false;
    };
  }, [start]);

  const run = useCallback(async () => {
    const current = view.current;
    if (!current) return;
    setState({ phase: "running", view: current });
    const outcome = await api.runMigration(current.id, [...selected], preference);
    if (!live.current) return;
    if (outcome.ok) {
      view.current = outcome.data;
      if (outcome.data.completed || outcome.data.state === "COMPLETED") setState({ phase: "done", view: outcome.data });
      else setState({ phase: "interrupted", view: outcome.data });
      return;
    }
    if (outcome.failure.kind === "interrupted") {
      // Refresh which items are already copied, then offer to continue.
      const latest = await api.getMigration(current.id);
      if (!live.current) return;
      if (latest.ok) view.current = latest.data;
      setState({ phase: "interrupted", view: view.current ?? current });
      return;
    }
    setState({
      phase: "failed",
      message: accountFailureText(outcome.failure),
      retryable: accountRetryable(outcome.failure),
      view: current,
    });
  }, [api, selected, preference]);

  return {
    state,
    selected,
    preference,
    toggle: (id) =>
      setSelected((current) => {
        const next = new Set(current);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      }),
    setPreference,
    run: () => void run(),
    restart: () => void start(),
  };
}
