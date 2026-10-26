// Fixed copy for Phase 1 (history and saved places). Nothing here comes from the server or the model.
import { boardFailureText } from "../board/viewModels";
import { DEFAULT_RETENTION_DAYS, MAX_RETENTION_DAYS, MIN_RETENTION_DAYS } from "../storage";
import { HISTORY_NOTICE_VERSION, SAVED_PLACE_NOTICE_VERSION, SERVER_RETENTION_CEILING_DAYS } from "./contractAdapter";
import type { HistoryFailure } from "./types";

// PLACEHOLDER notices. Counsel has not reviewed them (privacy notice and Quebec Law 25 treatment of a
// stored home position, issue #27). Replace with the approved wording before any real user sees them.
export const HISTORY_PLACEHOLDER_FLAG = `PLACEHOLDER NOTICE (${HISTORY_NOTICE_VERSION}), pending counsel review (issue #27)`;
export const HISTORY_NOTICE_TEXT =
  "If you turn this on, your outings are saved to your account: the place ID, the label you give it, an activity tag taken from your own request, and the time. " +
  "Google Maps details are not saved; they are fetched again when you open an entry. " +
  `They are kept for up to ${SERVER_RETENTION_CEILING_DAYS} days, and you can export, delete one or clear all at any time or turn this off.`;

export const SAVED_PLACE_PLACEHOLDER_FLAG = `PLACEHOLDER NOTICE (${SAVED_PLACE_NOTICE_VERSION}), pending counsel review (issue #27)`;
export const SAVED_PLACE_NOTICE_TEXT =
  "If you save Home, Work or another place, your account keeps the label you choose and this device's position rounded to about 100 metres. " +
  "It is only ever offered back to you as a starting area when you tap for it. You can rename, export or delete it at any time.";

export const HISTORY_HEADING = "Your history and saved places";
export const HISTORY_EMPTY = "No outings are saved yet. They appear here after you check in on a trip.";
export const SAVED_EMPTY = "No saved places yet.";
export const DETAILS_UNAVAILABLE = "Details unavailable";
export const STARTING_AREA_NOTE =
  "Your saved position is used only for your next message, and only because you tapped for it.";

export const TAG_TEXT: Record<string, string> = {
  FOOD_DRINK: "Food and drink",
  OUTDOOR: "Outdoors",
  KIDS: "Kids",
  ERRAND: "Errand",
  OTHER: "Other",
};

export function tagText(tag: string): string {
  return TAG_TEXT[tag] ?? "Other";
}

export function guestRetentionText(days: number): string {
  return (
    `Your outing history is kept on this device for ${days} days ` +
    `(the default is ${DEFAULT_RETENTION_DAYS} and the longest is ${MAX_RETENTION_DAYS}). Choose a shorter period any time.`
  );
}

export const SERVER_RETENTION_TEXT =
  `Your synced history is kept for up to ${SERVER_RETENTION_CEILING_DAYS} days. ` +
  "Choosing a shorter period is not available yet; you can delete entries or turn history off.";

export const RETENTION_CHOICES: number[] = [MIN_RETENTION_DAYS, 60, DEFAULT_RETENTION_DAYS, 120, 150, MAX_RETENTION_DAYS];

export function historyFailureText(failure: HistoryFailure): string {
  switch (failure.kind) {
    case "storage":
      return "Could not read or change what is saved on this device.";
    case "notice_required":
      return "Saving on this device needs your OK first. Accept the notice above, then try again.";
    case "no_store":
      return "This browser is not saving anything on this device.";
    default:
      return boardFailureText(failure, "action");
  }
}
