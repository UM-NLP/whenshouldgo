// A narrow re-export of the storage layer's public types for the board (the storage code itself is
// owned elsewhere and is only consumed through its index).
export { SCHEMA_VERSION } from "../storage";
export type { LocalStore, ReminderRecord, OutingOutcome as OutcomeValueLocal, OutingLogRecord } from "../storage";
