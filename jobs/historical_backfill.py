"""
Historical data backfill for college lacrosse.

Steps:
  1. Fetch ESPN team list → populate teams table
  2. Fetch all games for each historical season from ESPN → upsert games + results
  3. Fetch D1 team list from lacrossereference.com → update lr_pro_slug on teams
  4. Fetch per-game box scores from pro.lacrossereference.com → populate game_stats

Usage:
    python main.py backfill [--season YYYY] [--games-only] [--stats-only] [--box-only]

If no season specified, backfills ALL_SEASONS.
"""
import re
import sys
import time
import logging
import argparse
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import (
    get_db, init_db, upsert_team, upsert_game, upsert_lr_game, get_team_id,
    upsert_result, upsert_game_stats, build_slug_name_map,
)
from scrapers.espn_scraper import fetch_season_schedule, fetch_teams
from scrapers.lacrosse_ref import (
    fetch_d1_men_teams, fetch_team_game_slugs, fetch_game_box_score,
    probe_site_structure,
)
from config import ALL_SEASONS, SEASON_YEAR, DB_PATH
from shared.team_mapper import resolve_team_name

logger = logging.getLogger(__name__)

# Explicit LR display name → ESPN canonical name overrides.
# Used in backfill_lr_team_data() to resolve ambiguous or failed fuzzy matches.
# Teams NOT in this dict use the automated (exact → prefix → fuzzy) pipeline.
_LR_ESPN_OVERRIDES: dict[str, str] = {
    # Prefix-matching false positives: LR short name is a PREFIX of the wrong ESPN team
    "Penn":            "Pennsylvania Quakers",      # "Penn " is prefix of "Penn State Nittany Lions"
    "UMass":           "Massachusetts Minutemen",   # "UMass " is prefix of "UMass Lowell River Hawks"
    "Albany":          "UAlbany Great Danes",       # LR "Albany" → ESPN "UAlbany Great Danes" (not duplicate "Albany")
    # Teams that fail all automated matching due to name divergence
    "Penn State":      "Penn State Nittany Lions",
    "Notre Dame":      "Notre Dame Fighting Irish",
    "Ohio State":      "Ohio State Buckeyes",
    "North Carolina":  "North Carolina Tar Heels",
    "Boston U":        "Boston University Terriers",
    "Johns Hopkins":   "Johns Hopkins University Blue Jays",
    "Sacred Heart":    "Sacred Heart Pioneers",
    "St. John's":      "St. John's Red Storm",
    "Holy Cross":      "Holy Cross Crusaders",
    "Stony Brook":     "Stony Brook Seawolves",
    "Robert Morris":   "Robert Morris Colonials",
    "St. Bonaventure": "St. Bonaventure Bonnies",
    "Mount St Marys":  "Mount St. Mary's Mountaineers",
    "Air Force":       "Air Force Falcons",
    "High Point":      "High Point Panthers",
    "Le Moyne":        "Le Moyne Dolphins",
    "Cleveland State": "Cleveland State Vikings",
    "Saint Joseph's":  "Saint Joseph's Hawks",
    # Not in LR D1 database: Hartford, Furman, Lindenwood, Roberts Wesleyan
}


def backfill_teams(conn) -> int:
    """Fetch ESPN team list and populate teams table. Returns count upserted."""
    logger.info("Fetching teams from ESPN...")
    teams = fetch_teams()
    count = 0
    with conn:
        for t in teams:
            upsert_team(
                conn,
                canonical_name=t["name"],
                espn_name=t["name"],
                espn_id=str(t["espn_id"]) if t.get("espn_id") else None,
            )
            count += 1
    logger.info(f"Upserted {count} teams from ESPN")
    return count


