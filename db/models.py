"""
SQLite schema for the college lacrosse betting model.

Core design:
  - teams:            canonical team registry with name aliases per source
  - games:            one row per game (historical + future)
  - game_stats:       per-game box scores (one row per game), populated from lacrosse-ref
  - team_season_stats: season-level aggregate stats (supplemental; not used by rolling window)
  - betting_lines:    moneylines + spreads from The Odds API
  - predictions:      model-generated predicted spread + confidence per game
  - results:          final scores / ATS outcome

The spread model uses a rolling window of game_stats rows to compute per-team
feature averages before each prediction. See processors/features.py.
"""

CREATE_TEAMS = """
CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT NOT NULL UNIQUE,
    espn_name       TEXT,
    espn_id         TEXT,
    lacrosse_ref_name TEXT,
    lr_pro_slug     TEXT,               -- pro.lacrossereference.com team slug (e.g. "dukem-5269")
    ncaa_name       TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);
"""

CREATE_GAMES = """
CREATE TABLE IF NOT EXISTS games (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season          INTEGER NOT NULL,
    game_date       TEXT NOT NULL,              -- YYYY-MM-DD
    home_team_id    INTEGER REFERENCES teams(id),
    away_team_id    INTEGER REFERENCES teams(id),
    espn_game_id    TEXT UNIQUE,
    lr_game_slug    TEXT,                       -- lacrosse-ref slug for LR-sourced games
    source          TEXT DEFAULT 'espn',        -- 'espn' or 'lr'
    neutral_site    INTEGER DEFAULT 0,
    conference_game INTEGER DEFAULT 0,
    tournament_game INTEGER DEFAULT 0,          -- 1 if NCAA tournament
    tournament_round TEXT,                      -- e.g. "First Round", "Quarterfinal"
    created_at      TEXT DEFAULT (datetime('now'))
);
"""

# Partial unique index on lr_game_slug (NULLs excluded — ESPN rows leave it NULL)
CREATE_GAMES_LR_SLUG_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_games_lr_slug
    ON games(lr_game_slug) WHERE lr_game_slug IS NOT NULL;
"""

# Per-game box score stats — one row per game (home+away in same row).
# Populated from pro.lacrossereference.com box scores.
# game_slug is the lacrosse-ref URL slug (e.g. "game-duke-vs-jacksonville-mlax-2026-8195").
CREATE_GAME_STATS = """
CREATE TABLE IF NOT EXISTS game_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         INTEGER NOT NULL REFERENCES games(id),
    game_slug       TEXT,                       -- lacrosse-ref game slug for deduplication

    -- Raw counts (home team)
    home_goals          INTEGER,
    home_shots          INTEGER,
    home_sog            INTEGER,
    home_assists        INTEGER,
    home_possessions    INTEGER,
    home_turnovers      INTEGER,
    home_gbs            INTEGER,
    home_faceoff_wins   INTEGER,
    home_saves          INTEGER,
    home_top            REAL,                   -- time of possession fraction (0–1)

    -- Raw counts (away team)
    away_goals          INTEGER,
    away_shots          INTEGER,
    away_sog            INTEGER,
    away_assists        INTEGER,
    away_possessions    INTEGER,
    away_turnovers      INTEGER,
    away_gbs            INTEGER,
    away_faceoff_wins   INTEGER,
    away_saves          INTEGER,
    away_top            REAL,

    -- Shared
    faceoffs_total      INTEGER,

    -- Derived rates (home team)
    home_fo_pct         REAL,                   -- home_faceoff_wins / faceoffs_total
    home_shot_pct       REAL,                   -- home_goals / home_shots
    home_sog_pct        REAL,                   -- home_goals / home_sog
    home_save_pct       REAL,                   -- home_saves / away_sog (saves / opp SOG)

    -- Derived rates (away team)
    away_fo_pct         REAL,
    away_shot_pct       REAL,
    away_sog_pct        REAL,
    away_save_pct       REAL,                   -- away_saves / home_sog

    fetched_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(game_id)
);
"""

# Season-level aggregated stats per team — supplemental data from lacrosse-ref stats pages.
# Not used by the primary rolling-window feature pipeline; kept for reference/fallback.
CREATE_TEAM_SEASON_STATS = """
CREATE TABLE IF NOT EXISTS team_season_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    season          INTEGER NOT NULL,
    games_played    INTEGER NOT NULL,
    snapshot_date   TEXT NOT NULL,              -- date these stats were scraped

    -- Offense
    goals_per_game          REAL,
    shots_per_game          REAL,
    sog_per_game            REAL,
    shot_pct                REAL,
    sog_pct                 REAL,

    -- Defense (opponent stats)
    goals_allowed_per_game  REAL,
    opp_shots_per_game      REAL,
    opp_sog_per_game        REAL,
    opp_shot_pct            REAL,

    -- Goalie
    save_pct                REAL,

    -- Faceoffs
    fo_pct                  REAL,
    opp_fo_pct              REAL,               -- derived: 1 - team FO%
    fo_pct_diff             REAL,               -- fo_pct - 0.5

    -- Turnovers / ground balls
    turnovers_per_game      REAL,
    caused_turnovers_per_game REAL,
    ground_balls_per_game   REAL,
    turnover_margin         REAL,               -- caused_turnovers - turnovers per game

    -- Clearings
    clear_pct               REAL,

    -- Pace
    shots_per_game_pace     REAL,

    -- Man-up
    man_up_pct              REAL,

    -- Win/loss context
    wins                    INTEGER,
    losses                  INTEGER,

    UNIQUE(team_id, season, snapshot_date)
);
"""

# Betting lines: moneylines + spreads per game
CREATE_BETTING_LINES = """
CREATE TABLE IF NOT EXISTS betting_lines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         INTEGER NOT NULL REFERENCES games(id),
    scraped_at      TEXT DEFAULT (datetime('now')),
    book            TEXT,

    -- Moneyline
    home_moneyline  INTEGER,
    away_moneyline  INTEGER,
    home_implied_prob REAL,
    away_implied_prob REAL,
    home_novig_prob  REAL,
    away_novig_prob  REAL,
    vig             REAL,

    -- Spread
    home_spread     REAL,                       -- negative = home favored (e.g. -4.5)
    away_spread     REAL,
    home_spread_juice INTEGER,
    away_spread_juice INTEGER,

    UNIQUE(game_id, book, scraped_at)
);
"""

# Model predictions for each game
CREATE_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id             INTEGER NOT NULL REFERENCES games(id),
    generated_at        TEXT DEFAULT (datetime('now')),
    model_version       TEXT NOT NULL,

    -- Predicted margin (positive = home wins by N)
    predicted_spread    REAL,
    predicted_home_win_prob REAL,

    -- Market line at time of prediction
    market_spread       REAL,

    -- Edge = predicted_spread - market_spread
    spread_edge         REAL,

    -- Feature snapshot (JSON-encoded dict for reproducibility)
    features_json       TEXT,

    UNIQUE(game_id, model_version)
);
"""

