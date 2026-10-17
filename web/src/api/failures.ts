import type { components } from "./schema";

export type ErrorBody = components["schemas"]["ErrorBody"];

/** How a failed request may be retried. "same_key" is safe: the server releases what a failed
 * turn reserved, and a repeated key never runs the turn twice. */
export type Retry = "same_key" | "new_key" | "none";

export type Failure =
  | { kind: "location_consent_required" }
  | { kind: "rate_limited"; retryAfterSeconds: number | null }
  | { kind: "over_budget" }
  | { kind: "quota" }
  | { kind: "auth" }
  | { kind: "busy" }
  | { kind: "result_expired" }
  | { kind: "discovery_unavailable" }
  | { kind: "not_configured" }
  | { kind: "provider" }
  | { kind: "invalid" }
  | { kind: "network" }
  | { kind: "unknown" };

export function parseRetryAfter(value: string | null): number | null {
  if (value === null || !/^\d{1,6}$/.test(value.trim())) return null;
  return Number(value.trim());
}

/** Clients switch on `reason`, then `code` and status; never on the message. An unknown
 * reason is treated like its code (the contract is additive). */
export function classifyFailure(
  status: number,
  body: Partial<ErrorBody> | undefined,
  retryAfterHeader: string | null,
): Failure {
  const reason = body?.reason ?? undefined;
  if (reason === "LOCATION_CONSENT_REQUIRED") return { kind: "location_consent_required" };
  if (status === 401 || status === 403) return { kind: "auth" };
  if (status === 429) {
    if (reason === "OVER_BUDGET") return { kind: "over_budget" };
    if (reason === "RATE_LIMITED" || reason === "PROVIDER_RATE_LIMITED") {
      return { kind: "rate_limited", retryAfterSeconds: parseRetryAfter(retryAfterHeader) };
    }
    return { kind: "quota" };
  }
  if (status === 409) return { kind: "busy" };
  if (status === 422) return { kind: "invalid" };
  if (reason === "RESULT_EXPIRED") return { kind: "result_expired" };
  if (reason === "SERVER_NOT_CONFIGURED") return { kind: "not_configured" };
  if (reason === "DISCOVERY_UNAVAILABLE") return { kind: "discovery_unavailable" };
  if (status >= 500) return { kind: "provider" };
  return { kind: "unknown" };
}

export function retryFor(failure: Failure): Retry {
  switch (failure.kind) {
    case "network":
    case "provider":
    case "busy":
    case "rate_limited":
    case "discovery_unavailable":
    case "location_consent_required":
      return "same_key";
    case "result_expired":
      return "new_key";
    default:
      return "none";
  }
}

/** Plain, static copy per failure. Server messages are not shown: the codes are enough. */
export function failureText(failure: Failure): string {
  switch (failure.kind) {
    case "location_consent_required":
      return "Using your location needs your OK first. Turn it on to continue, or name an area in your message.";
    case "rate_limited":
      return failure.retryAfterSeconds === null
        ? "You are sending requests too quickly. Wait a moment and try again."
        : `You are sending requests too quickly. Try again in ${failure.retryAfterSeconds} seconds.`;
    case "over_budget":
      return "That request needed more processing than one turn allows. Try a shorter or simpler message.";
    case "quota":
      return "You have reached a usage limit for now. Reading what is already here and opening Maps links still work.";
    case "auth":
      return "You are not signed in to the service, so this request was refused. Reload the page and try again.";
    case "busy":
      return "Your previous request is still running. Try again in a moment.";
    case "result_expired":
      return "That answer is no longer available. Send the request again for a fresh one.";
    case "discovery_unavailable":
      return "Place search is unavailable right now. Try again shortly.";
    case "not_configured":
      return "The service is not set up yet, so it cannot answer right now.";
    case "provider":
      return "Something went wrong on our side while answering. Your message was not lost; you can try again.";
    case "invalid":
      return "That message could not be sent. Check it and try again.";
    case "network":
      return "Could not reach the service. Check your connection and try again.";
    default:
      return "Something unexpected happened. You can try again.";
  }
}