def _find_by_prefix(conn, lr_name: str) -> int | None:
    """
    Find a team whose ESPN canonical_name STARTS WITH the LR short name.
    Handles: LR "Duke" → ESPN "Duke Blue Devils".
    """
    row = conn.execute(
        "SELECT id FROM teams WHERE canonical_name LIKE ? OR espn_name LIKE ? LIMIT 1",
        (f"{lr_name} %", f"{lr_name} %"),
    ).fetchone()
    return row["id"] if row else None


def _find_by_fuzzy(conn, lr_name: str, canonical_names: list) -> int | None:
    """Difflib fuzzy match of LR name against all canonical names."""
    from difflib import get_close_matches
    matches = get_close_matches(lr_name, canonical_names, n=1, cutoff=0.75)
    if matches:
        return get_team_id(conn, matches[0])
    return None


def backfill_lr_team_data(conn, reset_existing: bool = False) -> int:
    """
    Fetch D1 men team list from lacrossereference.com and update each team's
    lr_pro_slug and lacrosse_ref_name in the teams table.

    reset_existing: if True, clears all lr_pro_slug / lacrosse_ref_name values
                    before re-populating (use when fixing wrong assignments).

    Returns number of teams updated.
    """
    if reset_existing:
        logger.info("Clearing existing lr_pro_slug / lacrosse_ref_name before re-mapping...")
        with conn:
            conn.execute("UPDATE teams SET lr_pro_slug = NULL, lacrosse_ref_name = NULL")

    logger.info("Fetching D1 team list from lacrossereference.com...")
    lr_teams = fetch_d1_men_teams()

    # Fetch all canonical names for fuzzy matching
    canonical_names = [
        r[0] for r in conn.execute("SELECT canonical_name FROM teams").fetchall()
    ]

    updated = 0
    with conn:
        for t in lr_teams:
            lr_name = t.get("name")
            pro_slug = t.get("pro_slug")
            if not lr_name or not pro_slug:
                logger.debug(f"Skipping LR team '{lr_name}' — no pro_slug resolved")
                continue

            # 0. Explicit override takes highest priority (handles ambiguous prefix matches
            #    and name divergence cases like "Penn" vs "Penn State", "Albany" vs "UAlbany")
            if lr_name in _LR_ESPN_OVERRIDES:
                espn_name = _LR_ESPN_OVERRIDES[lr_name]
                team_id = get_team_id(conn, espn_name)
                if team_id is None:
                    logger.warning(f"Override target '{espn_name}' for LR '{lr_name}' not found in DB")
            else:
                # Multi-strategy match: LR uses short names ("Duke"), ESPN uses full
                # names with mascots ("Duke Blue Devils"). Try in order:
                team_id = (
                    get_team_id(conn, lr_name)                               # 1. exact
                    or _find_by_prefix(conn, lr_name)                        # 2. ESPN name starts with LR name
                    or _find_by_fuzzy(conn, lr_name, canonical_names)        # 3. difflib fuzzy
                )

            if team_id is not None:
                conn.execute(
                    """
                    UPDATE teams
                    SET lacrosse_ref_name = COALESCE(lacrosse_ref_name, ?),
                        lr_pro_slug       = ?
                    WHERE id = ?
                    """,
                    (lr_name, pro_slug, team_id),
                )
            else:
                logger.warning(f"Could not resolve team name: '{lr_name}'")
                # Insert as new team with LR data only
                upsert_team(conn, lr_name, lacrosse_ref_name=lr_name, lr_pro_slug=pro_slug)

            updated += 1

    logger.info(f"Updated lr_pro_slug for {updated} D1 teams")
    return updated


