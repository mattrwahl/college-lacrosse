# College Lacrosse ATS Betting Model — Methodology

## Overview

A head-to-head spread betting model for NCAA Division I men's lacrosse.
Primary betting application: against the spread (ATS). Moneylines also tracked.

Season runs approximately February 1 – May 31 (regular season + NCAA tournament).

---

## Data Sources

### 1. pro.lacrossereference.com (primary — per-game box scores)

The backbone of the feature pipeline. Every completed game has a dedicated page
at `https://pro.lacrossereference.com/game-{team1}-vs-{team2}-mlax-{year}-{id}`
with box score data embedded as JavaScript in the initial HTML response
(no Playwright / headless browser required).

**Data extracted** (`BasicSummaryCounting` JSON array):

| Field | Description |
|-------|-------------|
| `goals` | Goals scored |
| `shots` | Total shots attempted |
| `sog` | Shots on goal |
| `assists` | Assists |
| `possessions` | True possession count |
| `turnovers` | Turnovers committed |
| `gbs` | Ground balls won |
| `faceoffs` | Faceoff wins (home + away sum = total) |
| `saves` | Goalie saves |
| `top` | Time of possession (0.0–1.0 fraction) |

**Team/game discovery:**
- 77 D1 men teams enumerable from `lacrossereference.com/stats/adj-efficiency-d1-men`
  via embedded `var td` JavaScript variable
- Each team's page links to their pro site slug (e.g. `dukem-5269`)
- Game slugs fetched from `pro.lacrossereference.com/{pro_slug}?view=games&year={season}`
- Game slug format: `game-{home_slug}-vs-{away_slug}-mlax-{year}-{alphanum_id}`
  — **IDs are alphanumeric** (e.g. `6h86`), not purely numeric
- Historical data available back to ~2018 for most programs

### 2. ESPN API (game schedule + results)

- Endpoint: `https://site.api.espn.com/apis/site/v2/sports/lacrosse/mens-college-lacrosse/`
- Provides: game IDs, scores, home/away designations, neutral site, conference/tournament flags
- Free, no API key required
- **Box score stats are NOT available** via ESPN for college lacrosse — the summary
  endpoint returns only game metadata (confirmed by testing multiple game IDs)

### 3. The Odds API (betting lines)

- Markets: `h2h` (moneyline) + `spreads`
- Sport key: `lacrosse_ncaa` (confirmed active)
- Preferred book: BetOnline (`betonline_ag`), then DraftKings, FanDuel, Bovada
- Historical lines: available at higher pricing tiers (not used in v1)

---

## Feature Set

All features are expressed as **home − away differentials** to directly model
the spread. The model predicts `home_score − away_score`.

| Feature | Formula | Direction |
|---------|---------|-----------|
| `fo_pct_diff` | home FO% − away FO% | + = home wins more faceoffs |
| `shot_pct_diff` | home (goals/shots) − away | + = home more efficient |
| `sog_pct_diff` | home (goals/SOG) − away | + = home converts SOG better |
| `save_pct_diff` | home save% − away save% | + = home goalie better |
| `goals_per_game_diff` | home GPG − away GPG | + = home scores more |
| `goals_allowed_diff` | home GA/G − away GA/G | − = home leaks more (bad) |
| `to_diff` | away TO/G − home TO/G | + = home turns it over less |
| `gb_per_game_diff` | home GB/G − away GB/G | + = home wins more ground balls |
| `pace_diff` | home shots/G − away shots/G | model learns direction |
| `home_field` | 0 if neutral, 1 if home site | context feature |

**10 total features** (all computed per-prediction from rolling window).

### Why these features:

**Faceoffs** are uniquely impactful in lacrosse. Every goal restarts with a
faceoff — a team winning 60%+ of faceoffs effectively controls possession rhythm.
FO% differential is expected to be the single strongest predictor.

**Shot efficiency + SOG%** capture quality vs. volume of offense. A disciplined
offense with fewer but higher-quality shots can outperform raw volume metrics.

**Goalie save%** is the defensive analog. Lacrosse goalies face 30–45 shots per
game; a team with a 60%+ save-rate goalie has a structural advantage.

**Turnover differential** (raw TOs only — caused TOs not available from box scores).
`to_diff = away_TO/G − home_TO/G` is positive when home commits fewer turnovers.
Possession protection is critical when there's no shot clock.

**Ground balls** are 50/50 contested possessions. Teams dominating GBs win
possession battles independent of faceoffs.

**Pace** (shots/game): mismatches matter — a high-tempo team vs. a slow, defensive
team creates strategic asymmetry that affects which team's strengths manifest.

**Features NOT included (data unavailable per-game):**
- `clear_pct` — clearing percentage (season aggregates only on lacrosse-ref)
- `caused_turnovers` — not in per-game box score JSON
- Man-up/man-down conversion rates

---

## Rolling Window Approach

Rather than using full-season aggregates, features are computed from each team's
**last N completed games before the prediction date**.

```
ROLLING_WINDOW = 5          # configurable in config.py
MIN_GAMES_FOR_PREDICTION = 3  # minimum before a prediction is generated
```

**Why rolling window over season averages:**
- Captures current form (hot/cold streaks, injury recovery, system changes)
- Automatically handles late-season vs. early-season regime differences
- Early-season: uses all available games (minimum 3); later: uses last 5
- No data leakage — only games completed *before* the prediction date are included

