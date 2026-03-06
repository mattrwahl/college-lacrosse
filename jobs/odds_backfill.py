"""
Historical odds backfill: fetch past-season spread lines from Odds API,
generate model predictions, and populate daily_snapshots for ATS evaluation.

Designed for val/test seasons where we have game_stats + results but no lines.

Usage:
    python main.py backfill-odds               # all VAL_SEASONS
    python main.py backfill-odds --season 2025 # single season
    python main.py backfill-odds --seasons 2024 2025

Requires paid Odds API plan with Historian access.
"""
import sys
import logging
import json
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import get_db
from scrapers.odds_api import fetch_historical_odds
from processors.model import load_model, predict_game, MODEL_VERSION
from config import VAL_SEASONS, SPREAD_THRESHOLD
from shared.implied_probs import american_to_implied_prob, remove_vig

logger = logging.getLogger(__name__)


def run(seasons: list[int] | None = None):
    """
    Fetch historical lines for past seasons and populate betting + prediction tables.
    """
    if seasons is None:
        seasons = VAL_SEASONS

    conn = get_db()
    model = load_model(conn)
    if model is None:
        logger.error("No model found — run 'python main.py train' first")
        conn.close()
        return

    # Get all completed games in target seasons with box scores
    placeholders = ",".join("?" * len(seasons))
    games = conn.execute(
        f"""
        SELECT g.id, g.game_date, g.season, g.home_team_id, g.away_team_id,
               g.espn_game_id, g.neutral_site,
               t1.canonical_name AS home_name,
               t2.canonical_name AS away_name,
               r.home_score, r.away_score, r.actual_margin
        FROM games g
        JOIN teams t1 ON t1.id = g.home_team_id
        JOIN teams t2 ON t2.id = g.away_team_id
        JOIN results r ON r.game_id = g.id
        JOIN game_stats gs ON gs.game_id = g.id
        WHERE g.season IN ({placeholders})
          AND r.game_status = 'final'
        ORDER BY g.game_date
        """,
        tuple(seasons),
    ).fetchall()

    logger.info(f"Found {len(games)} completed games with box scores in seasons {seasons}")
    if not games:
        logger.warning("No games found. Run historical_backfill first.")
        conn.close()
        return

    # Collect unique game dates
    unique_dates = sorted(set(g["game_date"] for g in games))
    logger.info(f"Fetching historical odds for {len(unique_dates)} game dates...")

    # Fetch historical odds once per date (10am ET = 14:00 UTC, safely pre-game)
    odds_by_date: dict[str, list[dict]] = {}
    for i, game_date in enumerate(unique_dates):
        date_iso = f"{game_date}T14:00:00Z"
        try:
            odds_list = fetch_historical_odds(date_iso)
            odds_by_date[game_date] = odds_list
            if odds_list:
                logger.info(f"  {game_date}: {len(odds_list)} games with lines")
            time.sleep(0.3)  # be polite to API
        except Exception as e:
            logger.warning(f"  {game_date}: API error — {e}")
            odds_by_date[game_date] = []

    # Process each game
    matched = predicted = 0
    for game in games:
        game_date = game["game_date"]
        home_name = game["home_name"]
        away_name = game["away_name"]

        # Match to odds
        odds = _match_odds(home_name, away_name, odds_by_date.get(game_date, []))
        if odds and odds.get("home_spread") is not None:
            matched += 1
            _store_betting_line(conn, game["id"], odds, game_date)

        # Generate model prediction
        prediction = predict_game(
            conn,
            game["home_team_id"],
            game["away_team_id"],
            game_date=game_date,
            season=game["season"],
            neutral_site=game["neutral_site"] or 0,
            model=model,
        )

        if prediction:
            predicted += 1
            market_spread = (odds or {}).get("home_spread")
            spread_edge = None
            if market_spread is not None:
                spread_edge = round(prediction["predicted_spread"] + market_spread, 2)

            _store_prediction(conn, game["id"], prediction, market_spread, spread_edge, game_date)
            _store_snapshot(conn, game, prediction, odds, spread_edge)

    conn.commit()
    logger.info(
        f"Done: {predicted} predictions generated, "
        f"{matched} matched to spread lines"
    )

    # ATS evaluation
    _print_ats_summary(conn, seasons)
    conn.close()


