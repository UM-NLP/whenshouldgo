// Fixed copy for the account screens. Every string here is a constant chosen by state or failure
// kind; nothing the server or the sign-in provider returns is ever shown.
import { boardFailureText } from "../board/viewModels";
import type { AuthServiceFailureKind } from "../firebase";
import type { AccountFailure } from "./accountApi";

export const CREATE_ACCOUNT_LABEL = "Create a free account";

export const NOT_CONFIGURED_TEXT =
  "Sign-in is not configured in this build, so accounts are not available yet. You can keep using the app as a guest.";

export const LINK_INTRO =
  "A free account lets you recover your trips and keep them in sync across your devices. Google is the fastest way; an email link works too. No phone number is needed, and nothing is added to or taken from your allowance until you finish.";

export const CANCELLED_TEXT = "Cancelled. Nothing was changed and nothing was used from your allowance.";
export const LINKED_TEXT = "Your free account is ready. Your trips and allowance stayed with you.";
export const EMAIL_SENT_TEXT =
  "Check your inbox and open the link on this device. It opens this page, and you will be asked for your email once more to finish.";
export const EMAIL_RETURN_TEXT = "Enter the email address you used to finish creating your account.";

export const MERGE_INTRO =
  "That account already exists. Choose which guest trips to copy into it. Nothing is deleted from the guest session until everything you chose has been copied, and your guest allowance is not added to your account's.";
export const MERGE_STOP_NOTE =
  "If you stop now you stay signed in to your existing account and the guest trips on the server are not copied. Trips saved on this device stay here.";
export const MERGE_DONE_TEXT = "All chosen trips were copied. Your guest session was closed.";
export const MERGE_RESUME_TEXT = "Copying was interrupted. Continue and it picks up where it stopped; nothing is copied twice.";

export const PREFERENCE_KEEP = "Keep my account's saved settings (recommended)";
export const PREFERENCE_GUEST = "Use my guest settings instead";

export const EXPORT_BLURB = "Download everything the server holds about you as a file. Available at any time, including when your allowance is used up.";
export const DELETE_CONFIRM_TEXT =
  "Delete everything the server holds about you and everything saved on this device? This cannot be undone.";
export const DELETE_DONE_TEXT = "Your data was deleted from the server and from this device.";
export const DELETE_INCOMPLETE_TEXT = "Deleting is not finished yet. Try again to complete it.";

export function authFailureText(kind: AuthServiceFailureKind): string {
  switch (kind) {
    case "not_configured":
      return NOT_CONFIGURED_TEXT;
    case "cancelled":
      return CANCELLED_TEXT;
    case "popup_blocked":
      return "Your browser blocked the sign-in window. Allow pop-ups for this site and try again.";
    case "network":
      return "Could not reach the sign-in service. Check your connection and try again.";
    case "invalid_email":
      return "That email address does not look right. Check it and try again.";
    case "invalid_link":
      return "That email link is no longer valid. Request a new one.";
    case "unsupported":
      return "This sign-in method is not available yet.";
    case "collision":
      return "That account already exists.";
    default:
      return "Sign-in did not finish. Nothing was changed. You can try again.";
  }
}

export function accountFailureText(failure: AccountFailure): string {
  switch (failure.kind) {
    case "unavailable":
      return "Accounts are not available yet. You can keep using the app as a guest.";
    case "expired":
      return "That sign-in attempt timed out. Nothing was changed. Start again.";
    case "consumed":
      return "That sign-in attempt was already used. Reload the page to see your account.";
    case "wrong_uid":
      return "This browser's guest session is not the one that started sign-in. Reload the page and start again.";
    case "anonymous_provider":
      return "The sign-in did not produce a permanent account. Try again.";
    case "not_found":
      return "That sign-in attempt was not found. Start again.";
    case "interrupted":
      return MERGE_RESUME_TEXT;
    case "collision":
      return "That account already exists.";
    default:
      return boardFailureText(failure, "action");
  }
}

/** Failures that a plain retry of the same step can fix. */
export function accountRetryable(failure: AccountFailure): boolean {
  return ["network", "provider", "rate_limited", "unknown", "interrupted"].includes(failure.kind);
}
