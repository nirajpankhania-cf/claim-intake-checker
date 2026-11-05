# Decisions log

| Date | Decision | Source |
|---|---|---|
| 2026-11-04 | Domain is car insurance; team defines the data shape; synthetic data only | Karn, meeting |
| 2026-11-04 | Tool flags and reports only. No approve/deny/pay actions anywhere in code or UI | Brief; confirmed by Karn |
| 2026-11-04 | Two outcomes per claim: READY_FOR_ADJUSTER (no flags) or NEEDS_REVIEW (≥1 flag) | Karn, meeting |
| 2026-11-04 | History records every claim's outcome per run, not just flags (leadership view) | Karn, meeting |
| 2026-11-04 | Claimed amount is captured and displayed, not verified | Karn, meeting |
| 2026-11-04 | Primary intake is one claim via form; CSV upload is the batch path | Karn, meeting |
| 2026-11-04 | Attachments stored as file names only; never opened or parsed | Team |
| 2026-11-04 | Default `REPORTING_WINDOW_DAYS = 30` — pending Karn | Team default |
| 2026-11-04 | Default `EXPIRY_DAY_COVERED = True` — pending Karn | Team default |
| 2026-11-04 | Duplicate = different claim_id, same policy_number + loss_date; later claim gets the flag, reason names the earlier one | Team |
| 2026-11-04 | Re-submitted claim_id is skipped and logged, not re-checked | Team |
| 2026-11-04 | Re-running checks creates a new run; nothing is updated or deleted | Team |
| 2026-11-05 | R1 and R1b return nothing when the policy is missing; R0 is the only flag for an unknown policy | Niraj, PR #3 |
| 2026-11-05 | A cover type absent from config covers nothing (flags, doesn't crash) | Niraj, PR #3 |
| 2026-11-05 | R4 amount-over-limit is stretch only, pending Karn's answer | Team |

## Scenarios

| # | Scenario | Rule | Outcome |
|---|---|---|---|
| S1 | Policy number not found | R0 | Needs review |
| S2 | Loss before policy effective date | R1 | Needs review |
| S3 | Loss after expiry or on/after cancellation | R1 | Needs review |
| S4 | Loss on the expiry date itself | R1 | Covered by default — pending Karn |
| S5 | Loss type not covered by cover type | R1b | Needs review |
| S6 | Reported more than 30 days after loss | R2 | Needs review |
| S7 | Different claim ID, same policy + loss date | R3 | Needs review |
| S8 | Same claim ID submitted again | intake | Skipped, logged |
| S9 | Required field missing or date unparseable | intake | Needs review |
| S10 | Claimed amount above policy limit | R4 | Stretch — pending Karn |
| S11 | None of the above | — | Ready for adjuster |

## Open questions sent to Karn (4 Nov)

1. Is a loss on the expiry date itself covered?
2. Is 30 days the right reporting window?
3. Should amount above policy limit be flagged at intake?
