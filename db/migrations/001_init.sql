-- 001 initial schema
--
-- Conventions:
--   times are UTC ISO 8601 text (2026-10-18T21:30:00Z); dates are YYYY-MM-DD
--   ratios are decimals (0.13 = 13%)
--   re-importing IBKR data uses INSERT ... ON CONFLICT DO UPDATE, never REPLACE
--
-- Append-only tables (enforced by triggers):
--   violations  no updates, no deletes
--   checks      no deletes; a blocked check can get an override reason added once
--   watchlist   no deletes; timestamps cannot change; can be marked removed once
--   hunches     no deletes; the 30-day review result can be added once
--   trades      no deletes (IBKR is the source of truth for fills)


-- ── mirrored IBKR data ─────────────────────────────────────

-- daily account snapshots: cash and NAV, for the cash ratio and position weights
CREATE TABLE account_snapshots (
    as_of           TEXT NOT NULL,      -- Flex statement date, or Gateway read time
    account_id      TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    cash            REAL NOT NULL,
    net_liquidation REAL NOT NULL CHECK (net_liquidation > 0),
    source          TEXT NOT NULL CHECK (source IN ('flex', 'gateway')),
    imported_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, account_id, source)
);

-- position snapshots: history kept by date; the audit needs positions at fill time
CREATE TABLE positions (
    as_of          TEXT NOT NULL,
    account_id     TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    asset_class    TEXT NOT NULL DEFAULT 'STK',
    quantity       REAL NOT NULL,
    avg_cost       REAL,                -- cost per share
    mark_price     REAL,
    market_value   REAL,
    unrealized_pnl REAL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    source         TEXT NOT NULL CHECK (source IN ('flex', 'gateway')),
    imported_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, account_id, symbol, source)
);

CREATE TABLE trades (
    trade_id        TEXT PRIMARY KEY,   -- IBKR tradeID
    account_id      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    asset_class     TEXT NOT NULL DEFAULT 'STK',
    side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity        REAL NOT NULL CHECK (quantity > 0),
    price           REAL NOT NULL CHECK (price > 0),
    proceeds        REAL,
    commission      REAL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    executed_at     TEXT NOT NULL,
    order_id        TEXT,
    order_placed_at TEXT,               -- order time; earlier than the fill day = a resting order that filled
    order_type      TEXT,
    time_in_force   TEXT,               -- DAY / GTC (Gateway only)
    source          TEXT NOT NULL CHECK (source IN ('flex', 'gateway')),
    raw             TEXT,               -- raw fields as JSON, for checking later
    imported_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX trades_symbol_time ON trades (symbol, executed_at);

CREATE TRIGGER trades_no_delete BEFORE DELETE ON trades
BEGIN
    SELECT RAISE(ABORT, 'trades mirror IBKR and cannot be deleted');
END;

-- open-order mirror: records orders that already exist at IBKR. The program never creates, changes or cancels orders.
CREATE TABLE orders (
    perm_id       TEXT PRIMARY KEY,     -- IBKR permId
    account_id    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity      REAL NOT NULL CHECK (quantity > 0),
    order_type    TEXT,
    limit_price   REAL,
    stop_price    REAL,
    time_in_force TEXT,
    status        TEXT,
    placed_at     TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    source        TEXT NOT NULL DEFAULT 'gateway' CHECK (source IN ('flex', 'gateway'))
);


-- ── records you write ──────────────────────────────────────

-- watchlist: registered while calm. A stock can have several entries (re-registering a catalyst = a new entry)
CREATE TABLE watchlist (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),  -- used by the "planned" rule
    symbol             TEXT NOT NULL,
    catalyst           TEXT NOT NULL CHECK (length(trim(catalyst)) > 0),        -- event + time window
    catalyst_due_on    TEXT,                                                   -- end of the time window (optional)
    invalidation       TEXT NOT NULL CHECK (length(trim(invalidation)) > 0),    -- what would prove you wrong
    invalidation_price REAL CHECK (invalidation_price > 0),                    -- price-based invalidation, watched automatically
    invalidation_when  TEXT CHECK (invalidation_when IN ('below', 'above')),
    removed_at         TEXT,
    removed_reason     TEXT,
    CHECK ((invalidation_price IS NULL) = (invalidation_when IS NULL)),
    CHECK ((removed_at IS NULL AND removed_reason IS NULL)
        OR (removed_at IS NOT NULL AND length(trim(removed_reason)) > 0))
);
CREATE INDEX watchlist_symbol ON watchlist (symbol, created_at);

