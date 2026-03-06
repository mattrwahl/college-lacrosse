"""
Scraper for pro.lacrossereference.com — per-game box score data.

Data flow:
  1. fetch_d1_men_teams()
       Hits lacrossereference.com/stats/adj-efficiency-d1-men
       Returns 77 D1 men teams with their LR team ID (a0019) and pro slug (dukem-5269)

  2. fetch_team_game_slugs(pro_slug, season)
       Hits pro.lacrossereference.com/{pro_slug}?view=games&year={season}
       Returns list of game slugs for that team/season

  3. fetch_game_box_score(game_slug)
       Hits pro.lacrossereference.com/{game_slug}
       Returns full box score: shots, SOG, goals, assists, possessions,
       turnovers, ground balls, faceoff wins, saves, time-of-possession

Box score fields (from BasicSummaryCounting):
  shots        — total shots
  sog          — shots on goal
  goals        — goals scored
  assists      — assists
  possessions  — possession count (true count, not proxy!)
  turnovers    — turnovers committed
  gbs          — ground balls won
  faceoffs     — faceoff wins (taken = home+away wins total)
  saves        — goalie saves
  top          — time of possession (0.0–1.0 fraction)

Data availability:
  - Game slugs: all seasons back to ~2018 (varies by team)
  - Box score data: all seasons with slugs (server-rendered, no JS required)
  - Game slug format: game-{team1}-vs-{team2}-mlax-{year}-{alphanum_id}
  - IDs are alphanumeric (e.g. 6h86, not just digits)

Notes:
  - The title field "Duke MLAX vs Jacksonville" follows home vs away convention
  - home_ID and away_ID map to internal LR team numeric IDs
  - No Playwright required — data is embedded in initial HTML response
"""
import logging
import requests
import json
import re
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

BASE_URL  = "https://lacrossereference.com"
PRO_URL   = "https://pro.lacrossereference.com"

# Regex for game slugs — IDs are alphanumeric (NOT purely numeric)
# Team names in slugs may contain hyphens (e.g. "notre-dame", "penn-state", "north-carolina")
_GAME_SLUG_RE = re.compile(r'game-[a-z-]+-vs-[a-z-]+-mlax-\d{4}-[a-z0-9]+')

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})

_POLITE_DELAY = 0.4  # seconds between requests


def fetch_d1_men_teams() -> list[dict]:
    """
    Fetch all 77 D1 men teams from the adj-efficiency stats page.
    For each team, also fetches their team page to resolve the pro site slug.

    Returns list of dicts:
        {
            "name": str,           # canonical display name (e.g. "Duke")
            "lr_id": str,          # regular site team ID (e.g. "a0019")
            "lr_url": str,         # regular site team URL
            "pro_slug": str|None,  # pro site slug (e.g. "dukem-5269")
        }
    """
    url = f"{BASE_URL}/stats/adj-efficiency-d1-men"
    try:
        resp = _SESSION.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch D1 team list: {e}")
        return []

    td_match = re.search(r"var td\s*=\s*(\{.*?\})\s*;", resp.text, re.DOTALL)
    if not td_match:
        logger.error("Could not find team data JS variable on efficiency page")
        return []

    try:
        data = json.loads(td_match.group(1))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse team data JSON: {e}")
        return []

    teams = []
    for row in data.get("data", []):
        link_val = row.get("link", {}).get("val", "")
        href_match = re.search(r'href="([^"]+)"', link_val)
        name = row.get("display_name") or row.get("display_name", {}).get("val", "?")
        if isinstance(name, dict):
            name = name.get("val", "?")
        if href_match:
            team_url = href_match.group(1)
            lr_id = team_url.rstrip("/").split("/")[-1]
            teams.append({
                "name": name,
                "lr_id": lr_id,
                "lr_url": team_url,
                "pro_slug": None,
            })

    logger.info(f"Found {len(teams)} D1 men teams on lacrossereference.com")

    # Resolve pro slugs — fetch each team page
    resolved = 0
    for i, team in enumerate(teams):
        try:
            r = _SESSION.get(team["lr_url"], timeout=15)
            r.raise_for_status()
            # Pro team slug format: teamnamem-DDDD (men's suffix)
            # Team names may contain hyphens (e.g. "notre-damem-1853", "penn-statem-XXXX")
            schedule_links = re.findall(
                r"https://pro\.lacrossereference\.com/([a-z][a-z-]*m-\d{4})\?view=games",
                r.text,
            )
            if schedule_links:
                team["pro_slug"] = schedule_links[0]
                resolved += 1
            else:
                # Broader fallback: any *m-DDDD link on the page
                broader = re.findall(
                    r"https://pro\.lacrossereference\.com/([a-z][a-z-]*m-\d{4})\b",
                    r.text,
                )
                if broader:
                    team["pro_slug"] = broader[-1]  # last one is usually the team slug
                    resolved += 1
        except requests.RequestException as e:
            logger.warning(f"Could not fetch team page for {team['name']}: {e}")

        time.sleep(_POLITE_DELAY)
        if (i + 1) % 10 == 0:
            logger.info(f"  Resolved {resolved}/{i+1} pro slugs so far...")

    logger.info(f"Resolved pro slugs for {resolved}/{len(teams)} teams")
    return teams


