-- 002 market data and fundamentals cache (yfinance)
--
-- History is kept, not just the latest values: the audit needs the 52-week low, recent high and
-- forward EPS as they were "at fill time".

-- daily bars: exchange-local dates. The last few days are overwritten on refresh (an intraday bar is final only after the close)
CREATE TABLE price_history (
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL NOT NULL,
    volume     INTEGER,
    fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (symbol, date)
);

-- quotes and fundamentals: one row appended per fetch
CREATE TABLE market_snapshots (
    symbol     TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('quote', 'fundamentals')),
    fetched_at TEXT NOT NULL,
    data       TEXT NOT NULL,           -- JSON
    PRIMARY KEY (symbol, kind, fetched_at)
);

-- earnings dates: past ones are kept; estimated future dates are replaced on each refresh (estimates move)
CREATE TABLE earnings_dates (
    symbol        TEXT NOT NULL,
    earnings_date TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (symbol, earnings_date)
);

-- last successful fetch per kind of data, to decide whether the cache is stale
CREATE TABLE market_fetch_log (
    symbol     TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('history', 'quote', 'fundamentals', 'earnings')),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, kind)
);
