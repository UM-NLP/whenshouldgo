import type { TurnResponse, TurnWarning } from "../api/client";
import { DiscoveryBlockView } from "./DiscoveryBlockView";

// Fixed client copy for the typed warnings. NEEDS_AREA is not a warning to the user: the reply
// already asks where to look. FACT_DROPPED changes nothing the user sees.
const WARNING_TEXT: Partial<Record<TurnWarning, string>> = {
  TOOL_BUDGET_EXHAUSTED: "Some results may be missing from this answer.",
  AREA_DROPPED: "The area in your message could not be used, so it was left out of the search.",
  QUERY_REJECTED: "That could not be turned into a search.",
};

export function AssistantMessage({ response }: { response: TurnResponse }) {
  const warnings = response.warnings.map((w) => WARNING_TEXT[w]).filter((t): t is string => !!t);
  return (
    <div className="assistant">
      {/* Server-authored framing: its own bubble, visually distinct from Google's block. */}
      <p className="reply">{response.reply}</p>
      {response.allowance_exhausted ? (
        <p className="notice notice-limit" role="status">
          You have reached your allowance for now. Everything above stays available, and Maps links still work.
        </p>
      ) : null}
      {response.discovery ? <DiscoveryBlockView block={response.discovery} /> : null}
      {warnings.map((text) => (
        <p key={text} className="notice notice-warning" role="status">
          {text}
        </p>
      ))}
    </div>
  );
}