def _match_odds(home_name: str, away_name: str, odds_games: list[dict]) -> dict | None:
    """Fuzzy team name match — same logic as daily_job._match_odds."""
    home_lower = home_name.lower()
    away_lower = away_name.lower()
    for odds in odds_games:
        oh = (odds.get("home_team") or "").lower()
        oa = (odds.get("away_team") or "").lower()
        if oh == home_lower and oa == away_lower:
            return odds
        if _partial_name_match(home_lower, oh) and _partial_name_match(away_lower, oa):
            return odds
    return None


def _partial_name_match(name_a: str, name_b: str) -> bool:
    words_a = name_a.split()
    words_b = name_b.split()
    if not words_a or not words_b:
        return False
    return words_a[-1] in name_b or words_b[-1] in name_a


def _store_betting_line(conn, game_id: int, odds: dict, game_date: str):
    h_ml = odds.get("home_moneyline")
    a_ml = odds.get("away_moneyline")
    home_nv = away_nv = vig = h_imp = a_imp = None

    if h_ml is not None and a_ml is not None:
        h_imp = american_to_implied_prob(h_ml)
        a_imp = american_to_implied_prob(a_ml)
        home_nv, away_nv = remove_vig(h_imp, a_imp)
        vig = round(h_imp + a_imp - 1.0, 4)

    with conn:
        conn.execute(
            """
            INSERT INTO betting_lines (
                game_id, scraped_at, book,
                home_moneyline, away_moneyline,
                home_implied_prob, away_implied_prob,
                home_novig_prob, away_novig_prob, vig,
                home_spread, away_spread,
                home_spread_juice, away_spread_juice
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id, book, scraped_at) DO NOTHING
            """,
            (
                game_id, odds.get("scraped_at", game_date), odds.get("book"),
                h_ml, a_ml,
                round(h_imp, 4) if h_imp else None,
                round(a_imp, 4) if a_imp else None,
                round(home_nv, 4) if home_nv else None,
                round(away_nv, 4) if away_nv else None,
                vig,
                odds.get("home_spread"),
                odds.get("away_spread"),
                odds.get("home_spread_juice"),
                odds.get("away_spread_juice"),
            ),
        )


def _store_prediction(conn, game_id: int, prediction: dict, market_spread, spread_edge, game_date: str):
    with conn:
        conn.execute(
            """
            INSERT INTO predictions (
                game_id, generated_at, model_version,
                predicted_spread, predicted_home_win_prob,
                market_spread, spread_edge, features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id, model_version) DO UPDATE SET
                predicted_spread = excluded.predicted_spread,
                predicted_home_win_prob = excluded.predicted_home_win_prob,
                market_spread = excluded.market_spread,
                spread_edge = excluded.spread_edge,
                features_json = excluded.features_json,
                generated_at = excluded.generated_at
            """,
            (
                game_id, game_date, MODEL_VERSION,
                prediction["predicted_spread"],
                prediction["predicted_home_win_prob"],
                market_spread,
                spread_edge,
                json.dumps(prediction.get("features", {})),
            ),
        )


def _store_snapshot(conn, game: dict, prediction: dict, odds: dict | None, spread_edge):
    """Write a daily_snapshots row for a historical game (with results already known)."""
    actual_margin = game["actual_margin"]
    home_score = game["home_score"]
    away_score = game["away_score"]
    market_spread = (odds or {}).get("home_spread")
    game_date = game["game_date"]

    home_covered = away_covered = None
    if market_spread is not None and actual_margin is not None:
        home_covered = 1 if (actual_margin + market_spread) > 0 else 0
        away_covered = 1 - home_covered

    h_ml = (odds or {}).get("home_moneyline")
    a_ml = (odds or {}).get("away_moneyline")
    home_nv = away_nv = None
    if h_ml and a_ml:
        from shared.implied_probs import american_to_implied_prob, remove_vig
        home_nv, away_nv = remove_vig(
            american_to_implied_prob(h_ml),
            american_to_implied_prob(a_ml),
        )
        home_nv = round(home_nv, 4)
        away_nv = round(away_nv, 4)

    with conn:
        conn.execute(
            """
            INSERT INTO daily_snapshots (
                snapshot_date, game_id, season,
                home_team, away_team, game_date, neutral_site,
                home_moneyline, away_moneyline,
                home_novig_prob, away_novig_prob,
                market_spread, model_version,
                predicted_spread, spread_edge, predicted_home_win_prob,
                home_score, away_score, actual_margin, result_home_win,
                home_covered, away_covered
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, game_id) DO UPDATE SET
                market_spread = COALESCE(excluded.market_spread, market_spread),
                predicted_spread = COALESCE(excluded.predicted_spread, predicted_spread),
                spread_edge = COALESCE(excluded.spread_edge, spread_edge),
                home_covered = COALESCE(excluded.home_covered, home_covered),
                away_covered = COALESCE(excluded.away_covered, away_covered)
            """,
            (
                game_date, game["id"], game["season"],
                game["home_name"], game["away_name"],
                game_date,
                game["neutral_site"] or 0,
                h_ml, a_ml,
                home_nv, away_nv,
                market_spread,
                MODEL_VERSION if prediction else None,
                prediction["predicted_spread"] if prediction else None,
                spread_edge,
                prediction["predicted_home_win_prob"] if prediction else None,
                home_score, away_score, actual_margin,
                1 if (actual_margin or 0) > 0 else 0,
                home_covered, away_covered,
            ),
        )


