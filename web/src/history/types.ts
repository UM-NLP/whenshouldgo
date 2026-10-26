// Phase 1 view types. Guests read these from the on-device store; registered users from the server.
// Only place IDs, the user's own labels, an activity tag and times appear here: no Maps content.
import type { BoardFailure } from "../board/boardApi";
import type { Result } from "../lib/typedCall";
import type { SavedPlaceKind } from "../storage";

export type HistoryFailure = BoardFailure | { kind: "storage" } | { kind: "notice_required" } | { kind: "no_store" };
export type HistoryOutcome<T> = Result<T, HistoryFailure>;

export type RepoMode = "device" | "server";

export interface OutingEntry {
  id: string;
  placeId: string;
  /** The user's own label, if they gave one. Never filled from a Maps name. */
  label: string | null;
  /** FOOD_DRINK, OUTDOOR, KIDS, ERRAND, OTHER (classified from the user's own request). */
  activityTag: string;
  at: string;
  /** Fixed text such as "Went", derived from the user's own report. */
  outcomeText: string | null;
}

export interface Position {
  latitude: number;
  longitude: number;
}

export interface SavedEntry {
  /** Stable key for the list: the place ID (server) or the record id (device). */
  id: string;
  placeId: string;
  label: string;
  kind: SavedPlaceKind;
  /** Rounded to about 100 m. Kept in memory for the explicit "use as starting area" tap only; never shown. */
  position: Position;
}

export interface HistoryRepo {
  readonly mode: RepoMode;
  readonly canEditLabel: boolean;
  readonly canDeleteOne: boolean;
  list(): Promise<HistoryOutcome<OutingEntry[]>>;
  editLabel(id: string, label: string): Promise<HistoryOutcome<null>>;
  deleteOne(id: string): Promise<HistoryOutcome<null>>;
  clearAll(): Promise<HistoryOutcome<null>>;
  /** Plain data for a download built on the client. Never written anywhere. */
  exportData(): Promise<HistoryOutcome<unknown>>;
}

export interface SavedRepo {
  readonly mode: RepoMode;
  list(): Promise<HistoryOutcome<SavedEntry[]>>;
  rename(entry: SavedEntry, label: string): Promise<HistoryOutcome<null>>;
  remove(entry: SavedEntry): Promise<HistoryOutcome<null>>;
  /**
   * Device only (guests): called after the user's explicit local confirmation. The server repo has no
   * such method: for a registered user the server writes the place itself when the action is confirmed.
   */
  saveConfirmed?(input: { placeId: string; label: string; kind: SavedPlaceKind; position: Position }): Promise<HistoryOutcome<null>>;
}
