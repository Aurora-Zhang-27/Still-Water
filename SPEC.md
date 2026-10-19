# Trading discipline system: design

A local program that reads IBKR data and enforces your own trading rules: red / yellow / green lights before
a buy, an automatic audit of every fill afterwards, and Streamlit pages to see it all. It constrains your
behaviour; it does not pick stocks for you and it never places orders.

## Goals and principles

Core principle: the system trusts nothing you type in. It looks at the data itself.

1. **As little manual input as possible.** People make mistakes, forget, and lie to themselves. Anything that
   can be fetched or computed is never typed by hand.
2. **Exceptions are allowed, but they cost something.** Design for the moment you *will* want to break a
   rule. When a hard rule fires, you must write a one-line reason and wait 24 hours, and the exception is
   recorded forever.
3. **Few rules; spend the complexity on data and analysis.** Keep the hard rules to a handful, so the alerts
   are never so frequent that you learn to ignore them.
4. **Read-only, never trades.** The program has no technical ability to place orders, so a bug can never
   trade for you.
5. **Protect your strengths.** Good instincts at the top, willingness to research, general restraint. The
   system should test your instincts, not smother them.

Typical failure patterns it targets:

- adding to a losing position again and again, anchored on the cost price
- wanting to catch the knife on a day of heavy selling
- trading too often in a volatile week
- breaking the cash floor one small buy at a time
- forgotten GTC orders filling weeks later
- regret after missing a move, then chasing it

## Buying: the three selection rules

All of them are judged automatically from data; you never tick a box. Missing any one of them sends the buy
into the rule-breaking flow.

| Rule | How the program decides | Result |
| --- | --- | --- |
| Near a low | Any one of: ≤ 15% above the 52-week low; ≥ 20% below the 60-day high; price in the "cheap–normal" valuation band | none hold → fail |
| PE not high | Price ≤ normal line (forward EPS × normal multiple) | between normal and rich → warning; above rich → fail |
| Has catalyst | A catalyst and an invalidation registered on the watchlist beforehand | not registered → fail |
| Planned | The stock has been on the watchlist for more than 24 hours (by timestamp, not by asking you) | otherwise → fail |

PE is misleading for cyclical stocks; they are flagged and use price-to-book, or are confirmed by hand.

The buy check page also shows the weight after the buy, the cash ratio after the buy, and where the stock
sits on its valuation lines.

## Rules and thresholds

Every threshold lives in `rules.yaml`, so changing a rule never needs code. There are three levels: hard
block (write a reason and wait 24 hours), warning (shown only) and reminder (sent periodically).

| Category | Rule | Threshold (example) | Level |
| --- | --- | --- | --- |
| Position | Single-stock cap | 13% by default; per-ticker overrides (e.g. an index ETF at 20%) | block |
| Position | No buying a stock already over its cap | current weight > cap | block |
| Cash | Cash floor | after the buy < 5% blocks; < 10% warns | block / warn |
| Buying | No averaging down | unrealized loss > 10% | block |
| Buying | Earnings blackout | the 5 trading days before earnings | block |
| Buying | Reduce-only list | exit when weight ≤ 8%, 30 days on the list and a new catalyst registered, all three | block |
| Buying | The three selection rules (above) | any one fails | block |
| Pace | Drop-day limit | when QQQ falls more than 2% in a day, at most 1 buy that day | block |
| Pace | Weekly budget | buys no more than 1/3 of week-start cash | warn |
| Pace | GTC review | every Sunday list open orders; flag ones open over 14 days, and ones whose fill would break a rule | remind |
| Selling | Take profits in stages | unrealized +50%, +100% | remind |
| Selling | Invalidation triggered | the price or event registered on the watchlist happens | remind, and asks for a review |
| Selling | Above the rich line | price > rich line | warn |

One special module: the **hunch journal**. When you feel like taking profit or waiting, write one sentence;
the program records the price, compares it 30 days later, and keeps score of your instincts.

Valuation lines update weekly. There are four: pressure (normal multiple × 0.75), cheap, normal and rich.

## Manual input and the post-trade audit

You only do two things by hand; everything else is automatic.

**By hand**

1. **When adding to the watchlist**: one sentence for the catalyst (event + time window) and one invalidation
   (a price or an event). Do this while calm, not at order time. The program records the timestamp.
