# College Lacrosse ATS Model — Claude Instructions

## Project Purpose
ATS (against the spread) betting model for NCAA Division I men's lacrosse.
Ridge regression on rolling-window team stat differentials → predicted home margin.

## Tech Stack
- Python 3.11+, SQLite (via built-in `sqlite3`), no ORM
- requests + BeautifulSoup for scraping
- numpy for feature engineering and model math (no sklearn)
- python-dotenv for secrets
- Shared utilities in `../shared/`: `implied_probs.py`, `team_mapper.py`
  - Use `resolve_team_name()` from `team_mapper` — NOT a `TeamMapper` class

## File Structure
```
config.py           — constants, paths, API keys, season params, rolling window
main.py             — CLI entry point
db/
  models.py         — CREATE TABLE statements (ALL_TABLES list)
  db.py             — get_db(), init_db(), upsert_* helpers, build_lr_numeric_id_map()
scrapers/
  espn_scraper.py   — game schedule + results (free ESPN API; NO box scores for lacrosse)
  lacrosse_ref.py   — per-game box scores from pro.lacrossereference.com
  odds_api.py       — moneyline + spread lines (The Odds API)
processors/
  features.py       — load_rolling_stats(), compute_game_features(), build_training_dataset()
  model.py          — RidgeSpreadModel, train(), predict_game(), evaluate_ats_performance()
jobs/
  historical_backfill.py — one-time load: ESPN games + LR box scores for ALL_SEASONS
  daily_job.py      — morning: refresh current-season box scores + generate predictions
  results_job.py    — morning: score yesterday's ATS predictions
```

## Common Commands
```
python main.py init            # initialize DB (create tables)
python main.py backfill        # load historical data (ESPN + LR box scores, ~20-30 min)
python main.py backfill --games-only   # ESPN games/results only
python main.py backfill --box-only     # box scores only (skip ESPN)
python main.py train           # fit/retrain Ridge model
python main.py both            # morning run: results + predictions
python main.py evaluate        # ATS performance summary
python main.py check-sports    # verify lacrosse_ncaa Odds API key
python main.py probe           # dump pro.lacrossereference.com page structure
```

## Spread Edge Formula
`spread_edge = predicted_spread + market_spread`
- `market_spread` sign convention: negative = home favored (e.g. -4.5), positive = home underdog (+10.5)
- Market's implied home margin = `-market_spread`; edge compares model to market's implied margin
- edge > 0 → BET HOME; edge < 0 → BET AWAY
- **Do NOT use `predicted - market`** — that has the wrong sign when home is an underdog

## Key Design Decisions

### Data source for features
- **pro.lacrossereference.com** is the ONLY source of per-game box scores
- ESPN `summary` endpoint returns empty boxscore for college lacrosse (confirmed)
- No Playwright needed — LR data is server-rendered in initial HTML response
- Data available ~2018+ for most programs; confirmed for 2024–2026

### game_stats schema (one row per game)
- Stores home_* and away_* columns for all raw counts + derived rates
- `UNIQUE(game_id)` — one row per game, not per team
- `game_slug` stores the lacrosse-ref URL slug for deduplication
- Teams linked via `lr_pro_slug` (e.g. `dukem-5269`) stored in `teams` table

### Rolling window features
- `ROLLING_WINDOW = 5` (configurable in config.py)
- `MIN_GAMES_FOR_PREDICTION = 3`
- `load_rolling_stats()` queries `game_stats` + `games` for team's last N games
- Team perspective: correctly uses home_* or away_* depending on team's role
- No data leakage: only games where `game_date < prediction_date`

### Feature names (FEATURE_NAMES in features.py)
All 10 features including `home_field` are in `FEATURE_NAMES` — do NOT append
home_field separately when building feature vectors.

### Turnover feature
`to_diff = away_TO/G − home_TO/G` (positive = home commits fewer TOs = good).
Caused turnovers are NOT in per-game box scores; this is a partial proxy.