def backfill_season_games(conn, season: int) -> int:
    """Fetch all games for a season from ESPN and store them."""
    logger.info(f"Fetching ESPN schedule for {season} season...")
    games = fetch_season_schedule(season)

    if not games:
        logger.warning(f"No games returned from ESPN for {season}")
        return 0

    inserted = 0
    skipped = 0

    with conn:
        for game in games:
            home_name = game["home_team"]
            away_name = game["away_team"]

            home_id = get_team_id(conn, home_name)
            away_id = get_team_id(conn, away_name)

            if home_id is None:
                home_id = upsert_team(conn, home_name, espn_name=home_name,
                                      espn_id=game.get("home_espn_id"))
            if away_id is None:
                away_id = upsert_team(conn, away_name, espn_name=away_name,
                                      espn_id=game.get("away_espn_id"))

            if not game.get("espn_game_id"):
                skipped += 1
                continue

            game_record = {
                "season":           season,
                "game_date":        game["game_date"],
                "home_team_id":     home_id,
                "away_team_id":     away_id,
                "espn_game_id":     game["espn_game_id"],
                "neutral_site":     1 if game.get("neutral_site") else 0,
                "conference_game":  1 if game.get("conference_game") else 0,
                "tournament_game":  1 if game.get("tournament_game") else 0,
                "tournament_round": game.get("tournament_round"),
            }
            game_id = upsert_game(conn, game_record)

            if game["game_status"] == "final" and game.get("home_score") is not None:
                home_score = game["home_score"]
                away_score = game["away_score"]
                winner_id = home_id if home_score > away_score else away_id
                upsert_result(conn, game_id, {
                    "home_score":     home_score,
                    "away_score":     away_score,
                    "winner_team_id": winner_id,
                    "game_status":    "final",
                })

            inserted += 1

    logger.info(f"Season {season}: {inserted} games upserted, {skipped} skipped")
    return inserted


