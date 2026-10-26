// When to ask "Is where you are now your home? Save it as Home?" (decision 24).
//
// The question is a fixed constant, never model text. For guests the asking rule is local and
// deterministic: the user's own message contains a "near home" / "near work" phrase, there is no saved
// Home/Work yet, the user has not chosen "don't ask again", and no decline happened in the last 30
// days. Asking after a user action is a confirmation, not an inference: nothing is stored from it
// unless the user confirms through the SAVE_PLACE action. For registered users the SERVER decides
// (see history/contractAdapter.ts, askSignalFromTurn).
import type { AskKind } from "../history/contractAdapter";
import { SCHEMA_VERSION, type LocalStore } from "../storage";

export const HOME_QUESTION = "Is where you are now your home? Save it as Home?";
export const WORK_QUESTION = "Is where you are now your work? Save it as Work?";
export const QUESTION_TEXT: Record<AskKind, string> = { HOME: HOME_QUESTION, WORK: WORK_QUESTION };
export const DECLINE_COOLDOWN_DAYS = 30;
const DAY_MS = 86_400_000;

const NEAR = String.raw`(?:near|nearby|close to|closest to|around|next to|by)`;
const HOME_RULE = new RegExp(String.raw`\b${NEAR}\s+(?:my\s+)?(?:home|house)\b`, "i");
const WORK_RULE = new RegExp(String.raw`\b${NEAR}\s+(?:my\s+|the\s+)?(?:work|office|workplace)\b`, "i");

/** The deterministic keyword rule. Looks only at the user's own message text. */
export function detectNearKind(text: string): AskKind | null {
  if (HOME_RULE.test(text)) return "HOME";
  if (WORK_RULE.test(text)) return "WORK";
  return null;
}

export interface AskState {
  dontAskAgain: boolean;
  /** When the user last declined the question; the cooldown runs from here. */
  declinedAt: string | null;
}

export const EMPTY_ASK_STATE: AskState = { dontAskAgain: false, declinedAt: null };

export function cooldownActive(state: AskState, now: number): boolean {
  if (!state.declinedAt) return false;
  const at = Date.parse(state.declinedAt);
  return Number.isNaN(at) ? true : now - at < DECLINE_COOLDOWN_DAYS * DAY_MS;
}

/** True when the question may be shown now. `hasSaved` means that label already exists. */
export function shouldAsk(state: AskState, hasSaved: boolean, now: number): boolean {
  return !hasSaved && !state.dontAskAgain && !cooldownActive(state, now);
}

export interface AskStore {
  get(kind: AskKind): Promise<AskState>;
  decline(kind: AskKind, now: number): Promise<void>;
  dontAskAgain(kind: AskKind): Promise<void>;
}

/**
 * Ask-state on the device (the `ask_state` store, keyed by label id). Writing needs the on-device
 * notice; without it the choice is kept in memory for this visit only, so a decline is still honoured.
 */
export function createAskStore(store: LocalStore | null): AskStore {
  const memory = new Map<AskKind, AskState>();
  async function read(kind: AskKind): Promise<AskState> {
    const held = memory.get(kind);
    if (held) return held;
    try {
      const row = await store?.get("ask_state", kind);
      if (row) return { dontAskAgain: row.dont_ask_again, declinedAt: row.last_asked_at };
    } catch {
      /* unreadable: treated as never asked */
    }
    return EMPTY_ASK_STATE;
  }
  async function write(kind: AskKind, next: AskState): Promise<void> {
    memory.set(kind, next);
    try {
      await store?.put("ask_state", {
        schema_version: SCHEMA_VERSION,
        key: kind,
        dont_ask_again: next.dontAskAgain,
        last_asked_at: next.declinedAt,
      });
    } catch {
      /* memory only for now */
    }
  }
  return {
    get: read,
    decline: async (kind, now) => write(kind, { ...(await read(kind)), declinedAt: new Date(now).toISOString() }),
    dontAskAgain: async (kind) => write(kind, { ...(await read(kind)), dontAskAgain: true }),
  };
}