2. **When a hard block fires**: one sentence of reason.
3. **(Optional) the hunch journal**: one sentence; the price is recorded for you.

**Automatic**: positions, fills, cash, open orders, PE, 52-week highs and lows, earnings dates, valuation lines,
the selection rules, P&L and violation statistics.

**The post-trade audit** (so a skipped or forgotten check is still caught)

- Every day, fetch the fills and re-run every rule on the data **as it was at fill time**.
- Violating trades are flagged.
- A fill with no matching buy check gets an extra "unchecked trade" violation.
- GTC fills are audited the same way.
- IBKR is the source of truth for fills; audit results cannot be deleted.

## IBKR data

In two phases: first the Flex Query (simple and safe), later a read-only IB Gateway for open orders and live
positions.

| Option | What you get | Pros | Cons |
| --- | --- | --- | --- |
| Flex Query (Flex Web Service) | positions, fills, cash, dividends | read-only; pulled with a token on a schedule; no machine left running; stable | no open orders; mostly end-of-day data |
| TWS API / IB Gateway (Python `ib_async`) | live positions, fills, open orders, quotes | the most complete | Gateway must stay running; disconnects daily; weekly 2FA login; live data needs subscriptions |
| Client Portal Web API | like the TWS API, over REST | no TWS install | sessions expire often; the most hassle for personal use |

**Phase 1: Flex Query + yfinance**

- After every close, fetch positions and fills for the audit, the logs, the weekly report and position checks.
- PE, 52-week highs and lows and earnings dates come free from yfinance.
- The buy check estimates the current state from "last night's positions + today's fills".

**Phase 2: a read-only IB Gateway**

- Read open orders (needed for the GTC review) and live positions.
- Tick **Read-Only API** in the Gateway's API settings, so the program technically cannot place orders.

## Architecture and pages

Data flow: IBKR (Flex / read-only Gateway) + yfinance → local SQLite → rule engine (reads `rules.yaml`) →
Streamlit pages. Everything runs locally; account data never leaves your computer.

**Modules**

1. `ingest/`: Flex Query, Gateway, yfinance prices and valuation data
2. `db/`: SQLite with tables including positions, trades, orders, watchlist, checks, violations, hunches, valuation
3. `rules/`: the rule engine; a proposed or past trade in, red / yellow / green per rule out
4. `audit/`: the daily post-trade audit
5. `app/`: the Streamlit pages
6. Scheduled jobs: fetch + audit after every close; weekly report and GTC review every Sunday

**Pages**

| Page | Contents |
| --- | --- |
| Overview | cash-ratio gauge (5% / 10% lines); position bars with cap ticks; this week's buys and budget; current violations and reminders |
| Buy check | ticker and shares → lights for every rule; weight and cash after the buy; when blocked, a reason and a 24-hour countdown |
| Valuation | where each price sits between the cheap / normal / rich lines, coloured by band; forward PE history; holdings and watchlist apart |
| Orders and sell reminders | open GTC orders and their age; whether a fill would break a rule; +50% / +100% reminders; triggered invalidations |
| Review | returns of compliant vs. violating trades; how blocked trades did afterwards; hunch accuracy; monthly report |

## Test period and how to judge it

Run a test period of one to two months, and ignore win rate: with a dozen trades, P&L mostly follows the market.

Look at three measures instead:

- **Compliance rate**: when a hard block fires, how often you comply vs. break it.
- **Compliant vs. violating**: the average return of each group (relative to QQQ over the same period).
- **Blocked trades**: the ones you did not buy — did they rise or fall afterwards?

**Do not change the rules during the test period.** When a rule feels "in the way", write it down and adjust
everything together at the end.

## Decisions

| Question | Decision | Why |
| --- | --- | --- |
| Averaging-down limit | Down more than 10%: no adding. Between 0 and −10%: only if all three selection rules pass | a 15% line would miss adding at −13% |
| Leaving the reduce-only list | Weight ≤ 8%, 30 days on the list and a new catalyst registered, all three | "back to cost" is not a condition, to avoid anchoring on cost |
| Valuation update frequency | Weekly | — |
| Pressure line | Normal multiple × 0.75 | about 25% multiple compression in a rate-hike scenario |
| Hard blocks on selling | None | — |
| Weekly budget | By amount: at most 1/3 of week-start cash | — |
