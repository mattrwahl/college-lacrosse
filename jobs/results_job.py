"""
Results job: pull yesterday's final scores and update DB.

Run each morning before the daily job to score the prior day's predictions.
Updates results, daily_snapshots (home_covered, away_covered), and prints ATS summary.

Usage:
    python main.py results
"""
import sys
import logging
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import get_db, upsert_result
from scrapers.espn_scraper import fetch_scoreboard

logger = logging.getLogger(__name__)


def run(target_date: date = None):
    """
    Fetch results for target_date (default: yesterday) and update DB.
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    conn = get_db()
    date_str = target_date.isoformat()
    logger.info(f"Fetching results for {date_str}...")

    games = fetch_scoreboard(target_date)
    final_games = [g for g in games if g["game_status"] == "final"]
    logger.info(f"Found {len(final_games)} final games on {date_str}")

    updated = 0
    for game in final_games:
        espn_id = game.get("espn_game_id")
        if not espn_id:
            continue

        row = conn.execute(
            "SELECT id, home_team_id, away_team_id FROM games WHERE espn_game_id = ?",
            (espn_id,),
        ).fetchone()

        if row is None:
            continue

        game_id = row["id"]
        home_id = row["home_team_id"]
        away_id = row["away_team_id"]
        home_score = game.get("home_score")
        away_score = game.get("away_score")

        if home_score is None or away_score is None:
            continue

        winner_id = home_id if home_score > away_score else away_id

        with conn:
            upsert_result(conn, game_id, {
                "home_score": home_score,
                "away_score": away_score,
                "winner_team_id": winner_id,
                "game_status": "final",
            })

            # Update daily_snapshots with ATS outcome
            _update_snapshot_ats(conn, game_id, home_score, away_score, date_str)

        updated += 1

    logger.info(f"Updated results for {updated} games on {date_str}")

    # Print ATS summary for the day
    _print_daily_ats_summary(conn, date_str)
    conn.close()


def _update_snapshot_ats(conn, game_id: int, home_score: int, away_score: int, date_str: str):
    """
    Update the daily_snapshots row with ATS coverage result.
    home_covered = 1 if home margin > market spread (home wins by more than spread)
    """
    actual_margin = home_score - away_score

    # Get market spread from snapshot
    snap = conn.execute(
        """
        SELECT id, market_spread, spread_edge
        FROM daily_snapshots
        WHERE game_id = ? AND snapshot_date = ?
        """,
        (game_id, date_str),
    ).fetchone()

    if snap is None:
        # Try to find any snapshot for this game
        snap = conn.execute(
            "SELECT id, market_spread, spread_edge FROM daily_snapshots WHERE game_id = ? ORDER BY snapshot_date DESC LIMIT 1",
            (game_id,),
        ).fetchone()

    if snap is None:
        return

    market_spread = snap["market_spread"]
    home_covered = None
    away_covered = None

    if market_spread is not None:
        # market_spread is the home spread (e.g. -4.5 means home favored by 4.5)
        # home covers if actual_margin > -market_spread ... convention:
        # home_spread = -4.5 means home must win by >4.5 to cover
        # home covers if actual_margin + home_spread > 0
        home_covered = 1 if (actual_margin + market_spread) > 0 else 0
        away_covered = 1 - home_covered

    # Recompute spread_edge from stored prediction
    predicted_spread = conn.execute(
        "SELECT predicted_spread FROM predictions WHERE game_id = ? ORDER BY generated_at DESC LIMIT 1",
        (game_id,),
    ).fetchone()
    spread_edge = None
    if predicted_spread and market_spread is not None:
        spread_edge = round(predicted_spread["predicted_spread"] + market_spread, 2)

    conn.execute(
        """
        UPDATE daily_snapshots
        SET home_score = ?,
            away_score = ?,
            actual_margin = ?,
            result_home_win = ?,
            home_covered = ?,
            away_covered = ?,
            spread_edge = COALESCE(?, spread_edge)
        WHERE game_id = ?
        """,
        (
            home_score, away_score, actual_margin,
            1 if actual_margin > 0 else 0,
            home_covered, away_covered,
            spread_edge,
            game_id,
        ),
    )


def _print_daily_ats_summary(conn, date_str: str):
    """Print ATS results for yesterday's flagged predictions."""
    from config import SPREAD_THRESHOLD

    rows = conn.execute(
        """
        SELECT
            home_team, away_team,
            market_spread, predicted_spread, spread_edge,
            home_score, away_score, actual_margin,
            home_covered, away_covered
        FROM daily_snapshots
        WHERE snapshot_date = ?
          AND spread_edge IS NOT NULL
          AND home_score IS NOT NULL
        ORDER BY ABS(spread_edge) DESC
        """,
        (date_str,),
    ).fetchall()

    if not rows:
        return

    print(f"\n{'='*65}")
    print(f"  ATS RESULTS — {date_str}")
    print(f"{'='*65}")
    print(f"  {'Matchup':<28} {'Mkt':>5} {'Pred':>5} {'Edge':>5} {'Score':>7} {'Cvrd':>4}")
    print(f"  {'-'*57}")

    for r in rows:
        game = f"{r['away_team']} @ {r['home_team']}"[:27]
        mkt = f"{r['market_spread']:+.1f}" if r['market_spread'] else "  N/A"
        pred = f"{r['predicted_spread']:+.1f}" if r['predicted_spread'] else "  N/A"
        edge = f"{r['spread_edge']:+.1f}" if r['spread_edge'] else "  N/A"
        score = f"{r['away_score']}-{r['home_score']}" if r['home_score'] is not None else "  N/A"
        edge_val = r['spread_edge'] or 0
        model_side = "home" if edge_val > 0 else "away"
        covered = r['home_covered'] if model_side == "home" else r['away_covered']
        cvrd = "WIN" if covered == 1 else ("LOSS" if covered == 0 else " ---")
        flag = " ***" if abs(edge_val) >= SPREAD_THRESHOLD else ""
        print(f"  {game:<28} {mkt:>5} {pred:>5} {edge:>5} {score:>7} {cvrd:>4}{flag}")

    print()
