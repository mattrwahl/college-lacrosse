"""
College lacrosse ATS prediction model.

Model: Ridge regression on game-level feature differentials.
       Predicts home_margin = home_score - away_score.
       Spread edge = predicted_margin - market_spread (positive = home team value,
       negative = away team value).

This is a v1 foundation. Planned iterations:
  v2 — logistic regression on ATS cover probability directly
  v3 — gradient boosted trees (XGBoost/LightGBM) with more features
  v4 — rolling opponent-adjusted stats (SOS-weighted)

Fit with: python main.py train
Predict with: python main.py predict (called automatically by daily_job)
"""
import sys
import logging
import json
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from config import TRAIN_SEASONS, VAL_SEASONS, DB_PATH, SPREAD_THRESHOLD
from processors.features import (
    FEATURE_NAMES,
    build_training_dataset,
    normalize_features,
    load_rolling_stats,
    compute_game_features,
)

logger = logging.getLogger(__name__)

MODEL_VERSION = "ridge_v1"


class RidgeSpreadModel:
    """
    Simple Ridge regression spread predictor.
    Uses sklearn-style interface without sklearn dependency (pure numpy).
    """

    def __init__(self, alpha: float = 1.0):
        self.alpha = alpha
        self.weights: np.ndarray | None = None
        self.bias: float = 0.0
        self.feature_mean: np.ndarray | None = None
        self.feature_std: np.ndarray | None = None
        self.n_features: int = 0

    def fit(self, X: np.ndarray, y: np.ndarray):
        """
        Fit ridge regression: minimize ||Xw - y||^2 + alpha * ||w||^2.
        Closed-form solution: w = (X^T X + alpha I)^{-1} X^T y
        """
        X_norm, self.feature_mean, self.feature_std = normalize_features(X)
        self.n_features = X_norm.shape[1]

        # Add bias column
        X_b = np.hstack([X_norm, np.ones((X_norm.shape[0], 1))])
        n_feat = X_b.shape[1]

        # Ridge penalty: don't penalize bias term
        reg = np.eye(n_feat) * self.alpha
        reg[-1, -1] = 0.0  # no penalty on bias

        try:
            self.weights = np.linalg.solve(X_b.T @ X_b + reg, X_b.T @ y)
            self.bias = float(self.weights[-1])
            self.weights = self.weights[:-1]
        except np.linalg.LinAlgError as e:
            logger.error(f"Ridge fit failed: {e}")
            self.weights = np.zeros(X_norm.shape[1])
            self.bias = np.mean(y)

        logger.info(f"Ridge model fit on {X.shape[0]} samples, alpha={self.alpha}")

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Predict home margin for feature matrix X."""
        if self.weights is None:
            raise RuntimeError("Model not fitted — run fit() first or load from DB")
        X_norm, _, _ = normalize_features(X, self.feature_mean, self.feature_std)
        return X_norm @ self.weights + self.bias

    def to_dict(self) -> dict:
        return {
            "alpha": self.alpha,
            "weights": self.weights.tolist() if self.weights is not None else None,
            "bias": self.bias,
            "feature_mean": self.feature_mean.tolist() if self.feature_mean is not None else None,
            "feature_std": self.feature_std.tolist() if self.feature_std is not None else None,
            "n_features": self.n_features,
            "feature_names": FEATURE_NAMES,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RidgeSpreadModel":
        m = cls(alpha=d["alpha"])
        m.weights = np.array(d["weights"]) if d.get("weights") else None
        m.bias = d.get("bias", 0.0)
        m.feature_mean = np.array(d["feature_mean"]) if d.get("feature_mean") else None
        m.feature_std = np.array(d["feature_std"]) if d.get("feature_std") else None
        m.n_features = d.get("n_features", 0)
        return m


def save_model(conn: sqlite3.Connection, model: RidgeSpreadModel, version: str = MODEL_VERSION):
    """Persist model weights to a JSON blob in a simple model_params table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_params (
            version     TEXT PRIMARY KEY,
            params_json TEXT NOT NULL,
            saved_at    TEXT DEFAULT (datetime('now'))
        )
        """
    )
    params_json = json.dumps(model.to_dict())
    conn.execute(
        """
        INSERT INTO model_params (version, params_json)
        VALUES (?, ?)
        ON CONFLICT(version) DO UPDATE SET
            params_json = excluded.params_json,
            saved_at    = datetime('now')
        """,
        (version, params_json),
    )
    logger.info(f"Model '{version}' saved to DB")


def load_model(conn: sqlite3.Connection, version: str = MODEL_VERSION) -> RidgeSpreadModel | None:
    """Load model weights from DB."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS model_params (version TEXT PRIMARY KEY, params_json TEXT NOT NULL, saved_at TEXT)"
    )
    row = conn.execute(
        "SELECT params_json FROM model_params WHERE version = ?", (version,)
    ).fetchone()
    if row is None:
        logger.warning(f"No model found for version '{version}'")
        return None
    return RidgeSpreadModel.from_dict(json.loads(row[0]))


