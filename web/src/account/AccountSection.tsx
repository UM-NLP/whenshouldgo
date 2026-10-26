import { useState } from "react";

import type { Account } from "./useAccount";
import type { AccountApi } from "./accountApi";
import { useMerge } from "./useMerge";
import {
  CREATE_ACCOUNT_LABEL,
  EMAIL_RETURN_TEXT,
  EMAIL_SENT_TEXT,
  LINKED_TEXT,
  LINK_INTRO,
  MERGE_DONE_TEXT,
  MERGE_INTRO,
  MERGE_RESUME_TEXT,
  MERGE_STOP_NOTE,
  NOT_CONFIGURED_TEXT,
  PREFERENCE_GUEST,
  PREFERENCE_KEEP,
} from "./copy";

function EmailForm({ onSubmit, onBack, label, note }: { onSubmit(email: string): void; onBack?: () => void; label: string; note?: string }) {
  const [email, setEmail] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (email.trim()) onSubmit(email.trim());
      }}
    >
      <p>{label}</p>
      {note ? (
        <p className="notice notice-error" role="alert">
          {note}
        </p>
      ) : null}
      <p>
        <label>
          Email address{" "}
          <input type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
      </p>
      <div className="notice-actions">
        <button type="submit" disabled={!email.trim()}>
          Continue
        </button>
        {onBack ? (
          <button type="button" className="secondary" onClick={onBack}>
            Back
          </button>
        ) : null}
      </div>
    </form>
  );
}

function MergeScreen({ api, account, intent }: { api: AccountApi; account: Account; intent: { id: string; nonce: string } }) {
  const merge = useMerge(api, intent);
  const { state } = merge;
  const items = "view" in state && state.view ? state.view.items : [];
  const locked = state.phase === "running" || state.phase === "starting" || state.phase === "done";
  return (
    <div className="account-merge" aria-label="Combine with your existing account">
      <h3>Combine with your existing account</h3>
      <p>{MERGE_INTRO}</p>
      {state.phase === "starting" ? (
        <p className="loading" role="status">
          Loading what can be copied...
        </p>
      ) : null}
      {state.phase === "failed" ? (
        <p className="notice notice-error" role="alert">
          {state.message}
        </p>
      ) : null}
      {state.phase === "interrupted" ? (
        <p className="notice notice-warning" role="status">
          {MERGE_RESUME_TEXT}
        </p>
      ) : null}
      {state.phase === "done" ? (
        <p className="notice" role="status">
          {MERGE_DONE_TEXT}
        </p>
      ) : null}
      {state.phase === "running" ? (
        <p className="loading" role="status">
          Copying...
        </p>
      ) : null}

      {state.phase !== "starting" && state.phase !== "failed" ? (
        <>
          <fieldset disabled={locked}>
            <legend>Guest trips to copy</legend>
            {items.length === 0 ? <p className="muted">There are no guest trips to copy.</p> : null}
            {items.map((item) => (
              <p key={item.id}>
                <label>
                  <input type="checkbox" checked={merge.selected.has(item.id)} onChange={() => merge.toggle(item.id)} /> {item.title}
                </label>
                {item.copied ? <span className="muted"> (copied)</span> : null}
              </p>
            ))}
          </fieldset>
          <fieldset disabled={locked}>
            <legend>If a setting differs</legend>
            <p>
              <label>
                <input type="radio" name="merge-pref" checked={merge.preference === "KEEP_REGISTERED"} onChange={() => merge.setPreference("KEEP_REGISTERED")} />{" "}
                {PREFERENCE_KEEP}
              </label>
            </p>
            <p>
              <label>
                <input type="radio" name="merge-pref" checked={merge.preference === "USE_GUEST"} onChange={() => merge.setPreference("USE_GUEST")} />{" "}
                {PREFERENCE_GUEST}
              </label>
            </p>
          </fieldset>
        </>
      ) : null}

      <div className="notice-actions">
        {state.phase === "preview" ? <button type="button" onClick={merge.run}>Copy selected trips</button> : null}
        {state.phase === "interrupted" ? <button type="button" onClick={merge.run}>Continue copying</button> : null}
        {state.phase === "failed" && state.retryable ? (
          <button type="button" onClick={state.view ? merge.run : merge.restart}>
            Try again
          </button>
        ) : null}
        {state.phase === "done" ? (
          <button type="button" onClick={() => account.endMerge(true)}>
            Done
          </button>
        ) : (
          <button type="button" className="secondary" onClick={() => account.endMerge(false)} disabled={state.phase === "running"}>
            Stop for now
          </button>
        )}
      </div>
      {state.phase !== "done" ? <p className="muted">{MERGE_STOP_NOTE}</p> : null}
    </div>
  );
}

/** The registration flow. Renders nothing while closed. Every state has fixed copy. */
export function AccountSection({ account, api }: { account: Account; api: AccountApi }) {
  const { state } = account;
  if (state.step === "closed") return null;

  return (
    <section className="notice account-panel" role="dialog" aria-label={CREATE_ACCOUNT_LABEL}>
      <h2>{CREATE_ACCOUNT_LABEL}</h2>

      {state.step === "choose" ? (
        account.mode === "not_configured" ? (
          <>
            <p role="status">{NOT_CONFIGURED_TEXT}</p>
            <div className="notice-actions">
              <button type="button" className="secondary" onClick={account.close}>
                Close
              </button>
            </div>
          </>
        ) : (
          <>
            <p>{LINK_INTRO}</p>
            {state.note ? <p role="status">{state.note}</p> : null}
            <div className="notice-actions">
              <button type="button" onClick={account.chooseGoogle}>
                Continue with Google
              </button>
              <button type="button" className="secondary" onClick={account.showEmailForm}>
                Use an email link instead
              </button>
              <button type="button" className="secondary" onClick={account.close}>
                Not now
              </button>
            </div>
          </>
        )
      ) : null}

      {state.step === "email_form" ? (
        <EmailForm label="We will email you a sign-in link." note={state.note} onSubmit={account.sendEmail} onBack={() => account.open()} />
      ) : null}

      {state.step === "email_sent" ? (
        <>
          <p role="status">{EMAIL_SENT_TEXT}</p>
          <div className="notice-actions">
            <button type="button" className="secondary" onClick={account.close}>
              Close
            </button>
          </div>
        </>
      ) : null}

      {state.step === "email_return" ? (
        <>
          <EmailForm label={EMAIL_RETURN_TEXT} note={state.note} onSubmit={account.finishEmail} />
          <div className="notice-actions">
            <button type="button" className="secondary" onClick={account.close}>
              Cancel
            </button>
          </div>
        </>
      ) : null}

      {state.step === "working" ? (
        <p className="loading" role="status">
          {state.what === "starting" ? "Getting ready..." : state.what === "provider" ? "Waiting for sign-in..." : "Finishing..."}
        </p>
      ) : null}

      {state.step === "failed" ? (
        <>
          <p className="notice notice-error" role="alert">
            {state.message}
          </p>
          <div className="notice-actions">
            {state.retryable ? (
              <button type="button" onClick={account.retry}>
                Try again
              </button>
            ) : null}
            <button type="button" className="secondary" onClick={account.close}>
              Close
            </button>
          </div>
        </>
      ) : null}

      {state.step === "linked" ? (
        <>
          <p role="status">{LINKED_TEXT}</p>
          <div className="notice-actions">
            <button type="button" onClick={account.close}>
              Done
            </button>
          </div>
        </>
      ) : null}

      {state.step === "merge" ? <MergeScreen api={api} account={account} intent={state.intent} /> : null}
    </section>
  );
}
