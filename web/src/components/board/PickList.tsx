import type { DiscoveryBlock } from "../../api/client";
import { placeIdOf } from "../../board/viewModels";
import type { Board } from "../../useBoard";

/**
 * Rendered AFTER Google's block, never inside or between it and its sources. The place ID is read
 * from each source's typed field; a source without one simply gets no button.
 */
export function PickList({ block, board }: { block: DiscoveryBlock; board: Board }) {
  const picks = block.places
    .map((place) => ({ place, id: placeIdOf(place) }))
    .filter((p): p is { place: (typeof block.places)[number]; id: string } => p.id !== null);
  if (picks.length === 0) return null;
  return (
    <div className="picks" aria-label="Check a place">
      {picks.map(({ place, id }) => (
        <button
          key={id}
          type="button"
          className="secondary"
          aria-label={`Check this place (${place.title})`}
          onClick={() => board.checkPlace(id)}
        >
          Check this place
        </button>
      ))}
    </div>
  );
}
