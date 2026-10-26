import type { BoardApi, Fact } from "../../board/boardApi";
import { useNow } from "../../board/useNow";
import {
  boardFailureText,
  canRetry,
  factStale,
  freshness,
  planWarningText,
  routeSummary,
  splitFacts,
} from "../../board/viewModels";
import type { Board } from "../../useBoard";
import type { LocationConsent } from "../../useLocationConsent";
import { ActionCard } from "./ActionCard";
import { CheckIn } from "./CheckIn";
import { AddFact, FactChips } from "./FactChips";
import { CheckView } from "./PlaceAndRoute";

interface Props {
  api: BoardApi;
  board: Board;
  location: LocationConsent;
}

const TIMELINE_KEYS: Record<string, string> = {
  leave_by: "Leave by",
  leave: "Leave by",
  arrive_by: "Arrive by",
  arrive: "Arrive by",
};

function timeline(facts: Fact[]): string[] {
  return facts.filter((f) => f.key in TIMELINE_KEYS && f.value != null).map((f) => `${TIMELINE_KEYS[f.key]} ${String(f.value)}`);
}

/** The Trip Board: goal, assumptions, constraints, up to three candidates, timing, freshness,
 * warnings, pending actions and the plan version. Text only; every state has fixed copy. */
export function TripBoard({ api, board, location }: Props) {
  const now = useNow();
  const { workspace } = board;
  const view = workspace.status === "ok" ? workspace.data : null;
  const facts = view?.facts ?? [];
  const { assumptions, confirmed } = splitFacts(facts);

  const warnings: string[] = [];
  if (view?.status === "UNSUPPORTED") warnings.push("This kind of request is not supported in this version, so the board may stay empty.");
  for (const code of view?.warnings ?? []) warnings.push(planWarningText(code));
  const staleFacts = facts.filter((f) => factStale(f, now)).length;
  if (staleFacts > 0) warnings.push(`${staleFacts} item${staleFacts === 1 ? "" : "s"} on the board may be out of date.`);
  for (const c of board.candidates) {
    const check = board.checks[c.placeId];
    if (check?.card.status === "ok" && freshness(check.card.data.fetched_at, now).stale) {
      warnings.push(`Details for ${c.label} may be out of date. Refresh before you go.`);
    }
  }

  const times = [
    ...timeline(facts),
    ...board.candidates.flatMap((c) => {
      const route = board.checks[c.placeId]?.route;
      return route?.status === "ok" ? [`${c.label}: ${routeSummary(route.data)}`] : [];
    }),
  ];
  const canAct = workspace.status === "ok";
  const busy = board.editing;

  return (
    <section className="board" aria-label="Trip Board">
      <h2>Trip Board</h2>

      {board.followUps.map((f) => (
        <CheckIn key={f.placeId} api={api} followUp={f} onFinish={board.finishFollowUp} />
      ))}

      {workspace.status === "loading" ? (
        <p className="loading" role="status">
          Loading your board...
        </p>
      ) : null}
      {workspace.status === "failed" ? (
        <div className={workspace.failure.kind === "unavailable" ? "notice" : "notice notice-error"} role="status">
          <p>{boardFailureText(workspace.failure, "workspace")}</p>
          {workspace.failure.kind !== "unavailable" && canRetry(workspace.failure) ? (
            <button type="button" onClick={board.reload}>
              Try again
            </button>
          ) : null}
        </div>
      ) : null}

      {view ? (
        <>
          <p className="goal">{view.goal ? `Goal: ${view.goal}` : "No goal yet. Tell the chat what you would like to do today."}</p>
          {board.conflict ? (
            <div className="notice notice-warning" role="alert">
              <p>{boardFailureText({ kind: "conflict" }, "edit")}</p>
              <button type="button" onClick={board.reload}>
                Reload the board
              </button>
            </div>
          ) : null}
          {board.editFailure ? (
            <div className="notice notice-error" role="alert">
              <p>{boardFailureText(board.editFailure, "edit")}</p>
              {canRetry(board.editFailure) ? (
                <button type="button" onClick={board.retryEdit}>
                  Try again
                </button>
              ) : null}
            </div>
          ) : null}
          <FactChips
            title="Assumptions"
            facts={assumptions}
            now={now}
            busy={busy}
            confirmable
            emptyText="No assumptions right now."
            onEdit={board.edit}
          />
          <FactChips
            title="Confirmed constraints"
            facts={confirmed}
            now={now}
            busy={busy}
            emptyText="Nothing confirmed yet. Add what you need and it will be treated as firm."
            onEdit={board.edit}
          />
          <AddFact busy={busy} onAdd={(edit) => board.edit([edit], [])} />
        </>
      ) : null}

      <section aria-label="Places">
        <h3>Places</h3>
        {board.candidates.length === 0 ? (
          <p className="muted">No places yet. After a search, tap &quot;Check this place&quot; on a result to add it here.</p>
        ) : null}
        {board.candidateNotice ? (
          <p className="notice notice-warning" role="status">
            {board.candidateNotice}
          </p>
        ) : null}
        {board.proposeFailure ? (
          <p className="notice notice-error" role="alert">
            {boardFailureText(board.proposeFailure, "action")}
          </p>
        ) : null}
        <ul className="candidates">
          {board.candidates.map((c) => (
            <li key={c.placeId} className="candidate">
              <label>
                <span className="sr-only">Your name for this place</span>
                <input value={c.label} maxLength={60} onChange={(e) => board.renameCandidate(c.placeId, e.target.value)} />
              </label>
              <CheckView
                check={board.checks[c.placeId]}
                location={location}
                now={now}
                onRefresh={() => board.checkPlace(c.placeId)}
                onCheckRoute={() => board.checkRoute(c.placeId)}
              />
              <div className="notice-actions">
                <button type="button" className="secondary" onClick={() => board.checkPlace(c.placeId)}>
                  Refresh
                </button>
                <button type="button" disabled={!canAct} onClick={() => board.propose("LAUNCH_HANDOFF", c.placeId)}>
                  Open in Google Maps
                </button>
                <button type="button" className="secondary" disabled={!canAct} onClick={() => board.propose("SET_REMINDER", c.placeId)}>
                  Remind me
                </button>
                <button type="button" className="secondary" onClick={() => board.removeCandidate(c.placeId)}>
                  Remove
                </button>
              </div>
              {!canAct ? <p className="muted">Actions are not available until the board loads.</p> : null}
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Timeline">
        <h3>Timeline</h3>
        {times.length === 0 ? (
          <p className="muted">No times yet. Check a place to see the drive time.</p>
        ) : (
          <ul>
            {times.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Warnings">
        <h3>Warnings</h3>
        {warnings.length === 0 ? (
          <p className="muted">Nothing to flag.</p>
        ) : (
          <ul>
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Pending actions">
        <h3>Pending actions</h3>
        {board.actions.length === 0 ? <p className="muted">Nothing waiting for your OK.</p> : null}
        {board.actions.map((a) => (
          <ActionCard key={a.action.id} state={a} board={board} now={now} />
        ))}
      </section>

      {view ? (
        <section aria-label="Plan history">
          <h3>Plan history</h3>
          <p>{view.latest_plan ? `Plan version ${view.latest_plan.version}.` : `No plan yet. Board version ${view.version}.`}</p>
          {(view.plan_versions ?? []).length > 1 ? (
            <p className="muted">{`Earlier versions: ${(view.plan_versions ?? []).slice(0, -1).join(", ")}.`}</p>
          ) : (
            <p className="muted">No earlier versions.</p>
          )}
          {view.status === "PROPOSED" || view.status === "CONFIRMED" ? (
            <div>
              <button type="button" disabled={board.activation.status === "loading" || board.activation.status === "ok"} onClick={board.activate}>
                Start this trip
              </button>
              {board.activation.status === "failed" ? (
                <p className="notice notice-error" role="alert">
                  {boardFailureText(board.activation.failure, "activate")}
                </p>
              ) : null}
              {board.activation.status === "ok" ? <p role="status">Trip started.</p> : null}
            </div>
          ) : null}
        </section>
      ) : null}
    </section>
  );
}
