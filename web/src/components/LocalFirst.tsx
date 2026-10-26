import { useState } from "react";

import { failureText, type Failure } from "../api/failures";
import { LOCAL_NOTICE_TEXT } from "../storage";
import type { LocalSession } from "../useLocalSession";

/** Shown before the first local save; nothing is stored on the device until the user accepts. */
export function LocalNoticeCard({ session }: { session: LocalSession }) {
  if (session.notice.state !== "unset") return null;
  return (
    <section className="notice" aria-label="Saved on this device">
      <p>{LOCAL_NOTICE_TEXT}</p>
      <p>Your messages and drafts will be saved on this device only.</p>
      <div className="notice-actions">
        <button type="button" onClick={session.notice.accept}>
          OK, save on this device
        </button>
        <button type="button" onClick={session.notice.decline}>
          Do not save
        </button>
      </div>
    </section>
  );
}

export function OfflineBanner({ online }: { online: boolean }) {
  if (online) return null;
  return (
    <p className="notice notice-warning" role="status">
      You are offline. You can still read what is here; new messages are queued and sent when you are back online.
    </p>
  );
}

export function UpdateBanner({ apply }: { apply: (() => void) | null }) {
  if (!apply) return null;
  return (
    <p className="notice" role="status">
      A new version is available.{" "}
      <button type="button" onClick={apply}>
        Reload to update
      </button>
    </p>
  );
}

/** Queued and stopped turns. A stopped turn blocks the ones behind it until the user decides. */
export function OutboxStatus({
  session,
  onResume,
  onDiscard,
}: {
  session: LocalSession;
  onResume(key: string, newKey: boolean): void;
  onDiscard(key: string): void;
}) {
  const items = session.outboxItems;
  if (items.length === 0) return null;
  const queued = items.filter((i) => i.state === "queued").length;
  return (
    <div className="outbox" aria-label="Queued messages">
      {queued > 0 ? (
        <p className="notice" role="status">
          {queued === 1 ? "1 message is waiting to send." : `${queued} messages are waiting to send.`}
        </p>
      ) : null}
      {items
        .filter((i) => i.state === "stopped")
        .map((item) => (
          <div key={item.key} className="notice notice-error" role="alert">
            <p>
              Not sent: &ldquo;{item.text.slice(0, 80)}&rdquo;. {failureText({ kind: item.stopKind ?? "unknown", retryAfterSeconds: null } as Failure)}
            </p>
            <div className="notice-actions">
              <button type="button" onClick={() => onResume(item.key, item.stopKind === "result_expired")}>
                Try again
              </button>
              <button type="button" onClick={() => onDiscard(item.key)}>
                Discard
              </button>
            </div>
          </div>
        ))}
    </div>
  );
}

function download(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

/** Export and delete of everything on this device. Always available, including at the gate. */
export function DataControls({ session }: { session: LocalSession }) {
  const [confirming, setConfirming] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  async function exportData() {
    try {
      download("whenshouldgo-data.json", JSON.stringify(await session.exportData(), null, 2));
      setStatus("Exported.");
    } catch {
      setStatus("Could not export your data.");
    }
  }
  async function deleteData() {
    setConfirming(false);
    try {
      await session.deleteData();
      setStatus("Everything saved on this device was deleted.");
    } catch {
      setStatus("Could not delete your data.");
    }
  }

  return (
    <section className="data-controls" aria-label="Your data on this device">
      <div className="notice-actions">
        <button type="button" onClick={() => void exportData()}>
          Export my data
        </button>
        {confirming ? (
          <>
            <span>Delete everything saved on this device?</span>
            <button type="button" onClick={() => void deleteData()}>
              Yes, delete
            </button>
            <button type="button" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" onClick={() => setConfirming(true)}>
            Delete my data
          </button>
        )}
      </div>
      {status ? <p role="status">{status}</p> : null}
    </section>
  );
}
