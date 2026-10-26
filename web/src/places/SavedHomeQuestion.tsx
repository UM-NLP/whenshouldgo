import type { AskKind } from "../history/contractAdapter";
import { QUESTION_TEXT } from "./SavedAskPolicy";

/**
 * The fixed question. It is a constant, never model text, and it appears only when the server (or the
 * guest's local deterministic rule) said so. "Not now" starts the 30-day cooldown; "Don't ask again"
 * stops it for that label.
 */
export function SavedHomeQuestion({
  kind,
  text,
  onYes,
  onLater,
  onNever,
}: {
  kind: AskKind;
  /** The server's fixed question text for a registered user; the same constant for a guest. */
  text?: string;
  onYes(): void;
  onLater(): void;
  onNever(): void;
}) {
  const label = kind === "HOME" ? "Home" : "Work";
  return (
    <section className="notice" role="group" aria-label={`Save ${label}?`}>
      <p>{text ?? QUESTION_TEXT[kind]}</p>
      <div className="notice-actions">
        <button type="button" onClick={onYes}>
          Yes, save as {label}
        </button>
        <button type="button" className="secondary" onClick={onLater}>
          Not now
        </button>
        <button type="button" className="secondary" onClick={onNever}>
          Don&apos;t ask again
        </button>
      </div>
    </section>
  );
}
