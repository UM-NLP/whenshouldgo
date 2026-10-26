// The account slice of the backend contract (docs/access-tiers.md): link intents, merge, export and
// delete. Types come from the generated schema. Server message text is never kept or shown.
import type { ErrorBody } from "../api/failures";
import type { components } from "../api/schema";
import { classifyBoardFailure, type BoardFailure } from "../board/boardApi";
import { createCaller, type CallerOptions, type Result } from "../lib/typedCall";

type S = components["schemas"];
export type AccountLinkIntent = S["AccountLinkIntent"];
export type AccountLinkResult = S["AccountLinkResult"];
export type LinkProvider = S["LinkProvider"];
export type MigrationView = S["MigrationView"];
export type MigrationItem = S["MigrationItemView"];
export type PreferenceChoice = S["PreferenceChoice"];
export type AccountExport = S["ExportBundle"];
export type DeleteReceipt = S["DeleteReceipt"];

export type AccountFailure =
  | BoardFailure
  | { kind: "collision" }
  | { kind: "expired" }
  | { kind: "consumed" }
  | { kind: "wrong_uid" }
  | { kind: "anonymous_provider" }
  | { kind: "interrupted" }
  | { kind: "not_found" };

export type AccountOutcome<T> = Result<T, AccountFailure>;

export function classifyAccountFailure(
  status: number,
  body: Partial<ErrorBody> | undefined,
  retryAfter: string | null,
): AccountFailure {
  switch (body?.reason ?? undefined) {
    case "ACCOUNT_COLLISION":
      return { kind: "collision" };
    case "LINK_INTENT_EXPIRED":
      return { kind: "expired" };
    case "LINK_INTENT_CONSUMED":
      return { kind: "consumed" };
    case "WRONG_UID":
      return { kind: "wrong_uid" };
    case "ANONYMOUS_PROVIDER":
      return { kind: "anonymous_provider" };
    case "MIGRATION_INTERRUPTED":
      return { kind: "interrupted" };
    default:
      break;
  }
  if (status === 404 && body?.reason !== "DISABLED") return { kind: "not_found" };
  return classifyBoardFailure(status, body, retryAfter);
}

export interface AccountApi {
  /** One key per attempt; a retry of the same attempt repeats it (the server replays the intent). */
  startLinkIntent(provider: LinkProvider, idempotencyKey: string): Promise<AccountOutcome<AccountLinkIntent>>;
  completeLink(intentId: string, nonce: string): Promise<AccountOutcome<AccountLinkResult>>;
  startMigration(intentId: string, nonce: string): Promise<AccountOutcome<MigrationView>>;
  /** The same call resumes an interrupted run. */
  runMigration(id: string, itemIds: string[], preference: PreferenceChoice): Promise<AccountOutcome<MigrationView>>;
  getMigration(id: string): Promise<AccountOutcome<MigrationView>>;
  exportMe(): Promise<AccountOutcome<AccountExport>>;
  deleteMe(): Promise<AccountOutcome<DeleteReceipt>>;
}

export function createAccountApi(options: CallerOptions): AccountApi {
  const { client, call } = createCaller<AccountFailure>(
    options,
    classifyAccountFailure,
    { kind: "auth" },
    { kind: "network" },
    { kind: "unknown" },
  );
  return {
    startLinkIntent: (provider, key) =>
      call((authorization) =>
        client.POST("/v1/account/link-intents", {
          params: { header: { authorization, "idempotency-key": key } },
          body: { schema_version: 1, provider },
        }),
      ),
    completeLink: (intentId, nonce) =>
      call((authorization) =>
        client.POST("/v1/account/link-intents/{id}:complete", {
          params: { header: { authorization }, path: { id: intentId } },
          body: { schema_version: 1, nonce },
        }),
      ),
    startMigration: (intentId, nonce) =>
      call((authorization) =>
        client.POST("/v1/account-migrations", {
          params: { header: { authorization } },
          body: { schema_version: 1, intent_id: intentId, nonce },
        }),
      ),
    runMigration: (id, itemIds, preference) =>
      call((authorization) =>
        client.POST("/v1/account-migrations/{id}:run", {
          params: { header: { authorization }, path: { id } },
          body: { schema_version: 1, item_ids: itemIds, preference },
        }),
      ),
    getMigration: (id) =>
      call((authorization) =>
        client.GET("/v1/account-migrations/{id}", { params: { header: { authorization }, path: { id } } }),
      ),
    exportMe: () => call((authorization) => client.POST("/v1/me:export", { params: { header: { authorization } } })),
    deleteMe: () => call((authorization) => client.POST("/v1/me:delete", { params: { header: { authorization } } })),
  };
}

/** Everything is "not available yet" (used when no account API is wired). */
export const UNAVAILABLE_ACCOUNT_API: AccountApi = (() => {
  const no = async (): Promise<{ ok: false; failure: AccountFailure }> => ({ ok: false, failure: { kind: "unavailable" } });
  return {
    startLinkIntent: no,
    completeLink: no,
    startMigration: no,
    runMigration: no,
    getMigration: no,
    exportMe: no,
    deleteMe: no,
  };
})();
