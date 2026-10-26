import { useCallback, useEffect, useRef, useState } from "react";

import { AuthServiceError, type AuthMode, type AuthService } from "../firebase";
import { newIdempotencyKey } from "../lib/idempotency";
import type { AccountApi, AccountFailure, LinkProvider } from "./accountApi";
import { accountFailureText, accountRetryable, authFailureText, CANCELLED_TEXT } from "./copy";

/** Held in memory only. The nonce is never rendered, stored or logged. */
export interface IntentRef {
  id: string;
  nonce: string;
}

export type LinkStep =
  | { step: "closed" }
  | { step: "choose"; note?: string }
  | { step: "email_form"; note?: string }
  | { step: "email_sent" }
  | { step: "email_return"; note?: string }
  | { step: "working"; what: "starting" | "provider" | "finishing" }
  | { step: "linked" }
  | { step: "failed"; message: string; retryable: boolean }
  | { step: "merge"; intent: IntentRef };

export interface Account {
  mode: AuthMode;
  state: LinkStep;
  open(): void;
  /** Cancelling at any point consumes nothing: an unfinished intent just expires. */
  close(): void;
  chooseGoogle(): void;
  showEmailForm(): void;
  sendEmail(email: string): void;
  finishEmail(email: string): void;
  retry(): void;
  /** The merge screen finished or the user left it. */
  endMerge(completed: boolean): void;
}

/**
 * The registration flow: start a link intent, run the provider link, complete. A collision (the
 * credential already owns an account) goes to the explicit merge screen, never a silent merge.
 */
export function useAccount(api: AccountApi, auth: AuthService, onLinked: () => void): Account {
  const [state, setState] = useState<LinkStep>({ step: "closed" });
  const live = useRef(true);
  const lastAttempt = useRef<(() => void) | null>(null);
  const onLinkedRef = useRef(onLinked);
  onLinkedRef.current = onLinked;

  useEffect(() => {
    live.current = true;
    void auth.isEmailLinkReturn().then((back) => {
      if (live.current && back) setState({ step: "email_return" });
    });
    return () => {
      live.current = false;
    };
  }, [auth]);

  const fail = useCallback((failure: AccountFailure) => {
    setState({ step: "failed", message: accountFailureText(failure), retryable: accountRetryable(failure) });
  }, []);

  const collided = useCallback(
    async (intent: IntentRef) => {
      // Park the intent for this attempt (the server answers COLLISION), then sign in as the existing
      // account so the merge can prove both identities. Nothing is merged until the user chooses.
      await api.completeLink(intent.id, intent.nonce);
      try {
        await auth.continueAsExisting();
      } catch (error) {
        if (live.current) setState({ step: "failed", message: authFailureText(error instanceof AuthServiceError ? error.kind : "unknown"), retryable: false });
        return;
      }
      if (live.current) setState({ step: "merge", intent });
    },
    [api, auth],
  );

  const runLink = useCallback(
    (provider: LinkProvider, doLink: () => Promise<void>) => {
      const key = newIdempotencyKey();
      const attempt = async () => {
        setState({ step: "working", what: "starting" });
        const started = await api.startLinkIntent(provider, key);
        if (!live.current) return;
        if (!started.ok) return fail(started.failure);
        const intent: IntentRef = { id: started.data.id, nonce: started.data.nonce };
        setState({ step: "working", what: "provider" });
        try {
          await doLink();
        } catch (error) {
          if (!live.current) return;
          const kind = error instanceof AuthServiceError ? error.kind : "unknown";
          if (kind === "collision") return void (await collided(intent));
          if (kind === "cancelled") return setState({ step: "choose", note: CANCELLED_TEXT });
          if (kind === "not_configured") return setState({ step: "failed", message: authFailureText(kind), retryable: false });
          return setState({ step: "failed", message: authFailureText(kind), retryable: kind === "network" || kind === "unknown" });
        }
        if (!live.current) return;
        setState({ step: "working", what: "finishing" });
        const done = await api.completeLink(intent.id, intent.nonce);
        if (!live.current) return;
        if (done.ok) {
          setState({ step: "linked" });
          onLinkedRef.current();
        } else if (done.failure.kind === "collision") await collided(intent);
        else fail(done.failure);
      };
      lastAttempt.current = () => void attempt();
      void attempt();
    },
    [api, fail, collided],
  );

  return {
    mode: auth.mode,
    state,
    open: () => setState({ step: "choose" }),
    close: () => {
      lastAttempt.current = null;
      setState({ step: "closed" });
    },
    chooseGoogle: () => runLink("GOOGLE", () => auth.linkWithGoogle()),
    showEmailForm: () => setState({ step: "email_form" }),
    sendEmail: (email) => {
      const attempt = async () => {
        setState({ step: "working", what: "provider" });
        try {
          await auth.sendEmailLink(email);
          if (live.current) setState({ step: "email_sent" });
        } catch (error) {
          const kind = error instanceof AuthServiceError ? error.kind : "unknown";
          if (live.current) setState({ step: "email_form", note: authFailureText(kind) });
        }
      };
      lastAttempt.current = () => void attempt();
      void attempt();
    },
    finishEmail: (email) => runLink("EMAIL_LINK", () => auth.linkWithEmailLink(email)),
    retry: () => lastAttempt.current?.(),
    endMerge: (completed) => {
      if (completed) onLinkedRef.current();
      setState(completed ? { step: "linked" } : { step: "closed" });
    },
  };
}
