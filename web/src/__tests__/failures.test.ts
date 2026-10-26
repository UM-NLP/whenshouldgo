import { describe, expect, it } from "vitest";

import { classifyFailure, failureText, retryFor } from "../api/failures";
import { classifyBoardFailure } from "../board/boardApi";

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