### Season config
- `SEASON_YEAR = 2026` (current live season)
- `ALL_SEASONS`: 2016–2019 + 2021–2026 (2020 excluded — COVID cancellation)
- `TRAIN_SEASONS`: 2016–2023; `VAL_SEASONS`: [2024, 2025]; `TEST_SEASONS`: [2026]

## Scraper Key Facts
- Game slug regex: `game-[a-z-]+-vs-[a-z-]+-mlax-\d{4}-[a-z0-9]+`
  — IDs are ALPHANUMERIC (e.g. `6h86`), not purely numeric
- D1 team pro slugs: `{teamname}m-{4digits}` (men's suffix convention)
  — Some teams have HYPHENATED pro slugs (e.g. `notre-damem-1853`, `penn-statem-1168`)
  — Game slugs strip hyphens: `notre-damem-1853` → games use `notredame`
  — `build_slug_name_map()` strips hyphens before indexing to match game slug format
- LR name matching uses `_LR_ESPN_OVERRIDES` dict in `historical_backfill.py` for ambiguous cases
  (e.g. LR "Penn" = Pennsylvania Quakers NOT Penn State, LR "UMass" = Massachusetts Minutemen NOT Lowell)
- LR numeric ID (e.g. `5269`) extracted from pro slug tail; used in `build_lr_numeric_id_map()`
- 77 D1 men teams discoverable from `lacrossereference.com/stats/adj-efficiency-d1-men`
- 4 ESPN teams NOT in LR D1 database: Hartford, Furman, Lindenwood, Roberts Wesleyan
- HTTP headers must include Chrome-style User-Agent (default gets 403)
- pro.lacrossereference.com can return transient 500 errors; use connect+read timeout tuple `(5, 8)` not just integer to prevent TCP hangs
- `python main.py fix-slugs` — resets all LR slug mappings and re-runs; use after changing `_LR_ESPN_OVERRIDES`

## Environment Variables (.env)
```
ODDS_API_KEY=your_key_here
```
Shared with `baseball_betting` — same key works for both.

## LR-Only Game Backfill — COMPLETED (2026-03-06)

### The Problem
ESPN's scoreboard API only captures **5–7 games per team per season for 2016–2024**,
versus the 15–20 games teams actually play. For 2025–2026 the coverage improves to
~13/team (but hits the 500-game API cap per season). LacrosseReference has 5,659 unique
game slugs across all seasons — about 2× what ESPN returned — meaning roughly half of all
D1 games are missing from the `games` table for training seasons 2016–2024.

This matters because rolling-window features (`load_rolling_stats()`, ROLLING_WINDOW=5)
computed on 7 of a team's 18 real games are noisy: the "last 5" can span almost the
whole season with random gaps rather than reflecting true recent form.

### Completed
Schema added, second-pass logic implemented, full backfill run, model retrained.

### What's Needed to Fix It
1. **Schema change** (`db/models.py`): make `espn_game_id` nullable in `games` table;
   add `lr_game_slug TEXT UNIQUE` as an alternate dedup key. Change the UNIQUE constraint
   from `UNIQUE(espn_game_id)` to a partial unique index or use `lr_game_slug` as the
   primary dedup for LR-sourced rows.

2. **New backfill logic** (`jobs/historical_backfill.py`): after `backfill_game_box_scores()`
   runs, add a second pass that:
   - Iterates over all collected game slugs that had `skipped_no_game` (i.e. no ESPN match)
   - For each: parse home/away from slug name map, date from box score title
   - Insert a synthetic `games` row (no `espn_game_id`, `lr_game_slug` = slug)
   - Re-upsert the `game_stats` row linking to the new game_id
   - Mark the game `source = 'lr'` (add a `source` column to `games`)

3. **Re-run**: `python main.py fix-slugs` will re-collect all 5,659 slugs and populate
   the expanded games table. Then retrain the model.

### Achieved Impact
- Training games: 827 → 2,672 (3.2×); val games: 471 → 890 (1.9×)
- ATS high-edge: 60.4% → 61.3% win rate; 212 → 243 bets (more opportunities)
- `game_stats` rows: 2,489 → 4,841

### Current State (v2 — post LR backfill)
- `games` table: 2,974 ESPN rows + 2,346 LR-only rows = ~5,320 total
- `game_stats` table: 4,841 rows (2,495 ESPN-matched + 2,346 LR-only)
- `lr_game_slug` column + partial unique index on games table (migration applied)
- **v1 model trained**: 827 train games, 471 val games; Val MAE=4.52 goals, RMSE=5.77
  Top features: shot_pct_diff (+1.38), goals_allowed_diff (-0.95), pace_diff (+0.75), fo_pct_diff (+0.58)
  Notable: home_field weight=0.00 (no detected HFA — investigate); save_pct collinear with shot_pct

### ATS Backtest Results (2025 season only — 2024 outside Odds API history window)
Run `python main.py backfill-odds` to fetch historical lines + populate daily_snapshots.

**v2 model (full LR backfill, 2,672 train games)** — current model in DB:
- **All bets**: 215-150 (58.9%), 365 games, avg_edge=3.81
- **High-edge (|edge| ≥ 2.0)**: 149-94 (61.3%), 243 bets
- Training: 2,672 games (up from 827); val: 890 games (up from 471); MAE=4.98, RMSE=6.46

**v1 model (ESPN-only, sparse rolling windows)** — archived reference:
- All bets: 181-130 (58.2%); High-edge: 128-84 (60.4%), 212 bets
- Training: 827 games; val: 471 games; MAE=4.52, RMSE=5.77

Breakeven for -110 juice is 52.4% — model shows meaningful positive edge on 2025 holdout.
home_field weight = 0.00 in both models (no detected HFA in college lacrosse — investigate)

---

## Next Investigations (prioritized)

### 1. Home Field Advantage = 0.00 (data quality)
Both v1 and v2 models assign zero weight to `home_field`. This is suspicious — HFA is real
in college lacrosse (~1–2 goals on average). Likely causes:
- LR-only games: `neutral_site = 0` by default (we don't know true neutral site status)
- LR home/away assignment may not match ESPN convention for some games
- Result: noise on `home_field` column drowns out any real signal
**Investigation**: cross-check a sample of LR-only games against ESPN to verify home/away
assignment. Consider dropping LR-only games with uncertain site status from training, or
adding `source = 'lr'` as a feature flag.

### 2. `pace_diff` Sign Flip (data quality + model stability)
Went from +0.75 (v1) to -0.53 (v2) — a full sign reversal with 3x more data. This means
pace_diff had no stable prior direction; the v1 weight may have been a spurious fit on
sparse data. With full data it now says slower-paced teams outperform vs. the spread,
which could be real (lower variance games → sharper model predictions) or still noise.
**Investigation**: LOO by season — does pace_diff show consistent sign across seasons?
If direction varies year-to-year, drop it. It's also the most model-volatile feature.

### 3. Daily Output — Accessibility
The daily job prints predictions to stdout but doesn't persist them anywhere reviewable.
**To do**: add a `--dry-run` flag or a `python main.py show-picks` command that reads
today's predictions from `daily_snapshots` (already stored) and pretty-prints them.
Alternatively, write a simple text/CSV file to `logs/picks_{date}.txt` each morning.

### 4. Feature Engineering Candidates
Current 9 active features are all rate-based box score stats. Potential additions:
- **SOS (strength of schedule)**: rolling avg of opponent win% or adj. efficiency — biggest
  gap vs. power ratings. Teams with easy schedules inflate all stats.
- **Recent form trend**: weighted rolling avg (last 3 games weighted 2x vs. games 4–5) —
  captures hot/cold streaks better than simple window average
- **Turnover margin** (caused TOs − committed TOs): per-game box scores only have committed
  TOs; caused TOs require a different LR endpoint. Worth checking if available.
- **Man-up conversion %**: power play efficiency — not in current box score scrape
- **Save% vs. shot quality**: current save_pct_diff is noisy; weighting by shot type (if available)
  would sharpen it
Before adding any feature: run LOO to verify it adds ATS value, not just MAE value.

### 5. Correlation Analysis
Before feature additions, audit existing features for collinearity:
- `save_pct_diff` and `shot_pct_diff` are known to be correlated (fewer goals → higher saves)
- `goals_per_game_diff` and `goals_allowed_diff` may be partially redundant
- `pace_diff` and `sog_pct_diff` potentially correlated through possession count
Run pairwise Pearson correlations on normalized feature matrix and drop features with |r| > 0.7.

### 6. Edge Threshold & Bet Sizing Optimization
Current `SPREAD_THRESHOLD = 2.0` is arbitrary. To optimize:
- Sweep threshold from 0.5 to 8.0 in 0.5 increments; compute win%, ROI at each level
- Find the knee: point where tighter threshold stops improving win% meaningfully
- For bet sizing: Kelly criterion with fractional Kelly (0.25–0.5×) based on edge magnitude
  rather than flat betting — larger edges justify larger bets
- Year-over-year returns table (unit P&L per season, ROI%) to assess variance and
  identify if 2025 is an outlier or representative
Goal: generate a final "bet sizing card" — edge range → recommended unit size.

### 7. Vig-Adjusted Edge & Bet Sizing
The current edge calculation (`predicted + market_spread`) tells us *which side* to bet but
ignores the *cost* of betting. College lacrosse lines are often not standard -110 — heavier
juice appears when books expect lopsided action (e.g. a big favorite getting even more action).

**Breakeven win rates by juice:**
- -110 → 52.4%  |  -120 → 54.5%  |  -130 → 56.5%  |  -140 → 58.3%  |  -150 → 60.0%

**Cover probability** (what we actually need, not outright win prob):
  `cover_prob ≈ Φ((predicted_margin - (-market_spread)) / RMSE)`
  where RMSE ≈ 6.46 (model prediction std dev), Φ = normal CDF
  Example: Delaware +10.5, predicted_margin=-3.2:
    cover_prob = Φ((-3.2 + 10.5) / 6.46) = Φ(1.13) ≈ 87%
  At -140 juice, breakeven = 58.3% → EV positive

**Juice-adjusted ROI**: `EV = cover_prob * (100/|juice|) - (1-cover_prob) * 1 unit`
Log actual juice on every bet. The tracker computes real ROI, not assumed-110 ROI.

**Recommended approach for edge threshold**: instead of a fixed spread_edge ≥ 2.0,
compute `cover_prob - breakeven_prob` where breakeven comes from actual juice.
Flag bets where `(cover_prob - breakeven_prob) ≥ 0.05` (5pp of edge minimum).
This replaces the fixed threshold with a juice-aware one.

### 8. Bet Tracker
A `bets` table in the DB + CLI commands to log bets and report P&L.
All actual bets placed should be logged here (separate from model predictions).

**Table**: `bets` — game_id, bet_date, model_side, market_spread, juice, units, result, pnl
**Commands**:
- `python main.py log-bet` — log a bet you're placing (records juice, units)
- `python main.py tracker` — P&L report: per-bet log + running totals + ROI by season

**P&L formula (American odds)**:
- Win: `pnl = units * (100.0 / abs(juice))`  — e.g. 1u at -140 wins 0.714u
- Loss: `pnl = -units`
- Push: `pnl = 0`

### 9. Year-Over-Year Performance Table
Once 2026 games accumulate (May), report format:
```
Season   Bets  W-L      Win%    P&L (units)   ROI%
2025      243  149-94   61.3%   +18.5u         +7.6%  (assumes -110)
2026      ???  ???      ???%    ???            ???     (actual juice tracked)
```
Tracker logs actual juice so 2026 ROI is real, not assumed.

---

## Notes
- Season: February 1 – May 31
- 2020 season cancelled (COVID) — excluded from ALL_SEASONS
- NCAA tournament: 48-team bracket (since 2022), campus sites through semis
- Faceoff win% is the most lacrosse-specific feature — likely strongest predictor
- Goalie save% is volatile; rolling average is more stable than single-game values
- `lacrosse_ncaa` is the confirmed Odds API sport key for men's college lacrosse
