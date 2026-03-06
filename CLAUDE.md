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
- Game slug regex: `game-[a-z]+-vs-[a-z]+-mlax-\d{4}-[a-z0-9]+`
  — IDs are ALPHANUMERIC (e.g. `6h86`), not purely numeric
- D1 team pro slugs: `{teamname}m-{4digits}` (men's suffix convention)
- LR numeric ID (e.g. `5269`) extracted from pro slug tail; used in `build_lr_numeric_id_map()`
- 77 D1 men teams discoverable from `lacrossereference.com/stats/adj-efficiency-d1-men`
- HTTP headers must include Chrome-style User-Agent (default gets 403)

## Environment Variables (.env)
```
ODDS_API_KEY=your_key_here
```
Shared with `baseball_betting` — same key works for both.

## Notes
- Season: February 1 – May 31
- 2020 season cancelled (COVID) — excluded from ALL_SEASONS
- NCAA tournament: 48-team bracket (since 2022), campus sites through semis
- Faceoff win% is the most lacrosse-specific feature — likely strongest predictor
- Goalie save% is volatile; rolling average is more stable than single-game values
- `lacrosse_ncaa` is the confirmed Odds API sport key for men's college lacrosse
