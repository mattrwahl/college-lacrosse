"""
Daily job: scrape today's lines and generate spread predictions.

Run each morning during the lacrosse season (roughly Feb–May).
Updates team season stats from lacrosse-ref, then for each game
with a market line generates a predicted spread and ATS recommendation.

Usage:
    python main.py daily
"""
import sys
import logging
import json
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import get_db, upsert_team, upsert_game, get_team_id, init_db
from scrapers.odds_api import scrape_lines
from scrapers.espn_scraper import fetch_scoreboard
from processors.model import load_model, predict_game, MODEL_VERSION
from config import SEASON_YEAR, SPREAD_THRESHOLD
from shared.implied_probs import american_to_implied_prob, remove_vig

logger = logging.getLogger(__name__)


def run():
    init_db()
    conn = get_db()
    today = date.today()
    today_str = today.isoformat()

    # 1. Update team season stats (daily refresh)
    _refresh_team_stats(conn, today_str)

    # 2. Fetch today's ESPN games (to get game IDs + matchup info)
    espn_games = fetch_scoreboard(today)
    logger.info(f"ESPN today: {len(espn_games)} games")

    # 3. Fetch today's lines from The Odds API
    try:
        odds_games = scrape_lines()
    except Exception as e:
        logger.error(f"Odds API failed: {e}. Continuing without lines.")
        odds_games = []

    # 4. Load model
    model = load_model(conn)
    if model is None:
        logger.warning("No model found — run 'python main.py train' first")

    # 5. Match ESPN games to odds, generate predictions
    predictions = []
    for espn_game in espn_games:
        # Only process scheduled games
        if espn_game["game_status"] not in ("scheduled", "in_progress"):
            continue

        home_name = espn_game["home_team"]
        away_name = espn_game["away_team"]
        espn_id = espn_game["espn_game_id"]

        # Ensure teams + game exist in DB
        home_id = get_team_id(conn, home_name)
        away_id = get_team_id(conn, away_name)

        if home_id is None:
            with conn:
                home_id = upsert_team(conn, home_name, espn_name=home_name,
                                      espn_id=espn_game.get("home_espn_id"))
        if away_id is None:
            with conn:
                away_id = upsert_team(conn, away_name, espn_name=away_name,
                                      espn_id=espn_game.get("away_espn_id"))

        # Upsert game
        with conn:
            game_id = upsert_game(conn, {
                "season":          SEASON_YEAR,
                "game_date":       espn_game["game_date"],
                "home_team_id":    home_id,
                "away_team_id":    away_id,
                "espn_game_id":    espn_id,
                "neutral_site":    1 if espn_game.get("neutral_site") else 0,
                "conference_game": 1 if espn_game.get("conference_game") else 0,
                "tournament_game": 1 if espn_game.get("tournament_game") else 0,
                "tournament_round": espn_game.get("tournament_round"),
            })

        # Find matching odds line
        odds = _match_odds(home_name, away_name, odds_games)

        # Save betting line if found
        if odds:
            _save_betting_line(conn, game_id, odds, today_str)

        # Generate prediction if model available
        prediction = None
        if model is not None:
            prediction = predict_game(
                conn, home_id, away_id,
                game_date=today_str,
                season=SEASON_YEAR,
                neutral_site=1 if espn_game.get("neutral_site") else 0,
                model=model,
            )
            if prediction:
                market_spread = (odds or {}).get("home_spread")
                spread_edge = None
                if market_spread is not None:
                    # edge = predicted_margin - market_implied_margin
                    # market implied home margin = -market_spread (e.g. home -4.5 → +4.5)
                    # so edge = predicted + market_spread
                    spread_edge = round(prediction["predicted_spread"] + market_spread, 2)
                _save_prediction(conn, game_id, prediction, market_spread, spread_edge, today_str)
                predictions.append({
                    "game":           f"{away_name} @ {home_name}",
                    "home_team":      home_name,
                    "away_team":      away_name,
                    "predicted_spread": prediction["predicted_spread"],
                    "market_spread":  market_spread,
                    "spread_edge":    spread_edge,
                    "home_win_prob":  prediction["predicted_home_win_prob"],
                    "home_spread_juice": (odds or {}).get("home_spread_juice"),
                    "away_spread_juice": (odds or {}).get("away_spread_juice"),
                })

        # Write daily snapshot
        _save_snapshot(conn, game_id, today_str, espn_game, odds, prediction)

    # 6. Print predictions
    _print_predictions(predictions)

    conn.close()


def _refresh_team_stats(conn, today_str: str):
    """Fetch any new game box scores from lacrosse-ref for the current season."""
    logger.info(f"Refreshing {SEASON_YEAR} game box scores from lacrossereference.com...")
    from jobs.historical_backfill import backfill_game_box_scores
    backfill_game_box_scores(conn, [SEASON_YEAR])


def _match_odds(home_name: str, away_name: str, odds_games: list[dict]) -> dict | None:
    """
    Match an ESPN game to an odds entry by fuzzy team name matching.
    Returns the odds dict or None.
    """
    home_lower = home_name.lower()
    away_lower = away_name.lower()

    for odds in odds_games:
        oh = (odds.get("home_team") or "").lower()
        oa = (odds.get("away_team") or "").lower()
        # Exact match first
        if oh == home_lower and oa == away_lower:
            return odds
        # Partial match (last word or first word)
        if _partial_name_match(home_lower, oh) and _partial_name_match(away_lower, oa):
            return odds

    return None