def backfill_game_box_scores(conn, seasons: list[int]) -> int:
    """
    Fetch per-game box scores from pro.lacrossereference.com for all seasons
    and populate the game_stats table.

    Strategy:
      1. Query all teams with lr_pro_slug from DB
      2. For each team × season, fetch game slugs (deduplicated across teams)
      3. For each unique slug, fetch box score
      4. Match box score to our games table via home_team_id + game_date
      5. Insert into game_stats

    Returns number of game_stats rows inserted/updated.
    """
    logger.info("Starting per-game box score backfill from pro.lacrossereference.com...")

    # Teams with LR slugs
    team_rows = conn.execute(
        "SELECT id, canonical_name, lr_pro_slug FROM teams WHERE lr_pro_slug IS NOT NULL"
    ).fetchall()

    if not team_rows:
        logger.warning("No teams have lr_pro_slug — run backfill_lr_team_data() first")
        return 0

    logger.info(f"Fetching game slugs for {len(team_rows)} teams × {len(seasons)} seasons...")

    # Collect (slug → team_db_id) — if a game appears for both teams, either is fine
    slug_to_team: dict[str, int] = {}
    for ti, team_row in enumerate(team_rows, 1):
        team_db_id = team_row["id"]
        pro_slug = team_row["lr_pro_slug"]
        team_slugs = 0
        for season in seasons:
            slugs = fetch_team_game_slugs(pro_slug, season)
            team_slugs += len(slugs)
            for slug in slugs:
                if slug not in slug_to_team:
                    slug_to_team[slug] = team_db_id
            time.sleep(0.2)  # polite delay
        logger.info(
            f"  Slug collection: {ti}/{len(team_rows)} teams done "
            f"({team_row['canonical_name']} — {team_slugs} new slugs, "
            f"{len(slug_to_team)} total so far)"
        )

    logger.info(f"Collected {len(slug_to_team)} unique game slugs across all seasons")

    # Build slug-name → team DB id mapping.
    # Game slugs encode team names directly: "game-duke-vs-jacksonville-mlax-2026-8195"
    # maps to pro slugs "dukem-5269" (strip "m-DDDD" → "duke").
    slug_name_map = build_slug_name_map(conn)
    logger.info(f"Slug name map: {len(slug_name_map)} teams")

    # Regex to parse home/away slug names and year from a game slug
    # Team names may contain hyphens (e.g. "notre-dame", "penn-state", "north-carolina")
    _SLUG_PARSE_RE = re.compile(r'^game-([a-z-]+)-vs-([a-z-]+)-mlax-(\d{4})-[a-z0-9]+$')

    inserted = 0
    skipped_no_data = 0
    skipped_no_game = 0
    lr_only_candidates = []  # (game_slug, box, home_team_db_id, away_team_db_id)

    for i, (game_slug, fallback_team_id) in enumerate(slug_to_team.items(), 1):
        if i % 100 == 0:
            logger.info(f"  Box scores: {i}/{len(slug_to_team)} slugs processed, {inserted} inserted")

        # Parse team slug names and year directly from the game slug (no HTTP needed yet)
        slug_m = _SLUG_PARSE_RE.match(game_slug)
        home_slug_name = slug_m.group(1) if slug_m else None
        away_slug_name = slug_m.group(2) if slug_m else None
        slug_year = int(slug_m.group(3)) if slug_m else None

        # Slug names may contain hyphens (e.g. "notre-dame") but slug_name_map keys
        # have hyphens stripped ("notredame") — must strip before lookup.
        home_slug_clean = home_slug_name.replace("-", "") if home_slug_name else None
        away_slug_clean = away_slug_name.replace("-", "") if away_slug_name else None

        home_team_db_id = slug_name_map.get(home_slug_clean) if home_slug_clean else None
        away_team_db_id = slug_name_map.get(away_slug_clean) if away_slug_clean else None

        # Skip if neither team is in our slug map (both non-D1 or unknown)
        if home_team_db_id is None and away_team_db_id is None:
            known_id = fallback_team_id
        else:
            known_id = home_team_db_id or away_team_db_id

        # Fetch the box score (needed for game_date and stats)
        box = fetch_game_box_score(game_slug)
        if not box or not box.get("game_date"):
            skipped_no_data += 1
            continue

        game_date = box["game_date"]
        season = box["season"]

        # Find game in our DB — try most specific match first
        game_row = None

        if home_team_db_id and away_team_db_id:
            # Both teams known: strict match
            game_row = conn.execute(
                """
                SELECT id FROM games
                WHERE home_team_id = ? AND away_team_id = ?
                  AND game_date = ? AND season = ?
                """,
                (home_team_db_id, away_team_db_id, game_date, season),
            ).fetchone()

            if game_row is None:
                # Try swapped (LR and ESPN may disagree on home/away for neutral sites)
                game_row = conn.execute(
                    """
                    SELECT id FROM games
                    WHERE home_team_id = ? AND away_team_id = ?
                      AND game_date = ? AND season = ?
                    """,
                    (away_team_db_id, home_team_db_id, game_date, season),
                ).fetchone()

        if game_row is None and known_id:
            # Fallback: one known D1 team + date (handles non-D1 opponents)
            game_row = conn.execute(
                """
                SELECT id FROM games
                WHERE (home_team_id = ? OR away_team_id = ?)
                  AND game_date = ? AND season = ?
                """,
                (known_id, known_id, game_date, season),
            ).fetchone()

        # Check if already stored as an LR-only game from a previous backfill run
        if game_row is None:
            game_row = conn.execute(
                "SELECT id FROM games WHERE lr_game_slug = ?", (game_slug,)
            ).fetchone()

        if game_row is None:
            # No ESPN match — queue for LR-only second pass if both teams are D1
            if home_team_db_id and away_team_db_id:
                lr_only_candidates.append((game_slug, box, home_team_db_id, away_team_db_id))
            else:
                skipped_no_game += 1
            continue

        with conn:
            upsert_game_stats(conn, game_row["id"], box)
        inserted += 1

        time.sleep(0.15)

    # Second pass: insert LR-only games (both teams D1, no ESPN equivalent)
    lr_inserted = _insert_lr_only_games(conn, lr_only_candidates)

    logger.info(
        f"Box score backfill done: {inserted} ESPN-matched, "
        f"{lr_inserted} LR-only inserted, "
        f"{skipped_no_data} no data, {skipped_no_game} no matching game (non-D1)"
    )
    return inserted + lr_inserted


