import type { SavedEntry } from "../history/types";
import { STARTING_AREA_NOTE } from "../history/copy";

export function startingAreaLabel(entry: SavedEntry): string {
  return entry.kind === "HOME" ? "Home" : entry.kind === "WORK" ? "Work" : entry.label;
}

/** Shown above the composer after the user tapped "Use my saved Home as the starting area". One-shot. */
export function StartingAreaChip({ entry, onRemove }: { entry: SavedEntry; onRemove(): void }) {
  return (
    <div className="notice starting-area" role="status">
      <p>
        Starting area for your next message: <strong>{startingAreaLabel(entry)}</strong>. {STARTING_AREA_NOTE}
      </p>
      <button type="button" className="secondary" onClick={onRemove}>
        Don&apos;t use it
      </button>
    </div>
  );
}