CREATE TRIGGER watchlist_no_delete BEFORE DELETE ON watchlist
BEGIN
    SELECT RAISE(ABORT, 'watchlist is append-only: no deletes; set removed_at to remove an entry');
END;

CREATE TRIGGER watchlist_remove_once BEFORE UPDATE ON watchlist
WHEN OLD.removed_at IS NOT NULL
  OR NEW.id                 IS NOT OLD.id
  OR NEW.created_at         IS NOT OLD.created_at
  OR NEW.symbol             IS NOT OLD.symbol
  OR NEW.catalyst           IS NOT OLD.catalyst
  OR NEW.catalyst_due_on    IS NOT OLD.catalyst_due_on
  OR NEW.invalidation       IS NOT OLD.invalidation
  OR NEW.invalidation_price IS NOT OLD.invalidation_price
  OR NEW.invalidation_when  IS NOT OLD.invalidation_when
BEGIN
    SELECT RAISE(ABORT, 'watchlist is append-only: entries and timestamps cannot change; an entry can be removed once');
END;

-- buy checks: every check is recorded, including blocked ones you gave up on (the review shows how they did afterwards)
CREATE TABLE checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity        REAL NOT NULL CHECK (quantity > 0),
    price           REAL NOT NULL CHECK (price > 0),
    verdict         TEXT NOT NULL CHECK (verdict IN ('pass', 'warn', 'block')),
    results         TEXT NOT NULL,      -- per-rule results as JSON
    state           TEXT NOT NULL,      -- account and market snapshot at check time, as JSON
    rules_version   INTEGER NOT NULL,
    override_reason TEXT,               -- the one-line reason written when overriding a block
    overridden_at   TEXT,
    cooldown_until  TEXT,               -- overridden_at + override.cooldown_hours
    CHECK ((override_reason IS NULL AND overridden_at IS NULL AND cooldown_until IS NULL)
        OR (verdict = 'block'
            AND length(trim(override_reason)) > 0
            AND overridden_at IS NOT NULL
            AND cooldown_until > overridden_at))
);
CREATE INDEX checks_symbol_time ON checks (symbol, created_at);

CREATE TRIGGER checks_no_delete BEFORE DELETE ON checks
BEGIN
    SELECT RAISE(ABORT, 'checks is append-only: no deletes');
END;

CREATE TRIGGER checks_override_once BEFORE UPDATE ON checks
WHEN OLD.override_reason IS NOT NULL
  OR NEW.id            IS NOT OLD.id
  OR NEW.created_at    IS NOT OLD.created_at
  OR NEW.symbol        IS NOT OLD.symbol
  OR NEW.side          IS NOT OLD.side
  OR NEW.quantity      IS NOT OLD.quantity
  OR NEW.price         IS NOT OLD.price
  OR NEW.verdict       IS NOT OLD.verdict
  OR NEW.results       IS NOT OLD.results
  OR NEW.state         IS NOT OLD.state
  OR NEW.rules_version IS NOT OLD.rules_version
BEGIN
    SELECT RAISE(ABORT, 'checks is append-only: results cannot change; a blocked check can get an override reason once');
END;

