"""
Bet tracker: log bets placed, score results, report P&L.

Usage:
    python main.py log-bet                          # interactive prompt
    python main.py tracker                          # full P&L report
    python main.py tracker --season 2026            # filter by season
    python main.py settle                           # auto-settle pending bets with known results
"""
import sys
import logging
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import get_db, init_db

logger = logging.getLogger(__name__)


def log_bet_interactive():
    """
    Interactively log a bet. Prompts for game + bet details, saves to bets table.
    """
    init_db()
    conn = get_db()

    print("\n--- Log a Bet ---")
    print("(Press Ctrl+C to cancel)\n")

    try:
        # Show today's predictions to help pick a game
        today_str = date.today().isoformat()
        picks = conn.execute(
            """
            SELECT ds.game_id, ds.home_team, ds.away_team, ds.game_date,
                   ds.market_spread, ds.predicted_spread, ds.spread_edge,
                   CASE WHEN ds.spread_edge >= 0 THEN 'HOME' ELSE 'AWAY' END AS model_side,
                   bl.home_spread_juice, bl.away_spread_juice
            FROM daily_snapshots ds
            LEFT JOIN betting_lines bl ON bl.game_id = ds.game_id
            WHERE ds.snapshot_date = ?
              AND ds.spread_edge IS NOT NULL
              AND ds.home_score IS NULL
            ORDER BY ABS(ds.spread_edge) DESC
            """,
            (today_str,),
        ).fetchall()

        if picks:
            print(f"Today's open picks ({today_str}):")
            for i, p in enumerate(picks):
                side = p["model_side"]
                juice_col = "home_spread_juice" if side == "HOME" else "away_spread_juice"
                juice = p[juice_col] or -110
                print(
                    f"  [{i+1}] {p['away_team']} @ {p['home_team']}  "
                    f"BET {side}  spread={p['market_spread']:+.1f}  "
                    f"edge={p['spread_edge']:+.1f}  juice={juice}"
                )
            print()
            choice = input("Select pick number (or 0 to enter manually): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(picks):
                p = picks[int(choice) - 1]
                game_id = p["game_id"]
                game_date = p["game_date"]
                home_team = p["home_team"]
                away_team = p["away_team"]
                model_side = p["model_side"].lower()
                market_spread = p["market_spread"]
                predicted_spread = p["predicted_spread"]
                spread_edge = p["spread_edge"]
                juice_col = "home_spread_juice" if model_side == "home" else "away_spread_juice"
                suggested_juice = p[juice_col] or -110
                juice_input = input(f"Juice [{suggested_juice}]: ").strip()
                juice = int(juice_input) if juice_input else suggested_juice
                units_input = input("Units [1.0]: ").strip()
                units = float(units_input) if units_input else 1.0
                notes_input = input("Notes (optional): ").strip()
                notes = notes_input or None
                _insert_bet(conn, {
                    "bet_date": today_str, "game_id": game_id, "game_date": game_date,
                    "home_team": home_team, "away_team": away_team,
                    "model_side": model_side, "market_spread": market_spread,
                    "predicted_spread": predicted_spread, "spread_edge": spread_edge,
                    "juice": juice, "units": units, "notes": notes,
                })
                side_label = f"{'HOME ' + home_team if model_side == 'home' else 'AWAY ' + away_team}"
                payout = units * (100.0 / abs(juice))
                print(f"\n✓ Logged: BET {side_label}  {units}u @ {juice}  "
                      f"(win={payout:.2f}u / lose={units:.2f}u)")
                conn.close()
                return

        # Manual entry fallback
        game_date = input(f"Game date [{today_str}]: ").strip() or today_str
        home_team = input("Home team: ").strip()
        away_team = input("Away team: ").strip()
        model_side = input("Side (home/away): ").strip().lower()
        market_spread = float(input("Market spread (home spread, e.g. +10.5 or -4.5): ").strip())
        predicted_spread = float(input("Predicted spread: ").strip())
        spread_edge = predicted_spread + market_spread
        juice = int(input("Juice on our side (e.g. -140): ").strip())
        units_input = input("Units [1.0]: ").strip()
        units = float(units_input) if units_input else 1.0
        notes_input = input("Notes (optional): ").strip()
        notes = notes_input or None

        # Try to find game_id
        row = conn.execute(
            """
            SELECT id FROM games
            WHERE game_date = ?
              AND home_team_id = (SELECT id FROM teams WHERE canonical_name = ?)
              AND away_team_id = (SELECT id FROM teams WHERE canonical_name = ?)
            """,
            (game_date, home_team, away_team),
        ).fetchone()
        game_id = row["id"] if row else None

        _insert_bet(conn, {
            "bet_date": today_str, "game_id": game_id, "game_date": game_date,
            "home_team": home_team, "away_team": away_team,
            "model_side": model_side, "market_spread": market_spread,
            "predicted_spread": predicted_spread, "spread_edge": spread_edge,
            "juice": juice, "units": units, "notes": notes,
        })
        print(f"\n✓ Logged bet: BET {model_side.upper()} {home_team if model_side == 'home' else away_team}")

    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        conn.close()


def _insert_bet(conn, bet: dict):
    with conn:
        conn.execute(
            """
            INSERT INTO bets (
                bet_date, game_id, game_date, home_team, away_team,
                model_side, market_spread, predicted_spread, spread_edge,
                juice, units, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bet["bet_date"], bet.get("game_id"), bet["game_date"],
                bet["home_team"], bet["away_team"], bet["model_side"],
                bet.get("market_spread"), bet.get("predicted_spread"), bet.get("spread_edge"),
                bet["juice"], bet.get("units", 1.0), bet.get("notes"),
            ),
        )


def settle_pending():
    """
    Auto-settle pending bets by looking up actual results in the DB.
    Run after results_job has populated scores.
    """
    init_db()
    conn = get_db()

    pending = conn.execute(
        "SELECT * FROM bets WHERE result = 'pending'"
    ).fetchall()

    if not pending:
        print("No pending bets to settle.")
        conn.close()
        return

    settled = 0
    for bet in pending:
        game_id = bet["game_id"]
        if not game_id:
            continue

        result_row = conn.execute(
            "SELECT home_score, away_score, actual_margin FROM results WHERE game_id = ?",
            (game_id,),
        ).fetchone()

        if not result_row or result_row["actual_margin"] is None:
            continue

        actual_margin = result_row["actual_margin"]
        market_spread = bet["market_spread"]
        model_side = bet["model_side"]
        juice = bet["juice"]
        units = bet["units"]

        # Determine cover result
        result = "pending"
        pnl = None

        if market_spread is not None:
            # home covers if actual_margin + market_spread > 0
            home_covered = (actual_margin + market_spread) > 0
            push = (actual_margin + market_spread) == 0

            if push:
                result = "push"
                pnl = 0.0
            elif (model_side == "home" and home_covered) or (model_side == "away" and not home_covered):
                result = "win"
                pnl = round(units * (100.0 / abs(juice)), 3)
            else:
                result = "loss"
                pnl = -units

        with conn:
            conn.execute(
                """
                UPDATE bets
                SET result = ?, actual_margin = ?, pnl = ?
                WHERE id = ?
                """,
                (result, actual_margin, pnl, bet["id"]),
            )
        settled += 1
        game_str = f"{bet['away_team']} @ {bet['home_team']}"
        pnl_str = f"{pnl:+.2f}u" if pnl is not None else "---"
        print(f"  Settled: {game_str}  → {result.upper()}  {pnl_str}")

    print(f"\nSettled {settled}/{len(pending)} pending bets.")
    conn.close()


def print_tracker(season: int = None):
    """
    Print full bet log and P&L summary.
    """
    init_db()
    conn = get_db()

    query = "SELECT * FROM bets"
    params = []
    if season:
        query += " WHERE SUBSTR(game_date, 1, 4) = ?"
        params.append(str(season))
    query += " ORDER BY game_date, id"

    bets = conn.execute(query, params).fetchall()
    conn.close()

    if not bets:
        msg = f"No bets logged" + (f" for {season}" if season else "")
        print(msg + ". Use 'python main.py log-bet' to record bets.")
        return

    print(f"\n{'='*78}")
    print(f"  BET TRACKER{' — ' + str(season) if season else ''}")
    print(f"{'='*78}")
    print(f"  {'Date':<11} {'Matchup':<28} {'Side':<5} {'Sprd':>5} {'Edge':>5} {'Juice':>5} {'Units':>5} {'Result':<7} {'P&L':>6}")
    print(f"  {'-'*73}")

    total_units = total_pnl = wins = losses = pushes = pending_count = 0
    total_wagered = 0.0

    for b in bets:
        game = f"{b['away_team']} @ {b['home_team']}"[:27]
        side = b["model_side"].upper()[:4]
        sprd = f"{b['market_spread']:+.1f}" if b["market_spread"] is not None else "  N/A"
        edge = f"{b['spread_edge']:+.1f}" if b["spread_edge"] is not None else "  N/A"
        juice_str = str(b["juice"])
        units_str = f"{b['units']:.1f}"
        result = b["result"] or "pending"
        pnl = b["pnl"]
        pnl_str = f"{pnl:+.2f}" if pnl is not None else "  ---"

        print(f"  {b['bet_date']:<11} {game:<28} {side:<5} {sprd:>5} {edge:>5} "
              f"{juice_str:>5} {units_str:>5} {result:<7} {pnl_str:>6}")

        total_units += b["units"]
        total_wagered += b["units"]
        if result == "win":
            wins += 1
            total_pnl += pnl
        elif result == "loss":
            losses += 1
            total_pnl += pnl
        elif result == "push":
            pushes += 1
        else:
            pending_count += 1

    settled = wins + losses
    win_pct = (wins / settled * 100) if settled > 0 else 0
    roi = (total_pnl / total_wagered * 100) if total_wagered > 0 else 0

    print(f"\n  {'─'*60}")
    print(f"  Bets: {len(bets)} total  ({wins}W-{losses}L-{pushes}P, {pending_count} pending)")
    if settled > 0:
        print(f"  Win rate: {wins}/{settled} ({win_pct:.1f}%)")
    print(f"  P&L: {total_pnl:+.2f}u  |  ROI: {roi:+.1f}%  |  Wagered: {total_wagered:.1f}u")

    # Breakeven reminder by juice distribution
    if bets:
        juices = [b["juice"] for b in bets if b["result"] != "pending"]
        if juices:
            avg_juice = sum(juices) / len(juices)
            breakeven = abs(avg_juice) / (abs(avg_juice) + 100) * 100
            print(f"  Avg juice: {avg_juice:.0f}  |  Breakeven at avg juice: {breakeven:.1f}%")

    print(f"{'='*78}\n")
