import { useEffect, useRef, useState, type FormEvent } from "react";

import type { Api } from "../api/client";
import { useChat } from "../useChat";
import { useLocalSession } from "../useLocalSession";
import { useServiceWorkerUpdate } from "../sw-register";
import type { LocalStore } from "../storage";
import { useLocationConsent } from "../useLocationConsent";
import { useUsage } from "../useUsage";
import { AssistantMessage } from "./AssistantMessage";
import { DataControls, LocalNoticeCard, OfflineBanner, OutboxStatus, UpdateBanner } from "./LocalFirst";
import { LocationControl } from "./LocationControl";
import { ProblemNotice } from "./ProblemNotice";
import { UsageMeter } from "./UsageMeter";

/** `local` is the on-device store; null (the default) keeps everything in memory only. */
export function Chat({ api, local = null }: { api: Api; local?: LocalStore | null }) {
  const location = useLocationConsent(api);
  const usage = useUsage(api);
  const session = useLocalSession(local, api);
  const chat = useChat(api, location, usage.update, session);
  const updateApply = useServiceWorkerUpdate();
  // The server's meter_state decides; the client computes nothing from the counters.
  const gated = usage.view?.meter_state === "GATED";
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  // Restore the unsent draft once.
  const draftRestored = useRef(false);
  useEffect(() => {
    if (session?.restoredDraft && !draftRestored.current) {
      draftRestored.current = true;
      setDraft((current) => current || session.restoredDraft || "");
    }
  }, [session?.restoredDraft]);

  // After the user grants consent following LOCATION_CONSENT_REQUIRED, send the held message again.
  const waitingForConsent = chat.problem?.type === "api" && chat.problem.failure.kind === "location_consent_required";
  const retried = useRef(false);
  useEffect(() => {
    if (location.phase === "on" && waitingForConsent && !retried.current) {
      retried.current = true;
      chat.retry();
    }
    if (!waitingForConsent) retried.current = false;
  }, [location.phase, waitingForConsent, chat]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [chat.items.length, chat.problem, chat.sending]);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim() || chat.sending || gated) return;
    chat.send(draft);
    setDraft("");
    session?.saveDraft("");
  }

  return (
    <main className="chat">
      <header>
        <h1>WhenShouldGo</h1>
        <p className="tagline">Same-day outings near you.</p>
      </header>

      <UpdateBanner apply={updateApply} />
      {session ? <OfflineBanner online={session.online} /> : null}
      {session ? <LocalNoticeCard session={session} /> : null}

      <div className="log" aria-live="polite" aria-label="Conversation">
        {chat.items.length === 0 && !chat.sending ? (
          <p className="empty">
            Ask about a place or an outing for today, for example a quiet place for dinner this evening in a
            neighbourhood you name.
          </p>
        ) : null}
        {chat.items.map((item) => {
          if (item.role === "user") {
            const waiting = !!item.key && !!session?.outboxItems.some((q) => q.key === item.key && q.state === "queued");
            return (
              <p key={item.id} className="user">
                {item.text}
                {waiting ? <span className="queued"> (waiting to send)</span> : null}
              </p>
            );
          }
          if (item.role === "restored") {
            return (
              <div key={item.id} className="assistant">
                <p className="reply">{item.text}</p>
                {item.hadDiscovery ? (
                  <p className="notice" role="status">
                    Results are not stored; ask again. Place details are unavailable offline.
                  </p>
                ) : null}
              </div>
            );
          }
          return <AssistantMessage key={item.id} response={item.response} />;
        })}
        {chat.sending ? (
          <p className="loading" role="status">
            Looking that up...
          </p>
        ) : null}
        {chat.problem ? (
          <ProblemNotice
            problem={chat.problem}
            sending={chat.sending}
            onRetry={chat.retry}
            onUseLocation={location.start}
          />
        ) : null}
        <div ref={endRef} />
      </div>

      {session ? <OutboxStatus session={session} onResume={chat.resumeQueued} onDiscard={chat.discardQueued} /> : null}

      {usage.view ? <UsageMeter usage={usage.view} /> : null}

      <LocationControl location={location} />

      <form className="composer" onSubmit={submit}>
        <label htmlFor="message" className="sr-only">
          Your message
        </label>
        <textarea
          id="message"
          value={draft}
          rows={2}
          maxLength={20000}
          disabled={gated}
          placeholder="What are you looking for?"
          onChange={(e) => {
            setDraft(e.target.value);
            session?.saveDraft(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <button type="submit" disabled={chat.sending || gated || !draft.trim()}>
          Send
        </button>
      </form>

      {session ? <DataControls session={session} /> : null}
    </main>
  );
}
