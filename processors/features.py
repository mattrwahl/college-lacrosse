"""
Feature engineering for the college lacrosse ATS (against-the-spread) model.

Model approach:
  For each matchup (home_team vs away_team), we compute a feature vector of
  differential stats — home_stat minus away_stat — to capture relative team
  quality. The model predicts: predicted_margin = home_score - away_score.
  This predicted margin is compared to the market spread to identify edges.

Core features (all expressed as home - away differentials):
  fo_pct_diff         Faceoff win% differential
  shot_pct_diff       Shot efficiency differential (goals/shots)
  sog_pct_diff        SOG efficiency differential (goals/SOG)
  save_pct_diff       Goalie save% differential
  goals_per_game_diff Scoring rate differential
  goals_allowed_diff  Goals allowed per game differential
  to_diff             Turnover differential (away_to - home_to; positive = home turns it over less)
  gb_per_game_diff    Ground balls per game differential
  pace_diff           Pace proxy differential (shots/game)

Additional context features (not differentials):
  home_field          Home field advantage indicator (1 if not neutral site)

Rolling window: features computed from the last ROLLING_WINDOW completed games
before the prediction date. If fewer games are available, uses all available
(minimum MIN_GAMES_FOR_PREDICTION).

Note: caused_turnovers and clear_pct are not available from per-game box scores,
so to_diff uses raw turnovers only, and clear_pct_diff is omitted.
"""
import sys
import logging
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from config import ROLLING_WINDOW, MIN_GAMES_FOR_PREDICTION

logger = logging.getLogger(__name__)


# Feature names in model input order (must match build_training_dataset output)
FEATURE_NAMES = [
    "fo_pct_diff",
    "shot_pct_diff",
    "sog_pct_diff",
    "save_pct_diff",
    "goals_per_game_diff",
    "goals_allowed_diff",
    "to_diff",
    "gb_per_game_diff",
    "pace_diff",
    "home_field",
]


def load_rolling_stats(
    conn: sqlite3.Connection,
    team_id: int,
    season: int,
    before_date: str,
    window: int = None,
) -> dict | None:
    """
    Compute rolling average stats for a team from their last `window` completed
    games before `before_date` in `season`.

    Returns a flat dict of per-game averages, or None if fewer than
    MIN_GAMES_FOR_PREDICTION games are available.

    Returned keys (compatible with compute_game_features):
        games_played, goals_per_game, goals_allowed_per_game,
        shots_per_game, sog_per_game, shot_pct, sog_pct, save_pct,
        fo_pct, ground_balls_per_game, turnovers_per_game
    """
    if window is None:
        window = ROLLING_WINDOW

    rows = conn.execute(
        """
        SELECT gs.*, g.home_team_id, g.away_team_id, g.game_date
        FROM game_stats gs
        JOIN games g ON g.id = gs.game_id
        WHERE (g.home_team_id = ? OR g.away_team_id = ?)
          AND g.season = ?
          AND g.game_date < ?
          AND gs.home_goals IS NOT NULL
        ORDER BY g.game_date DESC
        LIMIT ?
        """,
        (team_id, team_id, season, before_date, window),
    ).fetchall()

    if len(rows) < MIN_GAMES_FOR_PREDICTION:
        return None

    n = len(rows)

    goals_list, shots_list, sog_list, saves_list = [], [], [], []
    gbs_list, turnovers_list = [], []
    goals_allowed_list, opp_shots_list = [], []
    fo_pct_list, shot_pct_list, sog_pct_list, save_pct_list = [], [], [], []

    for row in rows:
        d = dict(row)
        is_home = (d["home_team_id"] == team_id)

        if is_home:
            goals_list.append(d.get("home_goals") or 0)
            shots_list.append(d.get("home_shots") or 0)
            sog_list.append(d.get("home_sog") or 0)
            saves_list.append(d.get("home_saves") or 0)
            gbs_list.append(d.get("home_gbs") or 0)
            turnovers_list.append(d.get("home_turnovers") or 0)
            goals_allowed_list.append(d.get("away_goals") or 0)
            opp_shots_list.append(d.get("away_shots") or 0)
            if d.get("home_fo_pct") is not None:
                fo_pct_list.append(d["home_fo_pct"])
            if d.get("home_shot_pct") is not None:
                shot_pct_list.append(d["home_shot_pct"])
            if d.get("home_sog_pct") is not None:
                sog_pct_list.append(d["home_sog_pct"])
            if d.get("home_save_pct") is not None:
                save_pct_list.append(d["home_save_pct"])
        else:
            goals_list.append(d.get("away_goals") or 0)
            shots_list.append(d.get("away_shots") or 0)
            sog_list.append(d.get("away_sog") or 0)
            saves_list.append(d.get("away_saves") or 0)
            gbs_list.append(d.get("away_gbs") or 0)
            turnovers_list.append(d.get("away_turnovers") or 0)
            goals_allowed_list.append(d.get("home_goals") or 0)
            opp_shots_list.append(d.get("home_shots") or 0)
            if d.get("away_fo_pct") is not None:
                fo_pct_list.append(d["away_fo_pct"])
            if d.get("away_shot_pct") is not None:
                shot_pct_list.append(d["away_shot_pct"])
            if d.get("away_sog_pct") is not None:
                sog_pct_list.append(d["away_sog_pct"])
            if d.get("away_save_pct") is not None:
                save_pct_list.append(d["away_save_pct"])

    def _avg(lst):
        return sum(lst) / len(lst) if lst else None

    shots_pg = _avg(shots_list)
    goals_pg = _avg(goals_list)

    # Fall back to computing shot_pct from counts if per-game rates are sparse
    shot_pct = _avg(shot_pct_list)
    if shot_pct is None and shots_pg and shots_pg > 0:
        shot_pct = goals_pg / shots_pg

    return {
        "games_played":          n,
        "goals_per_game":        goals_pg,
        "goals_allowed_per_game": _avg(goals_allowed_list),
        "shots_per_game":        shots_pg,
        "sog_per_game":          _avg(sog_list),
        "shot_pct":              shot_pct,
        "sog_pct":               _avg(sog_pct_list),
        "save_pct":              _avg(save_pct_list),
        "fo_pct":                _avg(fo_pct_list),
        "ground_balls_per_game": _avg(gbs_list),
        "turnovers_per_game":    _avg(turnovers_list),
    }