def train(conn: sqlite3.Connection, alpha: float = 1.0) -> RidgeSpreadModel:
    """
    Train the Ridge regression model on TRAIN_SEASONS data.
    Evaluates on VAL_SEASONS and prints MAE.
    """
    logger.info(f"Building training dataset for seasons {TRAIN_SEASONS}")
    X_train, y_train, train_ids = build_training_dataset(conn, TRAIN_SEASONS)

    if len(y_train) == 0:
        raise RuntimeError(
            "No training data found. Run historical_backfill first:\n"
            "  python main.py backfill"
        )

    model = RidgeSpreadModel(alpha=alpha)
    model.fit(X_train, y_train)

    # Validation
    if VAL_SEASONS:
        X_val, y_val, val_ids = build_training_dataset(conn, VAL_SEASONS)
        if len(y_val) > 0:
            y_pred = model.predict(X_val)
            mae = float(np.mean(np.abs(y_pred - y_val)))
            rmse = float(np.sqrt(np.mean((y_pred - y_val) ** 2)))
            logger.info(f"Validation ({VAL_SEASONS}): MAE={mae:.2f} goals, RMSE={rmse:.2f} goals, n={len(y_val)}")

    # Save weights
    with conn:
        save_model(conn, model)

    _print_feature_weights(model)
    return model


def predict_game(
    conn: sqlite3.Connection,
    home_team_id: int,
    away_team_id: int,
    game_date: str,
    season: int,
    neutral_site: int = 0,
    model: RidgeSpreadModel | None = None,
) -> dict | None:
    """
    Generate a spread prediction for a single upcoming game.

    Returns dict:
        {
            "predicted_spread": float,      # positive = home favored by N
            "predicted_home_win_prob": float,
            "features": dict,
        }
    or None if insufficient data.
    """
    if model is None:
        model = load_model(conn)
        if model is None:
            logger.error("No model in DB — run 'python main.py train' first")
            return None

    home_stats = load_rolling_stats(conn, home_team_id, season, game_date)
    away_stats = load_rolling_stats(conn, away_team_id, season, game_date)

    if home_stats is None:
        logger.debug(f"Insufficient stats for home team {home_team_id}")
        return None
    if away_stats is None:
        logger.debug(f"Insufficient stats for away team {away_team_id}")
        return None

    features = compute_game_features(home_stats, away_stats, neutral_site)
    if features is None:
        return None

    feature_vec = np.array(
        [features.get(name, 0.0) or 0.0 for name in FEATURE_NAMES],
        dtype=float,
    ).reshape(1, -1)

    predicted_margin = float(model.predict(feature_vec)[0])

    # Logistic transform for win probability (sigma(margin / scale))
    # Scale of ~5 goals works reasonably for lacrosse scoring
    scale = 5.0
    home_win_prob = float(1 / (1 + np.exp(-predicted_margin / scale)))

    return {
        "predicted_spread": round(predicted_margin, 2),
        "predicted_home_win_prob": round(home_win_prob, 4),
        "features": features,
    }


def evaluate_ats_performance(conn: sqlite3.Connection, min_edge: float = None):
    """
    Print ATS performance summary from the ats_candidates view.
    """
    if min_edge is None:
        min_edge = SPREAD_THRESHOLD

    rows = conn.execute(
        """
        SELECT
            model_side,
            COUNT(*) as bets,
            SUM(CASE WHEN (model_side = 'home' AND home_covered = 1)
                       OR (model_side = 'away' AND away_covered = 1)
                     THEN 1 ELSE 0 END) as wins,
            AVG(abs_edge) as avg_edge,
            MIN(season) as min_season,
            MAX(season) as max_season
        FROM ats_candidates
        WHERE abs_edge >= ?
          AND home_covered IS NOT NULL
        GROUP BY model_side
        """,
        (min_edge,),
    ).fetchall()

    if not rows:
        print(f"No completed bets found with edge >= {min_edge}")
        return

    print(f"\n--- ATS Performance (edge >= {min_edge}) ---")
    total_bets = total_wins = 0
    for row in rows:
        wins = row["wins"]
        bets = row["bets"]
        win_pct = (wins / bets * 100) if bets > 0 else 0
        total_bets += bets
        total_wins += wins
        print(f"  {row['model_side']:5s}: {wins}/{bets} ({win_pct:.1f}%)  avg_edge={row['avg_edge']:.2f}")
    overall = (total_wins / total_bets * 100) if total_bets > 0 else 0
    print(f"  Total: {total_wins}/{total_bets} ({overall:.1f}%)")


def _print_feature_weights(model: RidgeSpreadModel):
    names = FEATURE_NAMES
    weights = model.weights.tolist()
    print("\n--- Feature Weights (Ridge v1) ---")
    for name, w in sorted(zip(names, weights), key=lambda x: abs(x[1]), reverse=True):
        print(f"  {name:25s}: {w:+.4f}")
    print(f"  {'bias':25s}: {model.bias:+.4f}\n")