def fetch_team_game_slugs(pro_slug: str, season: int) -> list[str]:
    """
    Fetch all game slugs for a team in a given season.

    pro_slug: team's pro.lacrossereference.com identifier (e.g. "dukem-5269")
    season: 4-digit year (e.g. 2024)

    Returns list of unique game slugs (e.g. ["game-duke-vs-maryland-mlax-2024-6h57", ...])
    Each game appears on both teams' pages, so deduplication is needed at the caller level.
    """
    url = f"{PRO_URL}/{pro_slug}?view=games&year={season}"
    try:
        resp = _SESSION.get(url, timeout=(5, 8))  # (connect, read) — prevents TCP hangs
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch game slugs for {pro_slug}/{season}: {e}")
        return []

    slugs = list(dict.fromkeys(_GAME_SLUG_RE.findall(resp.text)))
    logger.debug(f"{pro_slug} {season}: {len(slugs)} game slugs")
    return slugs


def fetch_game_box_score(game_slug: str) -> dict | None:
    """
    Fetch box score stats for a single completed game.

    game_slug: e.g. "game-duke-vs-jacksonville-mlax-2026-8195"

    Returns dict:
        {
            "game_slug": str,
            "title": str,                 # "Duke MLAX vs Jacksonville, Feb 21, 2026"
            "game_date": str | None,      # parsed from title: "2026-02-21"
            "home_lr_id": int,            # LR internal numeric ID for home team
            "away_lr_id": int,
            "season": int,
            # Per-team box score (home perspective):
            "home_shots": int | None,
            "away_shots": int | None,
            "home_sog": int | None,
            "away_sog": int | None,
            "home_goals": int | None,
            "away_goals": int | None,
            "home_assists": int | None,
            "away_assists": int | None,
            "home_possessions": int | None,
            "away_possessions": int | None,
            "home_turnovers": int | None,
            "away_turnovers": int | None,
            "home_gbs": int | None,
            "away_gbs": int | None,
            "home_faceoff_wins": int | None,
            "away_faceoff_wins": int | None,
            "home_saves": int | None,
            "away_saves": int | None,
            "home_top": float | None,     # time of possession (0–1)
            "away_top": float | None,
            # Derived
            "faceoffs_total": int | None, # home_fw + away_fw
            "home_fo_pct": float | None,
            "home_shot_pct": float | None,
            "away_shot_pct": float | None,
            "home_sog_pct": float | None,
            "away_sog_pct": float | None,
            "home_save_pct": float | None,
            "away_save_pct": float | None,
            "home_to_margin": float | None,  # caused TOs not available; use -turnovers as proxy
        }
    or None if no box score data is available.
    """
    url = f"{PRO_URL}/{game_slug}"
    try:
        resp = _SESSION.get(url, timeout=(5, 20))  # (connect, read)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch game box score for {game_slug}: {e}")
        return None

    html = resp.text

    # Extract BasicSummaryCounting stats array
    stats_match = re.search(
        r'"BasicSummaryCounting":\s*(\[.*?\])\s*[,}]', html, re.DOTALL
    )
    if not stats_match:
        logger.debug(f"No box score data for {game_slug} (page size={len(html)})")
        return None

    try:
        stats_list = json.loads(stats_match.group(1))
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse box score JSON for {game_slug}")
        return None

    # Build stat lookup
    stats = {s["tag"]: s for s in stats_list}

    def _int(tag, side):
        s = stats.get(tag, {})
        v = s.get(f"{side}_val")
        return int(v) if v is not None else None

    def _float(tag, side):
        s = stats.get(tag, {})
        v = s.get(f"{side}_val")
        return float(v) if v is not None else None

    # Game metadata
    title_match = re.search(r'"title":\s*"([^"]+)"', html)
    title = title_match.group(1) if title_match else ""

    # Parse date from title: "Duke MLAX vs Jacksonville, Feb 21, 2026"
    game_date = _parse_date_from_title(title)

    home_id_match = re.search(r'"home_ID":\s*(\d+)', html)
    away_id_match = re.search(r'"away_ID":\s*(\d+)', html)

    # Always parse season from slug — the HTML "year" field is the current year,
    # not the game's season year (confirmed bug: returns 2026 for 2024 games).
    season = int(re.search(r"-mlax-(\d{4})-", game_slug).group(1))

    home_fw = _int("faceoffs", "home")
    away_fw = _int("faceoffs", "away")
    fo_total = (home_fw + away_fw) if (home_fw is not None and away_fw is not None) else None

    home_goals = _int("goals", "home")
    away_goals = _int("goals", "away")
    home_shots = _int("shots", "home")
    away_shots = _int("shots", "away")
    home_sog   = _int("sog", "home")
    away_sog   = _int("sog", "away")
    home_saves = _int("saves", "home")
    away_saves = _int("saves", "away")

    def _safe_div(a, b):
        return round(a / b, 4) if (a is not None and b and b > 0) else None

    return {
        "game_slug":         game_slug,
        "title":             title,
        "game_date":         game_date,
        "season":            season,
        "home_lr_id":        int(home_id_match.group(1)) if home_id_match else None,
        "away_lr_id":        int(away_id_match.group(1)) if away_id_match else None,

        # Raw counts
        "home_shots":        home_shots,
        "away_shots":        away_shots,
        "home_sog":          home_sog,
        "away_sog":          away_sog,
        "home_goals":        home_goals,
        "away_goals":        away_goals,
        "home_assists":      _int("assists", "home"),
        "away_assists":      _int("assists", "away"),
        "home_possessions":  _int("possessions", "home"),
        "away_possessions":  _int("possessions", "away"),
        "home_turnovers":    _int("turnovers", "home"),
        "away_turnovers":    _int("turnovers", "away"),
        "home_gbs":          _int("gbs", "home"),
        "away_gbs":          _int("gbs", "away"),
        "home_faceoff_wins": home_fw,
        "away_faceoff_wins": away_fw,
        "home_saves":        home_saves,
        "away_saves":        away_saves,
        "home_top":          _float("top", "home"),
        "away_top":          _float("top", "away"),

        # Derived rates
        "faceoffs_total":    fo_total,
        "home_fo_pct":       _safe_div(home_fw, fo_total),
        "away_fo_pct":       _safe_div(away_fw, fo_total),
        "home_shot_pct":     _safe_div(home_goals, home_shots),
        "away_shot_pct":     _safe_div(away_goals, away_shots),
        "home_sog_pct":      _safe_div(home_goals, home_sog),
        "away_sog_pct":      _safe_div(away_goals, away_sog),
        "home_save_pct":     _safe_div(home_saves, home_sog),   # saves / opp SOG
        "away_save_pct":     _safe_div(away_saves, away_sog),
    }


