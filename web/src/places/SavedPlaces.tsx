import { useCallback, useEffect, useState } from "react";

import { historyFailureText, SAVED_EMPTY } from "../history/copy";
import { downloadJson } from "../lib/download";
import type { HistoryFailure, SavedEntry, SavedRepo } from "../history/types";
import type { SaveFlow } from "./SavedPlaceFlow";
import { startingAreaLabel } from "./SavedStartingArea";

type List = { status: "loading" } | { status: "ok"; entries: SavedEntry[] } | { status: "failed"; failure: HistoryFailure };

const MAX_LABEL = 60;

function Row({
  entry,
  repo,
  onChanged,
  onProblem,
  onUseAsStart,
}: {
  entry: SavedEntry;
  repo: SavedRepo;
  onChanged(): void;
  onProblem(f: HistoryFailure): void;
  onUseAsStart(entry: SavedEntry): void;
}) {
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(entry.label);
  const [removing, setRemoving] = useState(false);
  const kindText = entry.kind === "HOME" ? "Home" : entry.kind === "WORK" ? "Work" : "Saved place";
  const startable = entry.kind === "HOME" || entry.kind === "WORK";

  async function rename() {
    const outcome = await repo.rename(entry, label.trim() || entry.label);
    if (!outcome.ok) return onProblem(outcome.failure);
    setEditing(false);
    onChanged();
  }
  async function remove() {
    const outcome = await repo.remove(entry);
    setRemoving(false);
    if (!outcome.ok) return onProblem(outcome.failure);
    onChanged();
  }

  return (
    <li className="saved-place">
      <p className="place-name">{entry.label}</p>
      <p className="muted">{kindText}</p>
      {editing ? (
        <form
          className="add-fact"
          onSubmit={(e) => {
            e.preventDefault();
            void rename();
          }}
        >
          <label>
            Name <input type="text" maxLength={MAX_LABEL} value={label} onChange={(e) => setLabel(e.target.value)} />
          </label>
          <button type="submit">Save name</button>
          <button type="button" className="secondary" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      ) : null}
      {removing ? (
        <div className="notice notice-warning" role="alertdialog" aria-label={`Delete ${entry.label}`}>
          <p>Delete {entry.label}?</p>
          <div className="notice-actions">
            <button type="button" onClick={() => void remove()}>
              Yes, delete {entry.label}
            </button>
            <button type="button" className="secondary" onClick={() => setRemoving(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="notice-actions">
          {startable ? (
            <button type="button" onClick={() => onUseAsStart(entry)}>
              Use my saved {startingAreaLabel(entry)} as the starting area
            </button>
          ) : null}
          <button type="button" className="secondary" onClick={() => setEditing(true)}>
            Rename
          </button>
          <button type="button" className="secondary" onClick={() => setRemoving(true)}>
            Delete
          </button>
        </div>
      )}
    </li>
  );
}

/**
 * Saved places. Home and Work are created only by the confirmed SAVE_PLACE flow (`flow.start`);
 * nothing here writes one directly. Rename and delete change or remove an existing one.
 */
export function SavedPlaces({
  repo,
  flow,
  version,
  onChanged,
  onUseAsStart,
}: {
  repo: SavedRepo;
  flow: SaveFlow;
  version: number;
  onChanged(): void;
  onUseAsStart(entry: SavedEntry): void;
}) {
  const [list, setList] = useState<List>({ status: "loading" });
  const [problem, setProblem] = useState<HistoryFailure | null>(null);
  const [customName, setCustomName] = useState("");

  const load = useCallback(async () => {
    const outcome = await repo.list();
    setList(outcome.ok ? { status: "ok", entries: outcome.data } : { status: "failed", failure: outcome.failure });
  }, [repo]);
  useEffect(() => {
    setList({ status: "loading" });
    void load();
  }, [load, version]);

  const entries = list.status === "ok" ? list.entries : [];
  const has = (kind: "HOME" | "WORK") => entries.some((e) => e.kind === kind);

  async function exportSaved() {
    if (list.status !== "ok") return;
    downloadJson("whenshouldgo-saved-places.json", {
      saved_places: entries.map((e) => ({ place_id: e.placeId, label: e.label, kind: e.kind, position: e.position })),
    });
  }

  return (
    <div className="saved" aria-label="Saved places">
      <h3>Saved places</h3>
      {list.status === "loading" ? (
        <p className="loading" role="status">
          Loading saved places...
        </p>
      ) : null}
      {list.status === "failed" ? (
        <p className="notice notice-error" role="alert">
          {historyFailureText(list.failure)}
        </p>
      ) : null}
      {list.status === "ok" && entries.length === 0 ? <p className="muted">{SAVED_EMPTY}</p> : null}
      {entries.length > 0 ? (
        <ul className="candidates">
          {entries.map((entry) => (
            <Row key={entry.id} entry={entry} repo={repo} onChanged={onChanged} onProblem={setProblem} onUseAsStart={onUseAsStart} />
          ))}
        </ul>
      ) : null}
      {problem ? (
        <p className="notice notice-error" role="alert">
          {historyFailureText(problem)}
        </p>
      ) : null}
      <div className="notice-actions">
        <button type="button" className="secondary" onClick={() => flow.start("HOME")}>
          {has("HOME") ? "Replace Home with where I am now" : "Save where I am now as Home"}
        </button>
        <button type="button" className="secondary" onClick={() => flow.start("WORK")}>
          {has("WORK") ? "Replace Work with where I am now" : "Save where I am now as Work"}
        </button>
        <button type="button" className="secondary" onClick={() => void exportSaved()} disabled={entries.length === 0}>
          Export saved places
        </button>
      </div>
      <form
        className="add-fact"
        onSubmit={(e) => {
          e.preventDefault();
          if (customName.trim()) flow.start("CUSTOM", customName.trim());
        }}
      >
        <label>
          Another place{" "}
          <input type="text" maxLength={MAX_LABEL} value={customName} placeholder="Name, for example Gym" onChange={(e) => setCustomName(e.target.value)} />
        </label>
        <button type="submit" className="secondary" disabled={!customName.trim()}>
          Save where I am now under this name
        </button>
      </form>
    </div>
  );
}
