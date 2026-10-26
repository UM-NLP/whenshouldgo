import { boardFailureText } from "../board/viewModels";
import { useNow } from "../board/useNow";
import { ActionCard } from "../components/board/ActionCard";
import { SAVED_PLACE_NOTICE_TEXT, SAVED_PLACE_PLACEHOLDER_FLAG } from "../history/copy";
import { LOCAL_NOTICE_TEXT } from "../storage";
import type { SaveFlow } from "./SavedPlaceFlow";

const NOOP = () => undefined;

/** Every state of saving a place, in fixed copy. The confirm step is the board's own ActionCard. */
export function SavedPlaceFlowView({ flow, mode }: { flow: SaveFlow; mode: "device" | "server" }) {
  const now = useNow(15_000);
  const { state } = flow;
  if (state.step === "idle") return null;
  const where = mode === "device" ? "on this device" : "to your account";

  return (
    <div className="save-flow" aria-label="Save a place">
      {state.step === "needs_location" ? (
        <p className="notice" role="status">
          Saving a place uses your location once. Turn on location sharing above, then tap save again.
        </p>
      ) : null}
      {state.step === "geo" ? (
        <p className="notice notice-error" role="alert">
          {state.text}
        </p>
      ) : null}
      {state.step === "working" ? (
        <p className="loading" role="status">
          Working...
        </p>
      ) : null}
      {state.step === "needs_notice" ? (
        <div className="notice" role="dialog" aria-label="Saved on this device">
          <p>{LOCAL_NOTICE_TEXT}</p>
          <p>Your saved place will be kept on this device only.</p>
          <div className="notice-actions">
            <button type="button" onClick={flow.acceptNotice}>
              OK, save on this device
            </button>
            <button type="button" className="secondary" onClick={flow.declineNotice}>
              Do not save
            </button>
          </div>
        </div>
      ) : null}
      {state.step === "needs_consent" ? (
        <div className="notice" role="dialog" aria-label="Save places to my account">
          <p className="placeholder-flag">{SAVED_PLACE_PLACEHOLDER_FLAG}</p>
          <p>{SAVED_PLACE_NOTICE_TEXT}</p>
          <div className="notice-actions">
            <button type="button" onClick={flow.grantConsent}>
              Agree and continue
            </button>
            <button type="button" className="secondary" onClick={flow.dismiss}>
              Not now
            </button>
          </div>
        </div>
      ) : null}
      {state.step === "unsaveable" ? (
        <p className="notice notice-warning" role="status">
          This browser is not saving anything on this device, so a place cannot be saved here.
        </p>
      ) : null}
      {state.step === "action" ? <ActionCard state={state.state} board={{ confirm: flow.confirm, cancel: flow.cancel, setDue: NOOP, acceptNotice: NOOP, declineNotice: NOOP }} now={now} /> : null}
      {state.step === "saved" ? (
        <p role="status">
          {state.kind === "HOME" ? "Home" : state.kind === "WORK" ? "Work" : "Your place"} is saved {where}. It is never used unless you tap to use it.
        </p>
      ) : null}
      {state.step === "finish_failed" ? (
        <div className="notice notice-error" role="alert">
          <p>The save was confirmed but could not be stored {where}. Try storing it again.</p>
          <button type="button" onClick={flow.retryFinish}>
            Try again
          </button>
        </div>
      ) : null}
      {state.step === "failed" ? (
        <p className="notice notice-error" role="alert">
          {boardFailureText(state.failure, "action")}
        </p>
      ) : null}
      {state.step !== "action" && state.step !== "working" ? (
        <button type="button" className="secondary" onClick={flow.dismiss}>
          Close
        </button>
      ) : null}
    </div>
  );
}
