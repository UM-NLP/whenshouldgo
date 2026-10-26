// Pure view-models for the Trip Board: fixed copy and formatting, no I/O. Every string a user sees
// from a failure is chosen here by kind; server message text never reaches the screen.
import type { DiscoveryPlace } from "../api/client";
import type { BoardFailure, ConfirmActionResponse, Fact, OutcomeValue, PendingAction, RouteCheck } from "./boardApi";
import type { OutingOutcome } from "../storage";

export const MAX_CANDIDATES = 3;
export const REMINDER_COPY =
  "Reminders show up when you open this page. A closed browser tab cannot fire a reminder, and it is saved on this device only.";

/** The place ID comes only from the typed source field; it is never parsed out of text or a URL. */
export function placeIdOf(place: DiscoveryPlace): string | null {
  const value = (place as DiscoveryPlace & { place_id?: unknown }).place_id;
  return typeof value === "string" && value.length > 0 && value.length <= 300 ? value : null;
}

export type BoardArea = "workspace" | "edit" | "place" | "route" | "action" | "outcome" | "activate";

export function boardFailureText(failure: BoardFailure, area: BoardArea): string {
  switch (failure.kind) {
    case "unavailable":
      return area === "workspace"
        ? "The Trip Board is not available yet. You can keep chatting."
        : "This is not available yet.";
    case "conflict":
      return area === "action"
        ? "This action changed or expired. Ask for it again to get a fresh preview."
        : "The board changed somewhere else. Reload it to see the latest version, then redo your change.";
    case "consent_required":
      return "Checking the drive needs your OK to use your location first.";
    case "quota":
      return "You have reached a usage limit for now. What is already here stays available.";
    case "rate_limited":
      return failure.retryAfterSeconds === null
        ? "Too many requests. Wait a moment and try again."
        : `Too many requests. Try again in ${failure.retryAfterSeconds} seconds.`;
    case "auth":
      return "You are not signed in to the service, so this was refused. Reload the page and try again.";
    case "provider":
      return "Something went wrong on our side. You can try again.";
    case "network":
      return "Could not reach the service. Check your connection and try again.";
    case "invalid":
      return "That could not be sent. Check it and try again.";
    default:
      return "Something unexpected happened. You can try again.";
  }
}

/** Retrying the same request is safe for these. */
export function canRetry(failure: BoardFailure): boolean {
  return ["network", "provider", "rate_limited", "unknown"].includes(failure.kind);
}

/** "About 25 minutes by car": rounded, never false precision. Only DRIVE is a normal result. */
export function routeSummary(route: RouteCheck): string {
  const minutes = route.duration_minutes;
  const how = route.mode === "WALK" ? "on foot" : "by car";
  const time = minutes < 5 ? "Under 5 minutes" : `About ${Math.round(minutes / 5) * 5} minutes`;
  const km = route.distance_meters / 1000;
  const distance = km < 1 ? "under 1 km" : km < 10 ? `about ${Math.round(km)} km` : `about ${Math.round(km / 5) * 5} km`;
  return `${time} ${how}, ${distance}.`;
}

export const WALK_BETA_TEXT = "Walking times are a beta estimate and may be off. Check the route before you set out.";
export function showsBetaWarning(route: RouteCheck): boolean {
  return route.mode === "WALK" || route.beta_warning;
}

const STALE_AFTER_MINUTES = 30;
/** A display label only: traffic and opening hours change, so older checks are marked. */
export function freshness(fetchedAt: string, now: number): { label: string; stale: boolean } {
  const at = Date.parse(fetchedAt);
  if (Number.isNaN(at)) return { label: "Check time unknown", stale: true };
  const minutes = Math.max(0, Math.floor((now - at) / 60_000));
  const stale = minutes >= STALE_AFTER_MINUTES;
  const age = minutes < 1 ? "just now" : minutes === 1 ? "1 minute ago" : minutes < 90 ? `${minutes} minutes ago` : `${Math.round(minutes / 60)} hours ago`;
  return { label: stale ? `Checked ${age}. May be out of date, refresh before you go.` : `Checked ${age}.`, stale };
}

export function factStale(fact: Fact, now: number): boolean {
  if (!fact.expires_at) return false;
  const at = Date.parse(fact.expires_at);
  return !Number.isNaN(at) && at <= now;
}

export function factText(fact: Fact): string {
  return fact.value === null || fact.value === undefined ? fact.key : `${fact.key}: ${String(fact.value)}`;
}

/** Visible assumptions are anything the user has not confirmed; constraints are confirmed facts. */
export function splitFacts(facts: Fact[]): { assumptions: Fact[]; confirmed: Fact[] } {
  return {
    assumptions: facts.filter((f) => f.confirmation !== "CONFIRMED"),
    confirmed: facts.filter((f) => f.confirmation === "CONFIRMED"),
  };
}

/** Keeps a number a number when the user edits a numeric fact. */
export function parseFactValue(input: string, previous: Fact["value"]): string | number {
  const trimmed = input.trim();
  if (typeof previous === "number" && trimmed !== "" && Number.isFinite(Number(trimmed))) return Number(trimmed);
  return trimmed;
}

/** Whole seconds left, never negative. */
export function secondsLeft(expiresAt: string, now: number): number {
  const at = Date.parse(expiresAt);
  return Number.isNaN(at) ? 0 : Math.max(0, Math.ceil((at - now) / 1000));
}

export function expiryText(seconds: number): string {
  if (seconds <= 0) return "Expired. Ask again for a fresh preview.";
  if (seconds < 60) return "Expires in under a minute.";
  return `Expires in about ${Math.ceil(seconds / 60)} minutes.`;
}

/** Handoff links are only ever Google Maps on the pinned host, https, opened as given. */
export function isPinnedMapsUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "www.google.com" && (url.pathname === "/maps" || url.pathname.startsWith("/maps/"));
  } catch {
    return false;
  }
}

/** The URL the server returned at confirm time, or null. Read defensively: the field is not in the
 * frozen contract yet (see the web README gap list), and anything off the pinned host is dropped. */
export function handoffUrlFrom(response: ConfirmActionResponse): string | null {
  const bag = response as unknown as Record<string, unknown>;
  const action = response.action as PendingAction & Record<string, unknown>;
  for (const candidate of [bag.handoff_url, action.handoff_url, action.target]) {
    if (isPinnedMapsUrl(candidate)) return candidate;
  }
  return null;
}

export const OUTCOME_LABEL: Record<OutcomeValue, string> = {
  WENT: "I went",
  DID_NOT_GO: "I did not go",
  CHANGED_PLAN: "I changed plans",
};

export function localOutcome(value: OutcomeValue): OutingOutcome {
  return value === "WENT" ? "WENT" : value === "DID_NOT_GO" ? "SKIPPED" : "CHANGED_PLAN";
}

export const REMINDER_CHOICES: { label: string; minutes: number }[] = [
  { label: "In 30 minutes", minutes: 30 },
  { label: "In 1 hour", minutes: 60 },
  { label: "In 2 hours", minutes: 120 },
];
