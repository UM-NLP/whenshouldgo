import { describe, expect, it } from "vitest";

import { classifyFailure, failureText, GUEST_NOT_ALLOWED_TEXT, HISTORY_CONSENT_TEXT, retryFor } from "../api/failures";
import { classifyBoardFailure } from "../board/boardApi";
import { boardFailureText, ownLabelOf } from "../board/viewModels";
import { HISTORY_NOTICE_VERSION, NOTICE_VERSIONS, SAVED_PLACE_NOTICE_VERSION, saveTargetId } from "../history/contractAdapter";

const body = (reason?: string) => ({ schema_version: 1, kind: "error", code: "PROVIDER", message: "RAW", reason }) as never;

describe("classifyFailure: not available yet", () => {
  it("maps 501, 503 and the typed not-built and switched-off reasons to 'unavailable'", () => {
    expect(classifyFailure(501, body("NOT_IMPLEMENTED"), null)).toEqual({ kind: "unavailable" });
    expect(classifyFailure(501, undefined, null)).toEqual({ kind: "unavailable" });
    expect(classifyFailure(404, body("DISABLED"), null)).toEqual({ kind: "unavailable" });
    expect(classifyFailure(503, body("DISABLED"), null)).toEqual({ kind: "unavailable" });
    expect(classifyFailure(503, undefined, null)).toEqual({ kind: "unavailable" });
  });

  it("keeps a transient provider 503 retryable and a configured-off server distinct", () => {
    expect(classifyFailure(503, body("PROVIDER_UNAVAILABLE"), null)).toEqual({ kind: "provider" });
    expect(classifyFailure(503, body("DISCOVERY_UNAVAILABLE"), null)).toEqual({ kind: "discovery_unavailable" });
    expect(classifyFailure(503, body("SERVER_NOT_CONFIGURED"), null)).toEqual({ kind: "not_configured" });
    expect(classifyFailure(500, undefined, null)).toEqual({ kind: "provider" });
    expect(retryFor({ kind: "unavailable" })).toBe("none");
    expect(retryFor({ kind: "provider" })).toBe("same_key");
  });

  it("has fixed copy that never carries server text", () => {
    const text = failureText({ kind: "unavailable" });
    expect(text).toMatch(/not available yet/);
    expect(text).not.toContain("RAW");
  });

  it("the board treats an unconfigured service (for example Places) as 'not available yet'", () => {
    expect(classifyBoardFailure(503, body("SERVER_NOT_CONFIGURED"), null)).toEqual({ kind: "unavailable" });
    expect(classifyBoardFailure(503, undefined, null)).toEqual({ kind: "unavailable" });
    expect(classifyBoardFailure(503, body("PROVIDER_UNAVAILABLE"), null)).toEqual({ kind: "provider" });
    expect(classifyBoardFailure(409, body(), null)).toEqual({ kind: "conflict" });
  });
});

describe("Phase 1 403 reasons", () => {
  it("GUEST_NOT_ALLOWED and CONSENT_REQUIRED are their own kinds with fixed copy, not generic auth", () => {
    expect(classifyFailure(403, body("GUEST_NOT_ALLOWED"), null)).toEqual({ kind: "guest_not_allowed" });
    expect(classifyFailure(403, body("CONSENT_REQUIRED"), null)).toEqual({ kind: "history_consent_required" });
    expect(classifyFailure(403, body("LOCATION_CONSENT_REQUIRED"), null)).toEqual({ kind: "location_consent_required" });
    expect(classifyFailure(403, body(), null)).toEqual({ kind: "auth" });
    expect(classifyBoardFailure(403, body("GUEST_NOT_ALLOWED"), null)).toEqual({ kind: "guest_not_allowed" });
    expect(classifyBoardFailure(403, body("CONSENT_REQUIRED"), null)).toEqual({ kind: "history_consent_required" });
    // Neither is a location-consent failure: the location prompt must not open for them.
    expect(classifyBoardFailure(403, body("CONSENT_REQUIRED"), null).kind).not.toBe("consent_required");
    expect(boardFailureText({ kind: "guest_not_allowed" }, "action")).toBe(GUEST_NOT_ALLOWED_TEXT);
    expect(boardFailureText({ kind: "history_consent_required" }, "action")).toBe(HISTORY_CONSENT_TEXT);
    expect(failureText({ kind: "guest_not_allowed" })).toBe(GUEST_NOT_ALLOWED_TEXT);
    expect(failureText({ kind: "history_consent_required" })).toBe(HISTORY_CONSENT_TEXT);
    expect(GUEST_NOT_ALLOWED_TEXT).not.toContain("RAW");
    expect(retryFor({ kind: "guest_not_allowed" })).toBe("none");
  });
});

describe("contract adapter", () => {
  it("uses the one notice version the server accepts for both history purposes (placeholder pending counsel)", () => {
    expect(HISTORY_NOTICE_VERSION).toBe("history-notice-1");
    expect(SAVED_PLACE_NOTICE_VERSION).toBe(HISTORY_NOTICE_VERSION);
    expect(NOTICE_VERSIONS).toEqual({ HISTORY_STORE: "history-notice-1", SAVED_PLACE: "history-notice-1" });
  });

  it("builds a SAVE_PLACE target that matches the server's place-ID pattern and invents nothing for a custom place", () => {
    const pattern = /^[A-Za-z0-9_-]{1,300}$/;
    expect(saveTargetId("HOME")).toMatch(pattern);
    expect(saveTargetId("WORK")).toMatch(pattern);
    expect(saveTargetId("HOME")).not.toBe(saveTargetId("WORK"));
    expect(saveTargetId("CUSTOM")).toBeNull();
    expect(saveTargetId("CUSTOM", "ChIJpicked")).toBe("ChIJpicked");
  });

  it("sends only the user's own words as a label, never the board's placeholder", () => {
    expect(ownLabelOf("Dinner spot")).toBe("Dinner spot");
    expect(ownLabelOf("My pick 2")).toBeNull();
    expect(ownLabelOf("My place")).toBeNull();
    expect(ownLabelOf("  ")).toBeNull();
    expect(ownLabelOf(undefined)).toBeNull();
  });
});