**Per-game stat computation from team's perspective:**
For each game in the window, stats are extracted from the correct side
(home stats if team was home, away stats if team was away) before averaging.

---

## Model Architecture

### v1: Ridge Regression (current)

- **Input:** 10 differential features (listed above)
- **Output:** predicted home margin (continuous regression)
- **Spread edge:** `predicted_margin − market_spread`
  - Positive = model likes home team
  - Negative = model likes away team
- **Win probability:** `sigma(predicted_margin / 5.0)` — logistic transform, scale ≈ 5 goals
- **Regularization (alpha):** 1.0 — tune after sufficient ATS outcome data
- **Training:** closed-form ridge solution (pure NumPy, no sklearn)
- **Normalization:** Z-score per feature on training data; applied at prediction time

**Train/Val/Test split (by season):**
```
Train:  2016–2023  (excludes 2020 — COVID cancellation)
Val:    2024–2025
Test:   2026       (current live season)
```

### Betting threshold:
- Default: flag bets where |spread_edge| ≥ 2.0 goals
- Calibrate empirically once ≥2 full seasons of ATS outcomes accumulate

### Planned model iterations:
- **v2:** Logistic regression directly on ATS cover probability (binary outcome)
- **v3:** Gradient boosted trees — captures non-linear interactions
  (e.g. high FO% team vs. team with weak save% is multiplicatively better)
- **v4:** Opponent-adjusted (SOS-weighted) rolling stats
- **v5:** Rolling window from per-game logs with variable window size tuning

---

## Data Pipeline

### Database schema (SQLite)

| Table | Purpose |
|-------|---------|
| `teams` | Canonical team registry; ESPN name, LR name, LR pro slug |
| `games` | One row per game (all seasons) |
| `game_stats` | Per-game box scores (one row per game, home+away columns) |
| `team_season_stats` | Season aggregates — supplemental, not used by primary pipeline |
| `betting_lines` | Moneyline + spread per game from Odds API |
| `predictions` | Model output per game |
| `results` | Final scores and ATS coverage outcomes |
| `daily_snapshots` | One row per game per day — primary backtesting table |
| `ats_candidates` | View: joins snapshots where model + line are both present |

### Startup / one-time backfill

```bash
python main.py init       # create tables
python main.py backfill   # ~20-30 min: ESPN + LR slugs + ~5,000 box score fetches
python main.py train      # fit Ridge model, print val MAE
```

### Daily in-season (run each morning Feb–May)

```bash
python main.py both       # results_job + daily_job
```

**`results_job`:** Fetches yesterday's final scores from ESPN, updates
`results` table, marks ATS coverage in `daily_snapshots`.

**`daily_job`:**
1. Re-fetches box scores for current season (any new completed games)
2. Fetches today's ESPN schedule
3. Pulls today's lines from The Odds API
4. Generates rolling-window features for each today's game
5. Produces predictions and flags ATS bets with |edge| ≥ threshold
6. Writes `predictions` + `daily_snapshots` rows

---

## Known Limitations & Open Questions

1. **Odds API lacrosse coverage:** College lacrosse has thin book coverage,
   especially for non-power-conference games. Lines may not be available for
   every game.

2. **Historical spread data:** Odds API historical endpoint requires a paid tier.
   For initial training, actual margins serve as the regression target (not ATS
   outcomes directly). The model is calibrated to margin prediction, not
   cover probability.

3. **Team name matching:** ESPN, lacrosse-ref, and The Odds API use different
   name formats. The `shared/team_mapper.py` `resolve_team_name()` function
   handles canonicalization, but manual curation may be needed for ~70 D1 programs.

4. **Early-season small samples:** The model requires MIN_GAMES_FOR_PREDICTION = 3.
   Games in the first two weeks of February may have no prediction.

5. **Caused turnovers missing:** Per-game box scores on pro.lacrossereference.com
   include own turnovers but not caused turnovers. `to_diff` uses
   `away_TO/G − home_TO/G` as a proxy. True turnover margin (caused − committed)
   would be a stronger feature.

6. **2020 season cancelled (COVID):** Excluded from ALL_SEASONS.

7. **Non-D1 opponents:** Some games in the schedule involve non-D1 programs.
   Their box scores are captured for the D1 team's rolling window but they
   don't appear as prediction targets.

---

## Season Structure

```
February:   Regular season begins (conference play)
March:      Conference tournaments
April:      Conference championship weekends; NCAA tournament selection
May:        NCAA Tournament (48 teams as of 2022); Final Four at Gillette Stadium
```

### Key context:
- **Home field:** Significant in college lacrosse; top programs have loud home venues
- **Neutral site:** ACC/Big Ten championship weekends, some early-season tournaments
- **Tournament:** 48-team bracket; campus sites through semifinals since 2022
- **Weather:** February/March games in cold climates (northeast) affect pace/scoring

---

## Conferences (Power Programs)

| Conference | Key Programs |
|-----------|--------------|
| ACC | Duke, Notre Dame, Syracuse, Virginia, UNC, Maryland |
| Big Ten | Maryland, Ohio State, Penn State, Michigan, Rutgers |
| Patriot | Army, Lehigh, Loyola |
| CAA | Delaware, Hofstra, Towson |
| A-10 | Richmond, Massachusetts, La Salle |
| Ivy | Harvard, Yale, Brown, Princeton (no conference tournament) |

Power conferences (ACC, Big Ten) have much better odds coverage and betting action
than mid-majors.