def compute_game_features(
    home_stats: dict, away_stats: dict, neutral_site: int = 0
) -> dict | None:
    """
    Compute the feature vector for a single matchup from rolling stats dicts.

    Returns dict keyed by FEATURE_NAMES (plus home_field), or None if critical
    data is missing.
    """
    def _f(stats, key, default=0.0):
        v = stats.get(key)
        return float(v) if v is not None else default

    # Require goals_per_game for both teams as a minimum viability check
    if _f(home_stats, "goals_per_game", None) is None or \
       _f(away_stats, "goals_per_game", None) is None:
        return None

    # to_diff: positive = home team commits fewer turnovers (better for home)
    h_to = _f(home_stats, "turnovers_per_game")
    a_to = _f(away_stats, "turnovers_per_game")
    to_diff = a_to - h_to

    features = {
        "fo_pct_diff":          _f(home_stats, "fo_pct", 0.5) - _f(away_stats, "fo_pct", 0.5),
        "shot_pct_diff":        _f(home_stats, "shot_pct") - _f(away_stats, "shot_pct"),
        "sog_pct_diff":         _f(home_stats, "sog_pct") - _f(away_stats, "sog_pct"),
        "save_pct_diff":        _f(home_stats, "save_pct") - _f(away_stats, "save_pct"),
        "goals_per_game_diff":  _f(home_stats, "goals_per_game") - _f(away_stats, "goals_per_game"),
        "goals_allowed_diff":   _f(home_stats, "goals_allowed_per_game") - _f(away_stats, "goals_allowed_per_game"),
        "to_diff":              to_diff,
        "gb_per_game_diff":     _f(home_stats, "ground_balls_per_game") - _f(away_stats, "ground_balls_per_game"),
        "pace_diff":            _f(home_stats, "shots_per_game") - _f(away_stats, "shots_per_game"),
        "home_field":           0 if neutral_site else 1,
    }

    return features


def build_training_dataset(conn: sqlite3.Connection, seasons: list[int]) -> tuple:
    """
    Build (X, y) arrays from historical games with known results and box scores.

    X: feature matrix (n_games, n_features)
    y: actual margin = home_score - away_score (continuous, for regression)

    Returns (X, y, game_ids).
    """
    rows = conn.execute(
        """
        SELECT
            g.id as game_id,
            g.game_date,
            g.season,
            g.home_team_id,
            g.away_team_id,
            g.neutral_site,
            r.actual_margin
        FROM games g
        JOIN results r ON r.game_id = g.id
        WHERE g.season IN ({})
          AND r.actual_margin IS NOT NULL
        ORDER BY g.game_date
        """.format(",".join("?" * len(seasons))),
        seasons,
    ).fetchall()

    X_list, y_list, game_ids = [], [], []

    for row in rows:
        game_id = row["game_id"]
        game_date = row["game_date"]
        season = row["season"]
        neutral = row["neutral_site"] or 0

        home_stats = load_rolling_stats(conn, row["home_team_id"], season, game_date)
        away_stats = load_rolling_stats(conn, row["away_team_id"], season, game_date)

        if home_stats is None or away_stats is None:
            continue

        features = compute_game_features(home_stats, away_stats, neutral)
        if features is None:
            continue

        feature_vec = [features.get(name, 0.0) or 0.0 for name in FEATURE_NAMES]

        X_list.append(feature_vec)
        y_list.append(float(row["actual_margin"]))
        game_ids.append(game_id)

    if not X_list:
        logger.warning("No training samples found")
        return np.empty((0, len(FEATURE_NAMES))), np.empty(0), []

    X = np.array(X_list, dtype=float)
    y = np.array(y_list, dtype=float)

    logger.info(f"Built training dataset: {len(y)} games across seasons {seasons}")
    return X, y, game_ids


def normalize_features(X: np.ndarray, mean: np.ndarray = None, std: np.ndarray = None):
    """
    Z-score normalize feature matrix. If mean/std provided, apply them (for test data).
    Returns (X_normalized, mean, std).
    """
    if mean is None:
        mean = np.nanmean(X, axis=0)
    if std is None:
        std = np.nanstd(X, axis=0)
    std_safe = np.where(std == 0, 1.0, std)
    X_norm = (X - mean) / std_safe
    X_norm = np.where(np.isnan(X_norm), 0.0, X_norm)
    return X_norm, mean, std