CREATE_RESULTS = """
CREATE TABLE IF NOT EXISTS results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         INTEGER NOT NULL REFERENCES games(id),
    home_score      INTEGER,
    away_score      INTEGER,
    actual_margin   INTEGER,                    -- home_score - away_score
    winner_team_id  INTEGER REFERENCES teams(id),
    game_status     TEXT,
    fetched_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(game_id)
);
"""

# Daily snapshot: one row per game per day — joins predictions + lines + results
CREATE_DAILY_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS daily_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date       TEXT NOT NULL,
    game_id             INTEGER NOT NULL REFERENCES games(id),
    season              INTEGER NOT NULL,
    home_team           TEXT,
    away_team           TEXT,
    game_date           TEXT,
    neutral_site        INTEGER DEFAULT 0,

    -- Lines
    home_moneyline      INTEGER,
    away_moneyline      INTEGER,
    home_novig_prob     REAL,
    away_novig_prob     REAL,
    market_spread       REAL,                   -- home spread from sportsbook

    -- Model output
    model_version       TEXT,
    predicted_spread    REAL,
    spread_edge         REAL,                   -- predicted - market
    predicted_home_win_prob REAL,

    -- Results (filled in after game)
    home_score          INTEGER,
    away_score          INTEGER,
    actual_margin       INTEGER,
    result_home_win     INTEGER,                -- 1=home won, 0=away won
    home_covered        INTEGER,                -- 1=home covered market spread
    away_covered        INTEGER,

    created_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(snapshot_date, game_id)
);
"""

# ATS performance view
CREATE_ATS_CANDIDATES = """
CREATE VIEW IF NOT EXISTS ats_candidates AS
SELECT
    ds.snapshot_date,
    ds.game_id,
    ds.season,
    ds.game_date,
    ds.home_team,
    ds.away_team,

    CASE
        WHEN ds.spread_edge IS NULL THEN NULL
        WHEN ds.spread_edge < 0 THEN 'away'
        ELSE 'home'
    END                                         AS model_side,

    ABS(ds.spread_edge)                         AS abs_edge,
    ds.market_spread,
    ds.predicted_spread,
    ds.spread_edge,

    ds.home_moneyline,
    ds.away_moneyline,
    ds.home_novig_prob,
    ds.away_novig_prob,

    ds.home_covered,
    ds.away_covered,
    ds.actual_margin,
    ds.neutral_site,
    ds.model_version
FROM daily_snapshots ds
WHERE ds.spread_edge IS NOT NULL
  AND ds.market_spread IS NOT NULL;
"""

ALL_TABLES = [
    CREATE_TEAMS,
    CREATE_GAMES,
    CREATE_GAME_STATS,
    CREATE_TEAM_SEASON_STATS,
    CREATE_BETTING_LINES,
    CREATE_PREDICTIONS,
    CREATE_RESULTS,
    CREATE_DAILY_SNAPSHOTS,
    CREATE_ATS_CANDIDATES,
]
