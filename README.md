# Still Water

*See clearly, then act.*

A local, read-only companion for an Interactive Brokers account that enforces **your own** trading rules.
Before a buy it shows red / yellow / green lights for every rule; after the close it audits every fill on the
data as it was at fill time; and a set of Streamlit pages — each with an impressionist-style background under liquid glass — shows
where you stand. The design is in [SPEC.md](SPEC.md).

**It never places orders.** There is no order placing, changing or cancelling code anywhere, and
`tests/test_read_only.py` fails if any appears. Data comes from IBKR's Flex Web Service, which can only
download statements.

> This is a personal discipline tool, not investment advice. The example rules and numbers are placeholders;
> set your own.

## What it does

- **Buy check** — enter a ticker, shares and price: every rule gets a light, plus the position weight and cash
  ratio after the buy and where the price sits on its valuation lines. A hard block can only be overridden
  with a one-line reason and a 24-hour cooldown. Every check is recorded, including the ones you drop.
- **Post-trade audit** — a daily job pulls your fills and re-runs the rules as of each fill. A buy with no
  matching check, a buy after a block without a reason, or a buy before the cooldown ended is recorded as a
  violation. Violations are append-only: database triggers stop even a bug from deleting them.
- **Watchlist** — register a catalyst and an invalidation while you are calm. Only entries older than 24 hours
  count as "planned". Your IBKR watchlists can be imported as a candidate pool: pick a stock and its ticker is
  filled in, but the catalyst and the invalidation are still yours to write.
- **Hunch journal** — write one sentence when you feel like taking profit or waiting; the price is recorded and
  compared 30 days later, so you learn how good your instincts are.
- **Valuation lines** — pressure / cheap / normal / rich lines from your own multiples and forward EPS, updated
  weekly by yfinance's revision ratio while keeping your EPS basis.
- **Sell reminders, review and weekly report** — staged take-profits, triggered invalidations, compliance rate,
  compliant vs. violating returns, how blocked trades did afterwards, and next week's earnings.

## Install

Python 3.11+ on macOS (the scheduled jobs use launchd; everything else runs anywhere).

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env              # then fill in your Flex token and query ids
.venv/bin/python -m db init       # create data/trading.db
```

`.env`, `data/` and `valuation_seed.csv` are in `.gitignore`: your account data stays on your machine.

### The Activity Flex Query

In IBKR Account Management → Reports → Flex Queries, create an Activity Flex Query with format **XML** and
period **Last 365 Calendar Days**, including:

- **Open Positions** — Summary level
- **Trades** — Execution level
- **Cash Report**
- **Net Asset Value (NAV) in Base** — daily cash and NAV, needed by the audit

Then enable the Flex Web Service to get a token. Raw statements are saved in `data/flex/` and can be re-parsed
with `--file`.

### Your rules

- `rules.yaml` — every rule and threshold. It is validated strictly: an unknown key, a misspelled rule or an
  invalid level is an error rather than a silently disabled rule.
- `valuation_seed.csv` — your multiples and forward EPS per stock; start from
  [valuation_seed.example.csv](valuation_seed.example.csv).

## Use it

```bash
.venv/bin/streamlit run app/main.py      # the pages at http://localhost:8501 (or double-click start.command)

.venv/bin/python -m ingest flex                # fetch the Activity Flex Query into the database
.venv/bin/python -m ingest flex --file x.xml   # re-parse a downloaded statement (no network)
.venv/bin/python -m ingest market              # refresh prices for positions + watchlist + QQQ
.venv/bin/python -m ingest valuation import    # import valuation_seed.csv
.venv/bin/python -m ingest valuation show      # latest valuation lines and where prices sit
.venv/bin/python -m ingest watchlists          # import IBKR watchlists (data/ibkr_watchlists.json) as the candidate pool

.venv/bin/python -m rules check NVDA 1 180.50           # check a proposed buy on the current state
.venv/bin/python -m rules recheck --since 2026-10-01    # re-run past buys as of their fill times

.venv/bin/python -m audit daily                # fetch Flex → refresh prices → audit new fills
.venv/bin/python -m audit weekly               # build this week's report into data/reports/
.venv/bin/python -m audit schedule install     # install the launchd jobs
```

Scheduled jobs (US Eastern): the daily job at 18:30 on weekdays with a catch-up run at 07:00 the next morning,
and the weekly report on Sunday at 18:00. Times are converted to your Mac's local time at install. If the
project lives in a protected folder such as Desktop and the log shows `Operation not permitted`, give Python
access in System Settings → Privacy & Security, or move the project.

## Pages

| Page | Contents |
| --- | --- |
| Overview | cash, this week's buying budget, positions vs. caps, current violations, cooldowns, the audit log (one row per buy) |
| Buy check | lights for every rule; override with a reason and a 24-hour cooldown |
| Valuation | price vs. pressure / cheap / normal / rich lines; holdings and watchlist apart |
| Watchlist | register catalysts and invalidations; your IBKR watchlists as a candidate pool |
| Hunches | one sentence, reviewed automatically after 30 days |
| Sell reminders | staged take-profits, above the rich line, triggered invalidations |
| Review | compliance rate, compliant vs. violating returns, blocked trades afterwards, violations by rule, monthly report |
| Weekly reports | generated every Sunday |

## How the rules are judged

- Rules come from the current `rules.yaml`; data comes from the moment of the trade. Positions and cost are
  replayed FIFO from the fills; cash is the end-of-day snapshot before the trading day plus earlier fills
  that day.
- Missing data is treated at the rule's own level: if the program cannot prove a buy complies, a block rule
  blocks, and the reason says what is missing.
- Only buys are checked. Selling has reminders, never hard blocks.
- `audit.start_date` starts the test period. Earlier fills are audited as a baseline for comparison only.

## Append-only records

Enforced by database triggers:

- `violations`: no updates, no deletes
- `checks`: no deletes; a blocked check can get an override reason once
- `watchlist`: no deletes, the registration time cannot change; an entry can be removed once
- `hunches`: no deletes; the review result can be added once
- `trades`, `valuation`: no deletes

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests run on a fictional account history, `tests/fixtures/sample_history.json`, generated by
`tests/fixtures/make_sample_history.py`. Every ticker (ORCA, PINE, KITE, MAPL…), price and quantity in it is made
up; QQQ only plays the index. The pages are tested headless with Streamlit's AppTest, with no network access.

## Project layout

| Path | Contents |
| --- | --- |
| `rules.yaml` | rules and thresholds (an example: edit it) |
| `settings.py` | reads `.env` |
| `rules/` | rule config, trading calendar, FIFO replay, state at a moment, the rule engine |
| `ingest/` | Flex Query, yfinance cache, valuation lines |
| `db/` | SQLite schema and migrations, watchlist and hunch records |
| `audit/` | post-trade audit, daily and weekly jobs, statistics, launchd schedule |
| `app/` | Streamlit pages, components and styles |
| `tests/` | tests and the fictional sample history |

## Style

The UI uses an impressionist-inspired light theme with a liquid-glass treatment: each page has a full-page
background painting, and every section is frosted glass floating on it. Colours and fonts are in
`.streamlit/config.toml`, the glass in `app/style.css`, and the components in `app/ui.py`. All artwork is
public domain (CC0).

## License

[MIT](LICENSE)
