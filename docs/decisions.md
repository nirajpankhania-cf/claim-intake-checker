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
| 2026-11-05 | Schema lives in `app/schema.sql`; `app/db.py` uses plain `sqlite3`, no ORM | Team, PR #5 |
| 2026-11-05 | Dates stored as ISO text; money stored as text so `Decimal` round-trips exactly | Team, PR #5 |
| 2026-11-05 | `check_results` and `flags` are append-only, enforced by DB triggers as well as by having no UPDATE/DELETE helpers | Team, PR #5 |
| 2026-11-05 | `claims.policy_number` has no foreign key to `policies`: a claim on an unknown policy is valid input that R0 flags | Team, PR #5 |
| 2026-11-05 | DB helpers don't commit; the caller owns the transaction (`with conn:`) | Team, PR #5 |
| 2026-11-05 | Generator is deterministic (seed 4242, reference date 2026-10-31); generated `data/` and `*.db` are not committed | Team, PR #6 |
| 2026-11-05 | Scenario claims are tagged `SCENARIO Sn:` in `description` for demo use only; rules never read them | Team, PR #6 |
| 2026-11-05 | S8 resubmissions and S9 malformed rows exist only in `data/claims.csv`, since the DB can't store them | Team, PR #6 |
| 2026-11-05 | Clean generated claims are reported within 21 days and use `COVERED_LOSS_TYPES`, so they stay clean if the window changes | Team, PR #6 |
| 2026-11-05 | `check_results.claim_id` and `flags.claim_id` have no foreign key to `claims`: intake records rows that never became claims (S8 resubmissions, S9 malformed rows) | Team, CIC-29 |
| 2026-11-05 | S8 and S9 rows are recorded as a `NEEDS_REVIEW` result plus an `INTAKE` flag with the reason, the line number and the raw row; S8 names the earlier run (or earlier line in the same file). No schema change | Team, CIC-29 |
| 2026-11-05 | Intake rows are keyed `ROW-<run_id>-<line_no>` when the claim_id is missing, is a resubmission, or is already used by a stored claim, so they never share a key with a real claim | Team, CIC-29 |
| 2026-11-05 | Intake trims and uppercases `claim_id` and `policy_number` before validating and before the S8 check; other free text is stored verbatim | Team, CIC-29 |
| 2026-11-05 | Intake validates formats only (required fields, strict `YYYY-MM-DD`, allowed values, plain decimals); date logic stays in the rules. CSV columns must match exactly, in any order | Team, CIC-29 |
| 2026-11-05 | Default `MAX_INTAKE_BYTES = 1 MiB`, for uploaded files and pasted text alike | Team default, CIC-29 |
| 2026-11-05 | Run contract: `create_run(conn, source, claim_count)` (intake) → `run_checks(conn, run_id, claim_ids)` (runner, CIC-28, no commit inside) → `record_intake_flags(conn, run_id, rows)` (intake), all in one transaction | Team, CIC-29 |

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
| S8 | Same claim ID submitted again | intake | Skipped, not re-checked; Needs review (`INTAKE` flag) |
| S9 | Required field missing or date unparseable | intake | Needs review (`INTAKE` flag) |
| S10 | Claimed amount above policy limit | R4 | Stretch — pending Karn |
| S11 | None of the above | — | Ready for adjuster |

## Open questions sent to Karn (4 Nov)

1. Is a loss on the expiry date itself covered?
2. Is 30 days the right reporting window?
3. Should amount above policy limit be flagged at intake?
