# Current Project Board

Updated 2026-10-11. Authoritative board: [@UM-NLP's When Should Go — Project 2](https://github.com/users/UM-NLP/projects/2).

| Order | Issue | Board state | Dependency |
|---:|---|---|---|
| 0 | [#13 Agent-first personal trip assistant](https://github.com/UM-NLP/whenshouldgo/issues/13) | Active epic | — |
| 1 | [#14 Trip Workspace and agent runtime](https://github.com/UM-NLP/whenshouldgo/issues/14) | Next | — |
| 1 | [#22 Anonymous guest and registered-free tiers](https://github.com/UM-NLP/whenshouldgo/issues/22) | Next | Build with #14 |
| 1 | [#23 Push-to-talk, travel-only scope, and token budgets](https://github.com/UM-NLP/whenshouldgo/issues/23) | Next | Build with #14 |
| 2 | [#20 Same-day local outings](https://github.com/UM-NLP/whenshouldgo/issues/20) | Queued | #14, #22, #23 |
| 3 | [#21 Quality, safety, cost, and pilot](https://github.com/UM-NLP/whenshouldgo/issues/21) | Queued | #20 |
| 4 | [#11 Android product gate and MVP](https://github.com/UM-NLP/whenshouldgo/issues/11) | Gated | #21 exit decision |
| 5 | [#15 Calendar and explicit routines](https://github.com/UM-NLP/whenshouldgo/issues/15) | Future | Android/account/privacy gates |
| 5 | [#16 Event-driven monitoring and reminders](https://github.com/UM-NLP/whenshouldgo/issues/16) | Future | NOTIFY-01 (#34) plus cost gates |
| 5 | [#17 Same-day multi-stop optimizer](https://github.com/UM-NLP/whenshouldgo/issues/17) | Future | Demand evidence |
| 5 | [#18 Multi-day trip builder](https://github.com/UM-NLP/whenshouldgo/issues/18) | Future | Separate validation |
| 5 | [#19 Android voice entry](https://github.com/UM-NLP/whenshouldgo/issues/19) | Future | #23 and Android retention |

Updated 2026-10-17: the roadmap places trip history, patterns and routine reminder notifications before Android, and #16 depends on NOTIFY-01 (#34) rather than #15. Issues #24 to #35 (LEGAL-01 to LEGAL-07, LLMOPS-01, HISTORY-01, ROUTINE-01, NOTIFY-01, ACCESS-02) were created and added to the board; their column state on the board is the GitHub default.

Updated 2026-10-26: decisions 25 to 27 build Phase 1 (HISTORY-01, #32) ahead of its gate, dark behind `history_enabled`. The contract freeze and router split are done; the planned routes exist as typed 501 stubs for parallel implementation. Enabling history still needs Phase 0 exit and the signed legal checklist.

Updated 2026-10-26 (integration pass): the Phase 0 journey is built in code (see [handoff.md](handoff.md) and the backlog): Trip Board, actions with Maps handoff, account link, merge, export and delete, composition wiring, regenerated contract. Code status only, board columns are unchanged and still need updating on GitHub: #14 and #22 are largely built (open: model escalation; web sign-in and Firebase Admin credentials), #20 is code-complete pending the evaluation, #21 has not started, #35 is partly done in code (open: identity-creation and per-IP limits, shared per-principal windows, real Firestore proof). Phase 1 (#32) stays dark.

Updated 2026-10-26 (Phase 1 dark build): HISTORY-01 (#32) is code-complete but DARK: history, saved places, Home/Work, the fixed "near home" question and the deletion cascade are built behind `history_active(config)`. Enabling requires Phase 0 exit (#21), the signed legal checklist, an approved notice version added to `APPROVED_HISTORY_NOTICE_VERSIONS` and the Firestore TTL policy for `history_records` (runbook). Board column unchanged; the web screens are the open part.

Verified through the authenticated Projects interface on 2026-10-11: Project 2 contains only issues #11 and #13–#23. All 12 current cards are in Todo; Done, In Progress, and On Hold are empty. Repository issue state and content are maintained through the GitHub connector.
