-- 005 a mirror of your IBKR watchlists: the candidate pool on the Watchlist page
--
-- This is IBKR's data as-is (IBKR is the source of truth, like positions); every sync replaces it whole.
-- These are not records you write, so the table is not append-only.
-- A stock in the pool is not "registered": the buy check's "Has catalyst" and "Planned" only read the watchlist table.

CREATE TABLE ibkr_watchlist (
    list_name   TEXT NOT NULL,      -- the list's name in IBKR (Chips, Software…)
    symbol      TEXT NOT NULL,      -- the ticker the program uses: BRK B → BRK-B (same as yfinance)
    ibkr_symbol TEXT NOT NULL,      -- the ticker as IBKR shows it
    conid       TEXT,               -- IBKR contract id (optional)
    position    INTEGER NOT NULL,   -- order within the IBKR list
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (list_name, symbol)
);
