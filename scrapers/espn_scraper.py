"""
ESPN API scraper for men's college lacrosse.

Endpoints used:
  Scoreboard:  https://site.api.espn.com/apis/site/v2/sports/lacrosse/mens-college-lacrosse/scoreboard
  Summary:     https://site.api.espn.com/apis/site/v2/sports/lacrosse/mens-college-lacrosse/summary?event={id}
  Teams:       https://site.api.espn.com/apis/site/v2/sports/lacrosse/mens-college-lacrosse/teams

The scoreboard endpoint accepts a `dates` param (YYYYMMDD or YYYYMMDD-YYYYMMDD range).
The summary endpoint returns box score stats when available.
"""
import logging
import requests
import time
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import ESPN_SCOREBOARD_URL, ESPN_TEAMS_URL, SEASON_YEAR

logger = logging.getLogger(__name__)

ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/lacrosse/mens-college-lacrosse/summary"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


def fetch_scoreboard(game_date: date) -> list[dict]:
    """
    Fetch all games for a given date.

    Returns list of dicts:
        {
            "espn_game_id": str,
            "game_date": str,           # YYYY-MM-DD
            "home_team": str,
            "away_team": str,
            "home_espn_id": str,
            "away_espn_id": str,
            "home_score": int | None,
            "away_score": int | None,
            "game_status": str,         # "scheduled", "in_progress", "final"
            "neutral_site": bool,
            "conference_game": bool,
            "tournament_game": bool,
            "tournament_round": str | None,
        }
    """
    date_str = game_date.strftime("%Y%m%d")
    try:
        resp = _SESSION.get(
            ESPN_SCOREBOARD_URL,
            params={"dates": date_str, "limit": 100},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"ESPN scoreboard request failed for {date_str}: {e}")
        return []

    data = resp.json()
    events = data.get("events", [])
    games = []

    for event in events:
        try:
            games.append(_parse_event(event))
        except Exception as e:
            logger.warning(f"Error parsing ESPN event {event.get('id', '?')}: {e}")

    logger.info(f"ESPN scoreboard {date_str}: {len(games)} games")
    return games


def fetch_scoreboard_range(start: date, end: date) -> list[dict]:
    """Fetch all games between start and end dates (inclusive)."""
    all_games = []
    current = start
    while current <= end:
        games = fetch_scoreboard(current)
        all_games.extend(games)
        current += timedelta(days=1)
        time.sleep(0.3)  # be polite
    return all_games


def fetch_season_schedule(season: int) -> list[dict]:
    """
    Fetch the full schedule for a season.
    Men's college lacrosse runs roughly Feb 1 – May 31.
    Uses the ESPN date-range param to bulk-fetch rather than day-by-day.
    """
    start = date(season, 2, 1)
    end   = date(season, 5, 31)

    start_str = start.strftime("%Y%m%d")
    end_str   = end.strftime("%Y%m%d")

    try:
        resp = _SESSION.get(
            ESPN_SCOREBOARD_URL,
            params={"dates": f"{start_str}-{end_str}", "limit": 500},
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"ESPN season schedule request failed for {season}: {e}")
        # Fall back to day-by-day
        logger.info("Falling back to day-by-day fetch...")
        return fetch_scoreboard_range(start, end)

    data = resp.json()
    events = data.get("events", [])
    games = []
    for event in events:
        try:
            games.append(_parse_event(event))
        except Exception as e:
            logger.warning(f"Error parsing ESPN event {event.get('id', '?')}: {e}")

    # ESPN may paginate — check for multiple pages
    page_count = data.get("pageCount", 1)
    page_index = data.get("pageIndex", 1)
    if page_count > 1:
        logger.info(f"ESPN returned {page_count} pages for {season}; fetching remaining pages")
        for page in range(2, page_count + 1):
            try:
                resp2 = _SESSION.get(
                    ESPN_SCOREBOARD_URL,
                    params={"dates": f"{start_str}-{end_str}", "limit": 500, "page": page},
                    timeout=60,
                )
                resp2.raise_for_status()
                for event in resp2.json().get("events", []):
                    try:
                        games.append(_parse_event(event))
                    except Exception as e2:
                        logger.warning(f"Error parsing event on page {page}: {e2}")
                time.sleep(0.3)
            except requests.RequestException as e:
                logger.warning(f"ESPN pagination request failed (page {page}): {e}")

    logger.info(f"ESPN: fetched {len(games)} games for {season} season")
    return games


def fetch_game_boxscore(espn_game_id: str) -> dict | None:
    """
    Fetch box score stats for a single completed game.
    Returns a dict with per-team stats keyed by 'home' and 'away', or None if unavailable.

    Box score stat categories vary — we extract what's available and fill None for missing.
    """
    try:
        resp = _SESSION.get(
            ESPN_SUMMARY_URL,
            params={"event": espn_game_id},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"ESPN summary request failed for game {espn_game_id}: {e}")
        return None

    data = resp.json()
    return _parse_boxscore(data, espn_game_id)