def _print_ats_summary(conn, seasons: list[int]):
    """Print ATS performance for the backfilled seasons."""
    placeholders = ",".join("?" * len(seasons))
    rows = conn.execute(
        f"""
        SELECT
            season,
            COUNT(*) AS bets,
            SUM(CASE WHEN (model_side = 'home' AND home_covered = 1)
                       OR (model_side = 'away' AND away_covered = 1)
                     THEN 1 ELSE 0 END) AS wins,
            AVG(abs_edge) AS avg_edge
        FROM ats_candidates
        WHERE home_covered IS NOT NULL
          AND season IN ({placeholders})
        GROUP BY season
        ORDER BY season
        """,
        tuple(seasons),
    ).fetchall()

    # Overall
    all_rows = conn.execute(
        f"""
        SELECT
            COUNT(*) AS bets,
            SUM(CASE WHEN (model_side = 'home' AND home_covered = 1)
                       OR (model_side = 'away' AND away_covered = 1)
                     THEN 1 ELSE 0 END) AS wins,
            AVG(abs_edge) AS avg_edge
        FROM ats_candidates
        WHERE home_covered IS NOT NULL
          AND season IN ({placeholders})
        """,
        tuple(seasons),
    ).fetchone()

    threshold_rows = conn.execute(
        f"""
        SELECT
            COUNT(*) AS bets,
            SUM(CASE WHEN (model_side = 'home' AND home_covered = 1)
                       OR (model_side = 'away' AND away_covered = 1)
                     THEN 1 ELSE 0 END) AS wins
        FROM ats_candidates
        WHERE home_covered IS NOT NULL
          AND abs_edge >= ?
          AND season IN ({placeholders})
        """,
        (SPREAD_THRESHOLD, *tuple(seasons)),
    ).fetchone()

    print(f"\n{'='*55}")
    print(f"  ATS BACKTEST — Seasons {seasons}")
    print(f"{'='*55}")
    print(f"  {'Season':>6}  {'W-L':>8}  {'Win%':>6}  {'AvgEdge':>8}")
    print(f"  {'-'*45}")
    for r in rows:
        bets = r["bets"]
        wins = r["wins"]
        losses = bets - wins
        pct = (wins / bets * 100) if bets else 0
        print(f"  {r['season']:>6}  {wins:>3}-{losses:<3}   {pct:>5.1f}%  {r['avg_edge']:>7.2f}")

    if all_rows and all_rows["bets"]:
        bets = all_rows["bets"]
        wins = all_rows["wins"]
        losses = bets - wins
        pct = wins / bets * 100
        print(f"  {'Total':>6}  {wins:>3}-{losses:<3}   {pct:>5.1f}%  {all_rows['avg_edge']:>7.2f}")

    if threshold_rows and threshold_rows["bets"]:
        bets = threshold_rows["bets"]
        wins = threshold_rows["wins"]
        losses = bets - wins
        pct = wins / bets * 100
        print(f"\n  High-edge only (|edge| >= {SPREAD_THRESHOLD}):")
        print(f"    {wins}-{losses}  ({pct:.1f}%)  — {bets} bets")
    print(f"{'='*55}\n")
