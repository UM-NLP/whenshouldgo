import { useRef, useState } from "react";

import type { BoardApi, OutcomeValue } from "../../board/boardApi";
import { boardFailureText, canRetry, ownLabelOf, OUTCOME_LABEL } from "../../board/viewModels";
import { newIdempotencyKey } from "../../lib/idempotency";
import type { BoardFailure } from "../../board/boardApi";
import type { FollowUp } from "../../useBoard";

const CHOICES: OutcomeValue[] = ["WENT", "DID_NOT_GO", "CHANGED_PLAN"];

interface Props {
  api: BoardApi;
  followUp: FollowUp;
  /** `localOutcome` is set when the report should go in the on-device outing log. */
  onFinish(placeId: string, localOutcome: OutcomeValue | null): void;
}

/** "Did you go?" The note is sent to the server once and is never stored, logged or echoed. */
export function CheckIn({ api, followUp, onFinish }: Props) {
  const [choice, setChoice] = useState<OutcomeValue | null>(null);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<BoardFailure | null>(null);
  // One key per check-in; every retry of this report carries it.
  const key = useRef(newIdempotencyKey());

  async function submit() {
    if (!choice || sending) return;
    setSending(true);
    setFailure(null);
    const outcome = await api.recordOutcome({
      placeId: followUp.placeId,
      outcome: choice,
      note: note.trim() ? note.trim() : null,
      idempotencyKey: key.current,
      userLabel: ownLabelOf(followUp.label),
      intentTag: followUp.intentTag ?? null,
    });
    setSending(false);
    if (outcome.ok) {
      // The server's ack decides: if it did not keep the report (guests), keep it on this device.
      onFinish(followUp.placeId, outcome.data.recorded ? null : choice);
    } else if (outcome.failure.kind === "unavailable") {
      onFinish(followUp.placeId, choice);
    } else setFailure(outcome.failure);
  }

  return (
    <section className="notice checkin" aria-label="Did you go?">
      <h4>Did you go?</h4>
      <p className="muted">{followUp.label}</p>
      <div className="notice-actions" role="group" aria-label="What happened">
        {CHOICES.map((value) => (
          <button
            key={value}
            type="button"
            className={choice === value ? "" : "secondary"}
            aria-pressed={choice === value}
            onClick={() => setChoice(value)}
          >
            {OUTCOME_LABEL[value]}
          </button>
        ))}
      </div>
      <label>
        <span className="sr-only">Optional note</span>
        <input
          value={note}
          maxLength={200}
          placeholder="Optional note"
          autoComplete="off"
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      {failure ? (
        <p className="notice notice-error" role="alert">
          {boardFailureText(failure, "outcome")}
        </p>
      ) : null}
      <div className="notice-actions">
        <button type="button" onClick={() => void submit()} disabled={!choice || sending}>
          {failure && canRetry(failure) ? "Try again" : "Send"}
        </button>
        <button type="button" className="secondary" onClick={() => onFinish(followUp.placeId, null)} disabled={sending}>
          Skip
        </button>
      </div>
    </section>
  );
}
