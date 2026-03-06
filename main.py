"""
Entry point for all college lacrosse betting model jobs.

Usage:
    python main.py init            Initialize DB only
    python main.py probe           Probe lacrossereference.com site structure (do this first!)
    python main.py backfill        Backfill all historical seasons (ESPN games + LR box scores)
    python main.py backfill --season 2024
    python main.py backfill --games-only   ESPN games/results only (skip box scores)
    python main.py backfill --box-only     Box scores only (skip ESPN games fetch)
    python main.py fix-slugs               Reset LR slug mappings and re-backfill box scores
    python main.py train           Train/retrain the spread prediction model
    python main.py results         Pull prior-day results + score predictions
    python main.py daily           Scrape today's lines + generate predictions
    python main.py both            results + daily (typical morning run)
    python main.py backfill-odds           Backfill historical odds for VAL_SEASONS (requires paid Odds API)
    python main.py backfill-odds --season 2025
    python main.py evaluate        Print ATS performance summary
    python main.py check-sports    List available sports on The Odds API
    python main.py log-bet         Interactively log a bet (shows today's picks, prompts for details)
    python main.py tracker         Print full P&L report for all logged bets
    python main.py tracker --season 2026   Filter report by season
    python main.py settle          Auto-settle pending bets using actual game results
"""
import sys
import logging
import argparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("main")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "init":
        from db.db import init_db
        init_db()
        logger.info("Database initialized.")

    elif command == "probe":
        from jobs.historical_backfill import probe_lacrosse_ref
        season = int(sys.argv[2]) if len(sys.argv) > 2 else 2024
        probe_lacrosse_ref(season)

    elif command == "backfill":
        from jobs.historical_backfill import run
        parser = argparse.ArgumentParser()
        parser.add_argument("--season", type=int, default=None)
        parser.add_argument("--games-only", action="store_true")
        parser.add_argument("--box-only", action="store_true")
        args = parser.parse_args(sys.argv[2:])
        seasons = [args.season] if args.season else None
        run(seasons=seasons, games_only=args.games_only, box_only=args.box_only)

    elif command == "fix-slugs":
        # Reset all LR slug mappings and re-run with corrected name overrides.
        # Use this after updating _LR_ESPN_OVERRIDES in historical_backfill.py.
        from jobs.historical_backfill import fix_lr_slugs
        parser = argparse.ArgumentParser()
        parser.add_argument("--season", type=int, default=None)
        args = parser.parse_args(sys.argv[2:])
        seasons = [args.season] if args.season else None
        fix_lr_slugs(seasons=seasons)

    elif command == "train":
        from db.db import get_db
        from processors.model import train
        conn = get_db()
        model = train(conn)
        conn.close()

    elif command == "results":
        from jobs.results_job import run
        run()

    elif command == "daily":
        from jobs.daily_job import run
        run()

    elif command == "both":
        from jobs.results_job import run as results_run
        from jobs.daily_job import run as daily_run
        results_run()
        daily_run()

    elif command == "backfill-odds":
        from jobs.odds_backfill import run as odds_run
        parser = argparse.ArgumentParser()
        parser.add_argument("--season", type=int, default=None)
        parser.add_argument("--seasons", type=int, nargs="+", default=None)
        args = parser.parse_args(sys.argv[2:])
        if args.season:
            seasons = [args.season]
        elif args.seasons:
            seasons = args.seasons
        else:
            seasons = None
        odds_run(seasons=seasons)

    elif command == "evaluate":
        from db.db import get_db
        from processors.model import evaluate_ats_performance
        from config import SPREAD_THRESHOLD
        conn = get_db()
        evaluate_ats_performance(conn, min_edge=SPREAD_THRESHOLD)
        conn.close()

    elif command == "check-sports":
        from scrapers.odds_api import get_available_sports
        sports = get_available_sports()
        lacrosse = [s for s in sports if "lacrosse" in str(s).lower()]
        if lacrosse:
            print("Lacrosse sports found:")
            for s in lacrosse:
                print(f"  {s}")
        else:
            print("No lacrosse sports found in Odds API. Available sports:")
            for s in sports[:30]:
                print(f"  {s.get('key', '?')}: {s.get('title', '?')}")

    elif command == "log-bet":
        from jobs.bet_tracker import log_bet_interactive
        log_bet_interactive()

    elif command == "tracker":
        from jobs.bet_tracker import print_tracker
        parser = argparse.ArgumentParser()
        parser.add_argument("--season", type=int, default=None)
        args = parser.parse_args(sys.argv[2:])
        print_tracker(season=args.season)

    elif command == "settle":
        from jobs.bet_tracker import settle_pending
        settle_pending()

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
