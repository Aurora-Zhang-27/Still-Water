-- 004 valuation lines: store yfinance's value at the same time, to measure weekly EPS revisions
--
-- Your EPS and yfinance's forward EPS may use different bases (e.g. 8.40 vs 9.10 for the same stock).
-- Weekly updates keep your basis and only apply yfinance's change: new EPS = old EPS × (yfinance now / reference_value)

ALTER TABLE valuation ADD COLUMN reference_value REAL;  -- yfinance forward EPS (or book value per share) when this row was written
ALTER TABLE valuation ADD COLUMN reference_at TEXT;     -- when that reference value was taken
