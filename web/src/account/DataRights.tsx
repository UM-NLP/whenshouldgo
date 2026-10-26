import { useState } from "react";

import type { AuthService } from "../firebase";
import { downloadJson } from "../lib/download";
import type { AccountApi } from "./accountApi";
import { accountFailureText, DELETE_CONFIRM_TEXT, DELETE_DONE_TEXT, DELETE_INCOMPLETE_TEXT, EXPORT_BLURB } from "./copy";

type Status = { kind: "idle" } | { kind: "working" } | { kind: "ok"; text: string } | { kind: "error"; text: string };

/**
 * Export and delete of the account data on the server. They need no allowance, so they work while the
 * meter is GATED. The export file is built in memory from the response and never stored. Delete clears
 * everything on this device afterwards through `onDeleted` (the existing deleteAll).
 */
export function DataRights({ api, auth, onDeleted }: { api: AccountApi; auth: AuthService; onDeleted(): Promise<void> }) {
  const [exporting, setExporting] = useState<Status>({ kind: "idle" });
  const [deleting, setDeleting] = useState<Status>({ kind: "idle" });
  const [confirming, setConfirming] = useState(false);
  const [deleted, setDeleted] = useState(false);

  async function exportData() {
    setExporting({ kind: "working" });
    const outcome = await api.exportMe();
    if (!outcome.ok) return setExporting({ kind: "error", text: accountFailureText(outcome.failure) });
    downloadJson("whenshouldgo-account-data.json", outcome.data);
    setExporting({ kind: "ok", text: "Exported." });
  }

  async function deleteData() {
    setConfirming(false);
    setDeleting({ kind: "working" });
    const outcome = await api.deleteMe();
    if (!outcome.ok) return setDeleting({ kind: "error", text: accountFailureText(outcome.failure) });
    if (!outcome.data.completed) return setDeleting({ kind: "error", text: DELETE_INCOMPLETE_TEXT });
    try {
      await onDeleted();
    } catch {
      /* the server side is gone; local clearing is retried by the user's next delete */
    }
    await auth.signOut().catch(() => undefined);
    setDeleted(true);
    setDeleting({ kind: "ok", text: DELETE_DONE_TEXT });
  }

  if (deleted) {
    return (
      <section className="data-controls" aria-label="Your account data">
        <p role="status">{DELETE_DONE_TEXT}</p>
      </section>
    );
  }

  return (
    <section className="data-controls" aria-label="Your account data">
      <p className="muted">{EXPORT_BLURB}</p>
      <div className="notice-actions">
        <button type="button" onClick={() => void exportData()} disabled={exporting.kind === "working"}>
          {exporting.kind === "working" ? "Preparing..." : "Export my account data"}
        </button>
        <button type="button" className="secondary" onClick={() => setConfirming(true)} disabled={deleting.kind === "working"}>
          Delete my account data
        </button>
      </div>
      {exporting.kind === "ok" || exporting.kind === "error" ? (
        <p role={exporting.kind === "error" ? "alert" : "status"}>{exporting.text}</p>
      ) : null}
      {deleting.kind === "working" ? (
        <p className="loading" role="status">
          Deleting...
        </p>
      ) : null}
      {deleting.kind === "error" ? (
        <div className="notice notice-error" role="alert">
          <p>{deleting.text}</p>
          <button type="button" onClick={() => void deleteData()}>
            Try again
          </button>
        </div>
      ) : null}
      {confirming ? (
        <div className="notice notice-warning" role="alertdialog" aria-modal="true" aria-label="Delete my account data">
          <p>{DELETE_CONFIRM_TEXT}</p>
          <div className="notice-actions">
            <button type="button" onClick={() => void deleteData()}>
              Yes, delete everything
            </button>
            <button type="button" className="secondary" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
