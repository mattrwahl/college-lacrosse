"""
Fetch college lacrosse moneylines and spreads from The Odds API.
https://the-odds-api.com

Sport key: lacrosse_ncaa (verify with get_available_sports() if not returning data)
Free tier: 500 requests/month

Markets fetched:
  h2h     — moneyline
  spreads — point spread (needed for ATS model)
"""
import logging
import requests
from datetime import datetime, date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import ODDS_API_KEY, ODDS_API_SPORT, PREFERRED_BOOKS, ODDS_MARKETS

logger = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def scrape_lines() -> list[dict]:
    """
    Fetch today's college lacrosse lines (moneyline + spread) from The Odds API.

    Returns list of dicts:
        {
            "home_team": str,
            "away_team": str,
            "home_moneyline": int | None,
            "away_moneyline": int | None,
            "home_spread": float | None,       # e.g. -4.5 means home favored by 4.5
            "away_spread": float | None,
            "home_spread_juice": int | None,
            "away_spread_juice": int | None,
            "book": str,
            "game_date": str,                  # YYYY-MM-DD
            "commence_time": str,
            "scraped_at": str,
        }
    """
    if not ODDS_API_KEY:
        raise ValueError("ODDS_API_KEY not set. Add it to .env.")

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": ",".join(ODDS_MARKETS),
        "oddsFormat": "american",
        "bookmakers": ",".join(PREFERRED_BOOKS),
    }

    logger.info(f"Fetching college lacrosse odds from The Odds API ({ODDS_API_SPORT})")
    try:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/{ODDS_API_SPORT}/odds",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Odds API request failed: {e}")
        raise

    remaining = resp.headers.get("x-requests-remaining", "?")
    used = resp.headers.get("x-requests-used", "?")
    logger.info(f"Odds API quota: {used} used, {remaining} remaining")

    events = resp.json()
    games = []
    scraped_at = datetime.now().isoformat()

    for event in events:
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        commence_time = event.get("commence_time", "")

        try:
            game_date = commence_time[:10]
        except Exception:
            game_date = date.today().isoformat()

        bookmakers = event.get("bookmakers", [])
        by_book = {b["key"]: b for b in bookmakers}

        home_ml, away_ml, home_spread, away_spread, h_juice, a_juice, book_used = (
            None, None, None, None, None, None, ""
        )

        for book_key in PREFERRED_BOOKS:
            if book_key not in by_book:
                continue
            result = _extract_lines(by_book[book_key], home_team, away_team)
            if result:
                home_ml, away_ml, home_spread, away_spread, h_juice, a_juice = result
                book_used = book_key
                break

        if not book_used:
            for b in bookmakers:
                result = _extract_lines(b, home_team, away_team)
                if result:
                    home_ml, away_ml, home_spread, away_spread, h_juice, a_juice = result
                    book_used = b["key"]
                    break

        if home_ml is None and home_spread is None:
            logger.debug(f"No lines found for {away_team} @ {home_team}")
            continue

        games.append({
            "home_team": home_team,
            "away_team": away_team,
            "home_moneyline": home_ml,
            "away_moneyline": away_ml,
            "home_spread": home_spread,
            "away_spread": away_spread,
            "home_spread_juice": h_juice,
            "away_spread_juice": a_juice,
            "book": book_used,
            "game_date": game_date,
            "commence_time": commence_time,
            "scraped_at": scraped_at,
        })

    logger.info(f"Fetched {len(games)} college lacrosse games from Odds API")
    return games


def _extract_lines(
    bookmaker: dict, home_team: str, away_team: str
) -> tuple | None:
    """
    Extract moneyline and spread from a bookmaker dict.
    Returns (home_ml, away_ml, home_spread, away_spread, home_juice, away_juice) or None.
    """
    markets_by_key = {m["key"]: m for m in bookmaker.get("markets", [])}

    home_ml = away_ml = None
    if "h2h" in markets_by_key:
        outcomes = {o["name"]: int(o["price"]) for o in markets_by_key["h2h"].get("outcomes", [])}
        home_ml = outcomes.get(home_team)
        away_ml = outcomes.get(away_team)

    home_spread = away_spread = home_juice = away_juice = None
    if "spreads" in markets_by_key:
        for o in markets_by_key["spreads"].get("outcomes", []):
            if o["name"] == home_team:
                home_spread = float(o.get("point", 0))
                home_juice = int(o.get("price", -110))
            elif o["name"] == away_team:
                away_spread = float(o.get("point", 0))
                away_juice = int(o.get("price", -110))

    if home_ml is None and home_spread is None:
        return None

    return home_ml, away_ml, home_spread, away_spread, home_juice, away_juice


def get_available_sports() -> list[dict]:
    """List all sports available on The Odds API. Use to confirm lacrosse sport key."""
    if not ODDS_API_KEY:
        raise ValueError("ODDS_API_KEY not set")
    resp = requests.get(
        f"{ODDS_API_BASE}/sports",
        params={"apiKey": ODDS_API_KEY, "all": True},
        timeout=15,
    )
    resp.raise_for_status()
    sports = resp.json()
    lacrosse = [s for s in sports if "lacrosse" in s.get("key", "").lower()
                or "lacrosse" in s.get("title", "").lower()]
    logger.info(f"Lacrosse sports found: {lacrosse}")
    return sports
