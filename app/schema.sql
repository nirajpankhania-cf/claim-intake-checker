-- Claim Intake Check SQLite schema. Loaded by app.db.init_db().
-- check_results and flags are append-only: triggers reject UPDATE/DELETE.
-- claims.policy_number has no FK to policies: unknown policies are flagged by R0.
CREATE TABLE IF NOT EXISTS policies (
    policy_number     TEXT PRIMARY KEY,
    policyholder_name TEXT NOT NULL,
    vehicle_reg       TEXT NOT NULL,
    cover_type        TEXT NOT NULL
                      CHECK (cover_type IN ('comprehensive', 'tpft', 'tpo')),
    effective_date    TEXT NOT NULL,
    expiration_date   TEXT NOT NULL,
    cancellation_date TEXT,
    policy_limit      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id             TEXT PRIMARY KEY,
    policy_number        TEXT NOT NULL,
    policyholder_name    TEXT NOT NULL,
    claimant_name        TEXT,
    vehicle_reg          TEXT NOT NULL,
    loss_date            TEXT NOT NULL,
    report_date          TEXT NOT NULL,
    loss_type            TEXT NOT NULL
                         CHECK (loss_type IN ('collision', 'theft', 'glass',
                                'vandalism', 'weather', 'third_party')),
    loss_location        TEXT,
    description          TEXT,
    claimed_amount       TEXT NOT NULL,
    third_party_involved INTEGER NOT NULL CHECK (third_party_involved IN (0, 1)),
    police_reference     TEXT,
    attachments          TEXT NOT NULL DEFAULT '[]',
    submitted_via        TEXT NOT NULL CHECK (submitted_via IN ('email', 'form'))
);
CREATE INDEX IF NOT EXISTS idx_claims_policy_number ON claims (policy_number);

CREATE TABLE IF NOT EXISTS check_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('form', 'csv')),
    claim_count INTEGER NOT NULL CHECK (claim_count >= 0)
);

CREATE TABLE IF NOT EXISTS check_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES check_runs (run_id),
    claim_id   TEXT NOT NULL,
    outcome    TEXT NOT NULL
               CHECK (outcome IN ('READY_FOR_ADJUSTER', 'NEEDS_REVIEW')),
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_results_run ON check_results (run_id);
-- One result per claim per run, so a repeated or retried check can't add rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_check_results_run_claim ON check_results (run_id, claim_id);

CREATE TABLE IF NOT EXISTS flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES check_runs (run_id),
    claim_id    TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flags_run_claim ON flags (run_id, claim_id);
-- One flag per rule per claim per run.
CREATE UNIQUE INDEX IF NOT EXISTS uq_flags_run_claim_rule ON flags (run_id, claim_id, rule_id);

CREATE TRIGGER IF NOT EXISTS check_results_no_update
BEFORE UPDATE ON check_results
BEGIN SELECT RAISE(ABORT, 'check_results is append-only'); END;

CREATE TRIGGER IF NOT EXISTS check_results_no_delete
BEFORE DELETE ON check_results
BEGIN SELECT RAISE(ABORT, 'check_results is append-only'); END;

CREATE TRIGGER IF NOT EXISTS flags_no_update
BEFORE UPDATE ON flags
BEGIN SELECT RAISE(ABORT, 'flags is append-only'); END;

CREATE TRIGGER IF NOT EXISTS flags_no_delete
BEFORE DELETE ON flags
BEGIN SELECT RAISE(ABORT, 'flags is append-only'); END;
