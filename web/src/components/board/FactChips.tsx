import { useState } from "react";

import type { Fact, FactEdit } from "../../board/boardApi";
import { factStale, factText, parseFactValue } from "../../board/viewModels";

interface Props {
  title: string;
  facts: Fact[];
  now: number;
  busy: boolean;
  emptyText: string;
  /** Assumptions can be confirmed in one tap; the server stamps the confirmation. */
  confirmable?: boolean;
  onEdit(upsert: FactEdit[], remove: string[]): void;
}

function toEdit(fact: Fact, value: Fact["value"], hard: boolean): FactEdit {
  return { key: fact.key, scope: fact.scope, value, hard };
}

export function FactChips({ title, facts, now, busy, emptyText, confirmable = false, onEdit }: Props) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [hard, setHard] = useState(false);

  return (
    <section className="chips-section" aria-label={title}>
      <h3>{title}</h3>
      {facts.length === 0 ? <p className="muted">{emptyText}</p> : null}
      <ul className="chips">
        {facts.map((fact) => {
          const text = factText(fact);
          const stale = factStale(fact, now);
          return (
            <li key={fact.id} className={stale ? "chip stale" : "chip"}>
              {editingId === fact.id ? (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    onEdit([toEdit(fact, parseFactValue(value, fact.value), hard)], []);
                    setEditingId(null);
                  }}
                >
                  <label>
                    <span className="sr-only">{`Value for ${fact.key}`}</span>
                    <input value={value} maxLength={200} onChange={(e) => setValue(e.target.value)} />
                  </label>
                  <label>
                    <input type="checkbox" checked={hard} onChange={(e) => setHard(e.target.checked)} /> Must have
                  </label>
                  <button type="submit" disabled={busy}>
                    Save
                  </button>
                  <button type="button" className="secondary" onClick={() => setEditingId(null)}>
                    Cancel
                  </button>
                </form>
              ) : (
                <>
                  <span className="chip-text">{text}</span>
                  {fact.hard ? <span className="chip-tag">must have</span> : null}
                  {stale ? <span className="chip-tag">may be out of date</span> : null}
                  {confirmable ? (
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy}
                      aria-label={`Confirm ${text}`}
                      onClick={() => onEdit([toEdit(fact, fact.value, fact.hard)], [])}
                    >
                      Confirm
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    aria-label={`Edit ${text}`}
                    onClick={() => {
                      setEditingId(fact.id);
                      setValue(fact.value === null || fact.value === undefined ? "" : String(fact.value));
                      setHard(fact.hard);
                    }}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    aria-label={`Remove ${text}`}
                    onClick={() => onEdit([], [fact.id])}
                  >
                    Remove
                  </button>
                </>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function AddFact({ busy, onAdd }: { busy: boolean; onAdd(edit: FactEdit): void }) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [hard, setHard] = useState(false);
  return (
    <form
      className="add-fact"
      onSubmit={(event) => {
        event.preventDefault();
        if (!key.trim()) return;
        onAdd({ key: key.trim(), value: value.trim(), hard, scope: "trip" });
        setKey("");
        setValue("");
        setHard(false);
      }}
    >
      <label>
        <span className="sr-only">Constraint name</span>
        <input placeholder="e.g. dietary" value={key} maxLength={60} onChange={(e) => setKey(e.target.value)} />
      </label>
      <label>
        <span className="sr-only">Constraint value</span>
        <input placeholder="e.g. vegetarian" value={value} maxLength={200} onChange={(e) => setValue(e.target.value)} />
      </label>
      <label>
        <input type="checkbox" checked={hard} onChange={(e) => setHard(e.target.checked)} /> Must have
      </label>
      <button type="submit" disabled={busy || !key.trim()}>
        Add
      </button>
    </form>
  );
}