def _insert_lr_only_games(conn, candidates: list) -> int:
    """
    Second pass: insert games + results + game_stats for LR-only slugs
    (both teams are D1 but no matching ESPN game exists).

    candidates: list of (game_slug, box_score_dict, home_team_db_id, away_team_db_id)
    """
    inserted = 0
    for game_slug, box, home_id, away_id in candidates:
        game_date = box.get("game_date")
        season = box.get("season")
        home_goals = box.get("home_goals")
        away_goals = box.get("away_goals")

        if not game_date or not season:
            continue

        with conn:
            game_id = upsert_lr_game(conn, {
                "season":       season,
                "game_date":    game_date,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "lr_game_slug": game_slug,
            })

            # Insert result from box score goals
            if home_goals is not None and away_goals is not None:
                winner_id = home_id if home_goals > away_goals else away_id
                upsert_result(conn, game_id, {
                    "home_score":     home_goals,
                    "away_score":     away_goals,
                    "winner_team_id": winner_id,
                    "game_status":    "final",
                })

            upsert_game_stats(conn, game_id, box)

        inserted += 1

    logger.info(f"LR-only second pass: {inserted}/{len(candidates)} games inserted")
    return inserted


def run(
    seasons: list[int] = None,
    games_only: bool = False,
    stats_only: bool = False,
    box_only: bool = False,
):
    """
    Main backfill entry point.

    Typical first run: python main.py backfill
    """
    if seasons is None:
        seasons = ALL_SEASONS

    init_db()
    conn = get_db()

    total_games = 0
    total_box = 0

    if not stats_only and not box_only:
        # Phase 1: Teams from ESPN
        backfill_teams(conn)

        # Phase 2: Games + results per season
        for season in seasons:
            logger.info(f"--- Backfilling ESPN games for {season} ---")
            games = backfill_season_games(conn, season)
            total_games += games

    if not games_only:
        # Phase 3: Update teams with LR pro slugs (needed before box scores)
        if not box_only:
            backfill_lr_team_data(conn)
        elif not _teams_have_lr_slugs(conn):
            logger.info("Running lr team data fetch (required for box scores)...")
            backfill_lr_team_data(conn)

        # Phase 4: Per-game box scores
        total_box = backfill_game_box_scores(conn, seasons)

    logger.info(
        f"Backfill complete: {total_games} games, {total_box} box score rows"
    )
    conn.close()


def _teams_have_lr_slugs(conn) -> bool:
    """Check if any teams have lr_pro_slug populated."""
    row = conn.execute(
        "SELECT COUNT(*) as n FROM teams WHERE lr_pro_slug IS NOT NULL"
    ).fetchone()
    return (row["n"] or 0) > 0


def fix_lr_slugs(seasons: list[int] = None):
    """
    One-shot command to:
      1. Reset all lr_pro_slug / lacrosse_ref_name assignments
      2. Re-fetch D1 team list with corrected name overrides
      3. Re-run box score backfill for the newly-mapped teams

    Use after correcting _LR_ESPN_OVERRIDES to fix wrong slug assignments.
    """
    if seasons is None:
        seasons = ALL_SEASONS

    init_db()
    conn = get_db()

    # Step 1+2: Reset and re-map all slugs
    backfill_lr_team_data(conn, reset_existing=True)

    # Step 3: Re-run box scores (idempotent — won't duplicate existing rows)
    total_box = backfill_game_box_scores(conn, seasons)
    logger.info(f"fix_lr_slugs complete: {total_box} game_stats rows inserted/updated")
    conn.close()


def probe_lacrosse_ref(season: int = 2024):
    """
    Diagnostic: dump lacrossereference.com page structure to console.
    Run this FIRST to verify scraper column mappings before the full backfill.
    """
    from pprint import pprint
    logger.info(f"Probing lacrossereference.com structure for {season}...")
    result = probe_site_structure()
    pprint(result)