def fetch_teams() -> list[dict]:
    """
    Fetch all D1 men's lacrosse teams from ESPN.
    Returns list of dicts: {espn_id, name, abbreviation, location}
    """
    teams = []
    try:
        resp = _SESSION.get(ESPN_TEAMS_URL, params={"limit": 500}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for entry in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
            t = entry.get("team", {})
            teams.append({
                "espn_id":      t.get("id"),
                "name":         t.get("displayName") or t.get("name"),
                "abbreviation": t.get("abbreviation"),
                "location":     t.get("location"),
            })
    except Exception as e:
        logger.error(f"ESPN teams fetch failed: {e}")
    logger.info(f"ESPN: fetched {len(teams)} teams")
    return teams


# ---------------------------------------------------------------------------
# Internal parsers
# ---------------------------------------------------------------------------

def _parse_event(event: dict) -> dict:
    """Parse an ESPN event dict into our normalized game dict."""
    competitions = event.get("competitions", [{}])
    comp = competitions[0] if competitions else {}
    competitors = comp.get("competitors", [])

    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})

    home_team_info = home.get("team", {})
    away_team_info = away.get("team", {})

    # Score
    home_score = _safe_int(home.get("score"))
    away_score = _safe_int(away.get("score"))

    # Status
    status_obj = event.get("status", {}).get("type", {})
    status_name = status_obj.get("name", "STATUS_UNKNOWN").lower()
    if "final" in status_name or "complete" in status_name:
        status = "final"
    elif "in" in status_name or "progress" in status_name:
        status = "in_progress"
    else:
        status = "scheduled"

    # Tournament / notes
    notes = comp.get("notes", [])
    note_text = notes[0].get("headline", "") if notes else ""
    tournament_game = any(
        kw in note_text.lower()
        for kw in ["ncaa", "tournament", "championship", "playoff"]
    )
    tournament_round = note_text if tournament_game else None

    # Game date
    start_date = event.get("date", "")[:10]  # YYYY-MM-DD

    return {
        "espn_game_id":   event.get("id"),
        "game_date":      start_date,
        "home_team":      home_team_info.get("displayName") or home_team_info.get("name", ""),
        "away_team":      away_team_info.get("displayName") or away_team_info.get("name", ""),
        "home_espn_id":   home_team_info.get("id"),
        "away_espn_id":   away_team_info.get("id"),
        "home_score":     home_score,
        "away_score":     away_score,
        "game_status":    status,
        "neutral_site":   comp.get("neutralSite", False),
        "conference_game": comp.get("conferenceCompetition", False),
        "tournament_game": tournament_game,
        "tournament_round": tournament_round,
    }


def _parse_boxscore(data: dict, espn_game_id: str) -> dict | None:
    """
    Parse the ESPN summary response for box score stats.
    Returns dict with keys 'home' and 'away', each a stat dict, or None.
    """
    boxscore = data.get("boxscore", {})
    players_data = boxscore.get("players", [])

    # ESPN lacrosse box scores may not always include detailed stats
    if not players_data:
        logger.debug(f"No box score players data for game {espn_game_id}")
        return None

    result = {}
    for team_data in boxscore.get("teams", []):
        home_away = team_data.get("homeAway", "home")
        stats_list = team_data.get("statistics", [])
        stats = {s.get("name"): s.get("displayValue") for s in stats_list}
        result[home_away] = _normalize_team_stats(stats)

    if not result:
        return None

    return result


def _normalize_team_stats(stats: dict) -> dict:
    """
    Normalize ESPN stat keys to our internal field names.
    ESPN stat names vary by sport/season — add mappings as discovered.
    """
    def _si(key):
        v = stats.get(key)
        return int(v) if v is not None and str(v).isdigit() else None

    def _sf(key):
        try:
            return float(stats.get(key, 0) or 0)
        except (ValueError, TypeError):
            return None

    def _parse_fraction(key):
        """Parse 'X/Y' stat like faceoffs '12/20'."""
        v = stats.get(key, "")
        if v and "/" in str(v):
            parts = str(v).split("/")
            try:
                return int(parts[0]), int(parts[1])
            except ValueError:
                pass
        return None, None

    goals = _si("goals") or _si("Goals")
    shots = _si("shots") or _si("Shots")
    sog = _si("shotsOnGoal") or _si("Shots on Goal") or _si("SOG")

    fo_made, fo_taken = _parse_fraction("faceoffs") or _parse_fraction("Faceoffs")
    fo_pct = (fo_made / fo_taken) if (fo_made and fo_taken) else None

    return {
        "goals":            goals,
        "shots":            shots,
        "shots_on_goal":    sog,
        "shot_pct":         (goals / shots) if (goals is not None and shots) else None,
        "sog_pct":          (goals / sog) if (goals is not None and sog) else None,
        "faceoffs_won":     fo_made,
        "faceoffs_taken":   fo_taken,
        "fo_pct":           fo_pct,
        "turnovers":        _si("turnovers") or _si("Turnovers"),
        "caused_turnovers": _si("causedTurnovers") or _si("Caused Turnovers"),
        "ground_balls":     _si("groundBalls") or _si("Ground Balls"),
        "saves":            _si("saves") or _si("Saves"),
    }


def _safe_int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
