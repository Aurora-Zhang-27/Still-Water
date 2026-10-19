-- 003 post-trade audit results: one row per fill, append-only
--
-- period:
--   live      fills in the test period (audit.start_date onwards); violations also go to violations
--   baseline  fills before go-live; audited only as a baseline, no violations written
--
-- check_status (how a buy fill matches a buy check):
--   not_needed            a sell; no buy check needed
--   passed                checked before the order; passed or warned
--   overridden            blocked, reason written, ordered after the cooldown
--   missing               no matching buy check                   → violation unchecked_trade
--   block_ignored         blocked, ordered without a reason       → violation block_ignored
--   cooldown_not_elapsed  reason written, ordered during cooldown → violation cooldown_not_elapsed

CREATE TABLE trade_audits (
    trade_id      TEXT PRIMARY KEY REFERENCES trades (trade_id),
    audited_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    period        TEXT NOT NULL CHECK (period IN ('live', 'baseline')),
    verdict       TEXT NOT NULL CHECK (verdict IN ('pass', 'warn', 'block')),
    results       TEXT NOT NULL,        -- per-rule results as JSON (re-run on the data at fill time)
    state         TEXT NOT NULL,        -- account and market state at fill time, as JSON
    decision_at   TEXT NOT NULL,        -- when the decision was made: order time, else fill time
    check_id      INTEGER REFERENCES checks (id),
    check_status  TEXT NOT NULL CHECK (check_status IN (
                      'not_needed', 'passed', 'overridden', 'missing', 'block_ignored', 'cooldown_not_elapsed')),
    rules_version INTEGER NOT NULL
);

CREATE TRIGGER trade_audits_no_update BEFORE UPDATE ON trade_audits
BEGIN
    SELECT RAISE(ABORT, 'trade_audits is append-only: no updates');
END;

CREATE TRIGGER trade_audits_no_delete BEFORE DELETE ON trade_audits
BEGIN
    SELECT RAISE(ABORT, 'trade_audits is append-only: no deletes');
END;
