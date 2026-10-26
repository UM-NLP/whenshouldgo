import { isPinnedMapsUrl, REMINDER_CHOICES, REMINDER_COPY, boardFailureText, expiryText, secondsLeft } from "../../board/viewModels";
import { LOCAL_NOTICE_TEXT } from "../../storage";
import type { ActionState, Board } from "../../useBoard";

function timeText(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

const TITLE = {
  LAUNCH_HANDOFF: "Open in Google Maps",
  SET_REMINDER: "Set a reminder",
  SAVE_PLACE: "Save this place",
} as const;

/** A pending action: the server's preview, explicit Confirm and Cancel, and the expiry. The nonce is
 * held in memory by the hook and never rendered. */
export type ActionCardBoard = Pick<Board, "confirm" | "cancel" | "setDue" | "acceptNotice" | "declineNotice">;

export function ActionCard({ state, board, now }: { state: ActionState; board: ActionCardBoard; now: number }) {
  const { action, phase } = state;
  const left = secondsLeft(action.expires_at, now);
  const expired = left <= 0;
  const id = action.id;
  const title = TITLE[action.effect];

  return (
    <section className="notice action-card" aria-label={title}>
      <h4>{title}</h4>
      <p className="muted">For: {state.label}</p>

      {phase === "preview" || phase === "confirming" || phase === "failed" ? (
        <>
          <p className="action-preview">{action.preview}</p>
          {action.effect === "SET_REMINDER" ? (
            <p>
              <label>
                Remind me{" "}
                <select
                  value={String(state.dueMinutes ?? 60)}
                  onChange={(e) => board.setDue(id, Number(e.target.value))}
                >
                  {REMINDER_CHOICES.map((c) => (
                    <option key={c.minutes} value={String(c.minutes)}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </label>{" "}
              <span className="muted">{state.dueAt ? `Around ${timeText(state.dueAt)}, on this device.` : ""}</span>
            </p>
          ) : null}
          <p className={expired ? "stale" : "muted"} role="status">
            {expiryText(left)}
          </p>
          {phase === "failed" && state.failure ? (
            <p className="notice notice-error" role="alert">
              {boardFailureText(state.failure, "action")}
            </p>
          ) : null}
          <div className="notice-actions">
            <button type="button" onClick={() => board.confirm(id)} disabled={phase === "confirming" || expired}>
              {phase === "confirming" ? "Working..." : phase === "failed" && state.failure?.kind !== "conflict" ? "Confirm again" : "Confirm"}
            </button>
            <button type="button" className="secondary" onClick={() => board.cancel(id)} disabled={phase === "confirming"}>
              Cancel
            </button>
          </div>
        </>
      ) : null}

      {phase === "cancelled" ? <p role="status">Cancelled. Nothing was done.</p> : null}

      {phase === "done" && action.effect === "LAUNCH_HANDOFF" ? (
        state.handoffUrl && isPinnedMapsUrl(state.handoffUrl) ? (
          <p role="status">
            Opened in Google Maps in a new tab.{" "}
            <a href={state.handoffUrl} target="_blank" rel="noopener noreferrer">
              Open Google Maps again
            </a>
          </p>
        ) : (
          <p className="notice notice-error" role="alert">
            Confirmed, but no Google Maps link came back. Ask again to get one.
          </p>
        )
      ) : null}

      {phase === "done" && action.effect === "SET_REMINDER" ? (
        <>
          {state.reminder === "saved" ? (
            <p role="status">
              Reminder saved on this device{state.dueAt ? ` for around ${timeText(state.dueAt)}` : ""}. {REMINDER_COPY}
            </p>
          ) : state.reminder === "needs_notice" ? (
            <div role="status">
              <p>{LOCAL_NOTICE_TEXT}</p>
              <p>Your reminder will be saved on this device only.</p>
              <div className="notice-actions">
                <button type="button" onClick={() => board.acceptNotice(id)}>
                  OK, save on this device
                </button>
                <button type="button" className="secondary" onClick={() => board.declineNotice(id)}>
                  Do not save
                </button>
              </div>
            </div>
          ) : (
            <p className="notice notice-warning" role="status">
              The reminder was confirmed but not saved, because this browser is not saving on this device.
            </p>
          )}
        </>
      ) : null}
      {phase === "done" && action.effect === "SAVE_PLACE" ? <p role="status">Done.</p> : null}
    </section>
  );
}