def _partial_name_match(name_a: str, name_b: str) -> bool:
    """Return True if the last word of name_a appears in name_b or vice versa."""
    words_a = name_a.split()
    words_b = name_b.split()
    if not words_a or not words_b:
        return False
    return words_a[-1] in name_b or words_b[-1] in name_a


def _save_betting_line(conn, game_id: int, odds: dict, today_str: str):
    """Save a betting line record."""
    h_ml = odds.get("home_moneyline")
    a_ml = odds.get("away_moneyline")
    home_nv = away_nv = vig = None
    h_imp = a_imp = None

    if h_ml is not None and a_ml is not None:
        h_imp = american_to_implied_prob(h_ml)
        a_imp = american_to_implied_prob(a_ml)
        home_nv, away_nv = remove_vig(h_imp, a_imp)
        vig = round((h_imp + a_imp - 1.0), 4)

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
                game_id, odds.get("scraped_at", today_str), odds.get("book"),
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


def _save_prediction(conn, game_id: int, prediction: dict, market_spread, spread_edge, today_str: str):
    """Save a model prediction record."""
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
                game_id, today_str, MODEL_VERSION,
                prediction["predicted_spread"],
                prediction["predicted_home_win_prob"],
                market_spread,
                spread_edge,
                json.dumps(prediction.get("features", {})),
            ),
        )


def _save_snapshot(conn, game_id: int, today_str: str, espn_game: dict, odds: dict | None, prediction: dict | None):
    """Write a daily_snapshots row for backtesting."""
    market_spread = (odds or {}).get("home_spread")
    with conn:
        conn.execute(
            """
            INSERT INTO daily_snapshots (
                snapshot_date, game_id, season,
                home_team, away_team, game_date, neutral_site,
                home_moneyline, away_moneyline,
                home_novig_prob, away_novig_prob,
                market_spread, model_version,
                predicted_spread, spread_edge, predicted_home_win_prob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, game_id) DO UPDATE SET
                home_moneyline = COALESCE(excluded.home_moneyline, home_moneyline),
                away_moneyline = COALESCE(excluded.away_moneyline, away_moneyline),
                market_spread = COALESCE(excluded.market_spread, market_spread),
                predicted_spread = COALESCE(excluded.predicted_spread, predicted_spread),
                spread_edge = COALESCE(excluded.spread_edge, spread_edge)
            """,
            (
                today_str, game_id, SEASON_YEAR,
                espn_game["home_team"], espn_game["away_team"],
                espn_game["game_date"],
                1 if espn_game.get("neutral_site") else 0,
                (odds or {}).get("home_moneyline"),
                (odds or {}).get("away_moneyline"),
                None,  # novig probs filled by separate processor
                None,
                market_spread,
                MODEL_VERSION if prediction else None,
                (prediction or {}).get("predicted_spread"),
                None,  # spread_edge: will be recomputed in results job
                (prediction or {}).get("predicted_home_win_prob"),
            ),
        )


def _print_predictions(predictions: list[dict]):
    if not predictions:
        print("\nNo predictions generated today (check stats coverage / model training).")
        return

    print(f"\n{'='*72}")
    print(f"  COLLEGE LACROSSE PREDICTIONS — {date.today()}")
    print(f"{'='*72}")
    print(f"  {'Matchup':<30} {'Pred':>6} {'Mkt':>6} {'Edge':>6} {'HWin%':>6} {'Juice':>6}")
    print(f"  {'-'*64}")

    flagged = []
    for p in sorted(predictions, key=lambda x: abs(x.get("spread_edge") or 0), reverse=True):
        edge = p.get("spread_edge")
        pred = p.get("predicted_spread")
        mkt = p.get("market_spread")
        game = p.get("game", "")[:29]
        hwp = p.get("home_win_prob", 0)

        pred_str = f"{pred:+.1f}" if pred is not None else "  N/A"
        mkt_str  = f"{mkt:+.1f}"  if mkt is not None else "  N/A"
        edge_str = f"{edge:+.1f}" if edge is not None else "  N/A"

        # Show juice for the side the model recommends
        side = "HOME" if (edge or 0) > 0 else "AWAY"
        juice_key = "home_spread_juice" if side == "HOME" else "away_spread_juice"
        juice = p.get(juice_key)
        juice_str = str(juice) if juice is not None else "  N/A"

        flag = " ***" if (edge is not None and abs(edge) >= SPREAD_THRESHOLD) else ""
        print(f"  {game:<30} {pred_str:>6} {mkt_str:>6} {edge_str:>6} {hwp*100:5.1f}%{flag} {juice_str:>6}")

        if flag:
            flagged.append((side, game, edge, juice))

    print()
    if flagged:
        import math
        print("  FLAGGED BETS (|edge| >= {:.1f}):".format(SPREAD_THRESHOLD))
        for side, game, edge, juice in flagged:
            juice_disp = str(juice) if juice is not None else "-110"
            j = juice if juice is not None else -110
            breakeven = abs(j) / (abs(j) + 100) * 100
            payout = round(100.0 / abs(j), 3)
            print(f"  BET: {side} {game}  edge={edge:+.1f}  juice={juice_disp}  "
                  f"breakeven={breakeven:.1f}%  win={payout:.3f}u/unit")
    print(f"{'='*72}\n")
