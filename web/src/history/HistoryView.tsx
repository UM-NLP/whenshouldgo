import { useCallback, useEffect, useState } from "react";

import type { BoardApi, PlaceCard } from "../board/boardApi";
import { PlaceCardView } from "../components/board/PlaceAndRoute";
import { downloadJson } from "../lib/download";
import { DETAILS_UNAVAILABLE, HISTORY_EMPTY, historyFailureText, tagText } from "./copy";
import type { HistoryFailure, HistoryRepo, OutingEntry } from "./types";

type List = { status: "loading" } | { status: "ok"; entries: OutingEntry[] } | { status: "failed"; failure: HistoryFailure };
type Detail = { status: "loading" } | { status: "ok"; card: PlaceCard } | { status: "unavailable" };

const MAX_LABEL = 60;

function when(at: string): string {
  const t = Date.parse(at);
  return Number.isNaN(t) ? "Time unknown" : new Date(t).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function Entry({
  entry,
  repo,
  boardApi,
  onChanged,
  onProblem,
}: {
  entry: OutingEntry;
  repo: HistoryRepo;
  boardApi: BoardApi;
  onChanged(): void;
  onProblem(failure: HistoryFailure): void;
}) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(entry.label ?? "");
  const [removing, setRemoving] = useState(false);

  // Maps details are fetched by place ID when asked for and live in this component's memory only.
  async function showDetails() {
    setDetail({ status: "loading" });
    const outcome = await boardApi.placeDetails(entry.placeId);
    setDetail(outcome.ok && outcome.data ? { status: "ok", card: outcome.data } : { status: "unavailable" });
  }

  async function saveLabel() {
    const outcome = await repo.editLabel(entry.id, label.trim());
    if (!outcome.ok) return onProblem(outcome.failure);
    setEditing(false);
    onChanged();
  }

  async function remove() {
    const outcome = await repo.deleteOne(entry.id);
    setRemoving(false);
    if (!outcome.ok) return onProblem(outcome.failure);
    onChanged();
  }

  return (
    <li className="history-entry">
      <p className="place-name">{entry.label ?? "Unnamed outing"}</p>
      <p className="muted">
        {tagText(entry.activityTag)} · {when(entry.at)}
        {entry.outcomeText ? ` · ${entry.outcomeText}` : ""}
      </p>

      {editing ? (
        <form
          className="add-fact"
          onSubmit={(e) => {
            e.preventDefault();
            void saveLabel();
          }}
        >
          <label>
            Your label <input type="text" maxLength={MAX_LABEL} value={label} onChange={(e) => setLabel(e.target.value)} />
          </label>
          <button type="submit">Save label</button>
          <button type="button" className="secondary" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      ) : null}

      {detail?.status === "loading" ? (
        <p className="loading" role="status">
          Loading place details...
        </p>
      ) : null}
      {detail?.status === "ok" ? <PlaceCardView card={detail.card} now={Date.now()} /> : null}
      {detail?.status === "unavailable" ? <p className="muted">{DETAILS_UNAVAILABLE}</p> : null}

      {removing ? (
        <div className="notice notice-warning" role="alertdialog" aria-label="Delete this entry">
          <p>Delete this entry?</p>
          <div className="notice-actions">
            <button type="button" onClick={() => void remove()}>
              Yes, delete this entry
            </button>
            <button type="button" className="secondary" onClick={() => setRemoving(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="notice-actions">
          {detail === null || detail.status === "unavailable" ? (
            <button type="button" className="secondary" onClick={() => void showDetails()}>
              Show place details
            </button>
          ) : (
            <button type="button" className="secondary" onClick={() => setDetail(null)}>
              Hide place details
            </button>
          )}
          {repo.canEditLabel ? (
            <button type="button" className="secondary" onClick={() => setEditing(true)}>
              Edit label
            </button>
          ) : null}
          {repo.canDeleteOne ? (
            <button type="button" className="secondary" onClick={() => setRemoving(true)}>
              Delete entry
            </button>
          ) : null}
        </div>
      )}
    </li>
  );
}

/** Entries by the user's own label and activity tag; place details only on request, in memory. */
export function HistoryView({ repo, boardApi, version, onChanged }: { repo: HistoryRepo; boardApi: BoardApi; version: number; onChanged(): void }) {
  const [list, setList] = useState<List>({ status: "loading" });
  const [problem, setProblem] = useState<HistoryFailure | null>(null);
  const [clearing, setClearing] = useState(false);
  const [exported, setExported] = useState(false);

  const load = useCallback(async () => {
    const outcome = await repo.list();
    setList(outcome.ok ? { status: "ok", entries: outcome.data } : { status: "failed", failure: outcome.failure });
  }, [repo]);

  useEffect(() => {
    setList({ status: "loading" });
    void load();
  }, [load, version]);

  async function clearAll() {
    setClearing(false);
    const outcome = await repo.clearAll();
    if (!outcome.ok) return setProblem(outcome.failure);
    setProblem(null);
    onChanged();
  }

  async function exportData() {
    const outcome = await repo.exportData();
    if (!outcome.ok) return setProblem(outcome.failure);
    downloadJson("whenshouldgo-history.json", outcome.data);
    setProblem(null);
    setExported(true);
  }

  const entries = list.status === "ok" ? list.entries : [];
  return (
    <div className="history" aria-label="History">
      <h3>History</h3>
      {list.status === "loading" ? (
        <p className="loading" role="status">
          Loading your history...
        </p>
      ) : null}
      {list.status === "failed" ? (
        <p className="notice notice-error" role="alert">
          {historyFailureText(list.failure)}
        </p>
      ) : null}
      {list.status === "ok" && entries.length === 0 ? <p className="muted">{HISTORY_EMPTY}</p> : null}
      {entries.length > 0 ? (
        <ul className="candidates">
          {entries.map((entry) => (
            <Entry key={entry.id} entry={entry} repo={repo} boardApi={boardApi} onChanged={onChanged} onProblem={setProblem} />
          ))}
        </ul>
      ) : null}
      {!repo.canEditLabel || !repo.canDeleteOne ? (
        <p className="muted">Changing or removing a single entry is not available yet for synced history. You can clear it all.</p>
      ) : null}
      {problem ? (
        <p className="notice notice-error" role="alert">
          {historyFailureText(problem)}
        </p>
      ) : null}
      <div className="notice-actions">
        <button type="button" className="secondary" onClick={() => void exportData()}>
          Export my history
        </button>
        <button type="button" className="secondary" onClick={() => setClearing(true)} disabled={entries.length === 0}>
          Clear all history
        </button>
      </div>
      {exported ? <p role="status">Exported.</p> : null}
      {clearing ? (
        <div className="notice notice-warning" role="alertdialog" aria-label="Clear all history">
          <p>Delete all {entries.length} saved outings? This cannot be undone.</p>
          <div className="notice-actions">
            <button type="button" onClick={() => void clearAll()}>
              Yes, clear all
            </button>
            <button type="button" className="secondary" onClick={() => setClearing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