def _parse_date_from_title(title: str) -> str | None:
    """
    Parse game date from title like "Duke MLAX vs Jacksonville, Feb 21, 2026".
    Returns ISO format string "2026-02-21" or None.
    """
    months = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }
    m = re.search(r"(\w{3})\s+(\d{1,2}),\s+(\d{4})", title)
    if m:
        month_str, day, year = m.groups()
        month_num = months.get(month_str)
        if month_num:
            return f"{year}-{month_num:02d}-{int(day):02d}"
    return None


def fetch_season_aggregate_stats(season: int = None) -> list[dict]:
    """
    Fetch current-season aggregate stats for all D1 men teams
    from lacrossereference.com stats pages.

    Returns list of dicts with team-level aggregate stats.
    Useful as supplemental data (adj. efficiency, possession%, etc.)
    that goes beyond raw counting stats available in game box scores.
    """
    stat_pages = {
        "efficiency": f"{BASE_URL}/stats/adj-efficiency-d1-men",
        "shooting":   f"{BASE_URL}/stats/adj-shooting-pct-d1-men",
        "possession": f"{BASE_URL}/stats/timeofpossession-d1-men",
        "pace":       f"{BASE_URL}/stats/pace-d1-men",
    }

    # Collect per-team data from all pages
    team_data: dict[str, dict] = {}

    for stat_type, url in stat_pages.items():
        try:
            resp = _SESSION.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch {stat_type} stats: {e}")
            time.sleep(_POLITE_DELAY)
            continue

        td_match = re.search(r"var td\s*=\s*(\{.*?\})\s*;", resp.text, re.DOTALL)
        if not td_match:
            time.sleep(_POLITE_DELAY)
            continue

        try:
            data = json.loads(td_match.group(1))
        except json.JSONDecodeError:
            time.sleep(_POLITE_DELAY)
            continue

        for row in data.get("data", []):
            name = row.get("display_name")
            if isinstance(name, dict):
                name = name.get("val", "")
            if not name:
                continue

            if name not in team_data:
                team_data[name] = {"name": name}

            # Extract numeric values
            for key, val in row.items():
                if key in ("display_name", "link", "pro_url_icon"):
                    continue
                if isinstance(val, dict) and "val" in val:
                    v = val["val"]
                    if isinstance(v, (int, float)):
                        team_data[name][key] = v

        time.sleep(_POLITE_DELAY)

    return list(team_data.values())


def probe_site_structure():
    """Diagnostic: print the structure of key pages. Run this first."""
    from pprint import pprint

    print("=== D1 Men Efficiency Page ===")
    r = _SESSION.get(f"{BASE_URL}/stats/adj-efficiency-d1-men", timeout=20)
    td = re.search(r"var td\s*=\s*(\{.*?\})\s*;", r.text, re.DOTALL)
    if td:
        d = json.loads(td.group(1))
        print(f"Teams: {len(d.get('data', []))}")
        print(f"Fields: {[f.get('sort_by') for f in d.get('fields', [])]}")
        print(f"Sample row: {d['data'][0] if d.get('data') else 'NONE'}")

    print("\n=== Sample Game Box Score (Duke vs Jacksonville 2026) ===")
    bs = fetch_game_box_score("game-duke-vs-jacksonville-mlax-2026-8195")
    if bs:
        pprint(bs)
    else:
        print("No data returned")