-- hunch journal: one sentence, price recorded automatically, compared automatically after 30 days
CREATE TABLE hunches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    symbol          TEXT NOT NULL,
    expected_move   TEXT NOT NULL CHECK (expected_move IN ('up', 'down')),  -- take profit = down
    note            TEXT NOT NULL CHECK (length(trim(note)) > 0),
    price_at_entry  REAL NOT NULL CHECK (price_at_entry > 0),
    review_due_on   TEXT NOT NULL,      -- entry day + hunches.review_after_days
    reviewed_at     TEXT,
    price_at_review REAL,
    was_right       INTEGER CHECK (was_right IN (0, 1)),
    CHECK ((reviewed_at IS NULL AND price_at_review IS NULL AND was_right IS NULL)
        OR (reviewed_at IS NOT NULL AND price_at_review > 0 AND was_right IS NOT NULL))
);

CREATE TRIGGER hunches_no_delete BEFORE DELETE ON hunches
BEGIN
    SELECT RAISE(ABORT, 'hunches is append-only: no deletes');
END;

CREATE TRIGGER hunches_review_once BEFORE UPDATE ON hunches
WHEN OLD.reviewed_at IS NOT NULL
  OR NEW.id             IS NOT OLD.id
  OR NEW.created_at     IS NOT OLD.created_at
  OR NEW.symbol         IS NOT OLD.symbol
  OR NEW.expected_move  IS NOT OLD.expected_move
  OR NEW.note           IS NOT OLD.note
  OR NEW.price_at_entry IS NOT OLD.price_at_entry
  OR NEW.review_due_on  IS NOT OLD.review_due_on
BEGIN
    SELECT RAISE(ABORT, 'hunches is append-only: entries cannot change; the review result can be added once');
END;


-- ── records the program computes ───────────────────────────

-- violations: written by buy checks and the post-trade audit; append-only
CREATE TABLE violations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    detected_by   TEXT NOT NULL CHECK (detected_by IN ('check', 'audit')),
    rule_id       TEXT NOT NULL,        -- rule name from rules.yaml, or unchecked_trade
    level         TEXT NOT NULL CHECK (level IN ('block', 'warn', 'remind')),
    symbol        TEXT,
    trade_id      TEXT REFERENCES trades (trade_id),
    check_id      INTEGER REFERENCES checks (id),
    message       TEXT NOT NULL,
    details       TEXT,                 -- the numbers at the time, as JSON
    rules_version INTEGER NOT NULL
);
-- the audit can re-run: one record per fill per rule (with INSERT OR IGNORE)
CREATE UNIQUE INDEX violations_trade_rule ON violations (trade_id, rule_id)
    WHERE trade_id IS NOT NULL;

CREATE TRIGGER violations_no_update BEFORE UPDATE ON violations
BEGIN
    SELECT RAISE(ABORT, 'violations is append-only: no updates');
END;

CREATE TRIGGER violations_no_delete BEFORE DELETE ON violations
BEGIN
    SELECT RAISE(ABORT, 'violations is append-only: no deletes');
END;

-- valuation lines: one row per week, history kept (the audit uses the lines of the fill's week)
CREATE TABLE valuation (
    symbol          TEXT NOT NULL,
    as_of           TEXT NOT NULL,      -- this week's update day
    metric          TEXT NOT NULL CHECK (metric IN ('pe', 'pb')),  -- cyclicals use pb
    base_value      REAL NOT NULL CHECK (base_value > 0),          -- forward EPS or book value per share
    cheap_multiple  REAL NOT NULL,
    normal_multiple REAL NOT NULL,
    rich_multiple   REAL NOT NULL,
    pressure_line   REAL NOT NULL,      -- normal multiple × valuation.pressure_multiple_of_normal × base_value
    cheap_line      REAL NOT NULL,
    normal_line     REAL NOT NULL,
    rich_line       REAL NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('seed', 'yfinance', 'manual')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (symbol, as_of),
    CHECK (0 < cheap_multiple AND cheap_multiple < normal_multiple AND normal_multiple < rich_multiple)
);

CREATE TRIGGER valuation_no_delete BEFORE DELETE ON valuation
BEGIN
    SELECT RAISE(ABORT, 'valuation keeps its history and cannot be deleted');
END;
