"""
Database initialization and core CRUD helpers for college lacrosse.
"""
import sqlite3
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DB_PATH
from db.models import ALL_TABLES

logger = logging.getLogger(__name__)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    with conn:
        for ddl in ALL_TABLES:
            conn.execute(ddl)
        _migrate_db(conn)
    logger.info(f"Database initialized at {DB_PATH}")
    conn.close()


def _migrate_db(conn: sqlite3.Connection):
    """Apply incremental schema migrations for existing databases."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
    if "lr_game_slug" not in existing_cols:
        conn.execute("ALTER TABLE games ADD COLUMN lr_game_slug TEXT")
        logger.info("DB migration: added lr_game_slug column to games table")
    if "source" not in existing_cols:
        conn.execute("ALTER TABLE games ADD COLUMN source TEXT DEFAULT 'espn'")
        logger.info("DB migration: added source column to games table")
    # Always ensure the partial unique index exists (handles both fresh install and migration)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_games_lr_slug "
        "ON games(lr_game_slug) WHERE lr_game_slug IS NOT NULL"
    )


def upsert_team(conn: sqlite3.Connection, canonical_name: str, **kwargs) -> int:
    """
    Insert or update a team record. Returns team id.
    kwargs: espn_name, espn_id, lacrosse_ref_name, lr_pro_slug, ncaa_name
    """
    conn.execute(
        """
        INSERT INTO teams (canonical_name, espn_name, espn_id, lacrosse_ref_name, lr_pro_slug, ncaa_name)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_name) DO UPDATE SET
            espn_name         = COALESCE(excluded.espn_name, espn_name),
            espn_id           = COALESCE(excluded.espn_id, espn_id),
            lacrosse_ref_name = COALESCE(excluded.lacrosse_ref_name, lacrosse_ref_name),
            lr_pro_slug       = COALESCE(excluded.lr_pro_slug, lr_pro_slug),
            ncaa_name         = COALESCE(excluded.ncaa_name, ncaa_name)
        """,
        (
            canonical_name,
            kwargs.get("espn_name"),
            kwargs.get("espn_id"),
            kwargs.get("lacrosse_ref_name"),
            kwargs.get("lr_pro_slug"),
            kwargs.get("ncaa_name"),
        ),
    )
    row = conn.execute(
        "SELECT id FROM teams WHERE canonical_name = ?", (canonical_name,)
    ).fetchone()
    return row["id"]


def get_team_id(conn: sqlite3.Connection, name: str) -> int | None:
    """
    Look up a team id by any known name alias.
    Searches canonical_name, espn_name, lacrosse_ref_name, ncaa_name.
    """
    row = conn.execute(
        """
        SELECT id FROM teams
        WHERE canonical_name = ?
           OR espn_name = ?
           OR lacrosse_ref_name = ?
           OR ncaa_name = ?
        LIMIT 1
        """,
        (name, name, name, name),
    ).fetchone()
    return row["id"] if row else None


def get_team_by_lr_pro_slug(conn: sqlite3.Connection, lr_pro_slug: str) -> int | None:
    """Look up a team id by their pro.lacrossereference.com slug."""
    row = conn.execute(
        "SELECT id FROM teams WHERE lr_pro_slug = ?", (lr_pro_slug,)
    ).fetchone()
    return row["id"] if row else None


def build_lr_numeric_id_map(conn: sqlite3.Connection) -> dict:
    """
    Build a mapping from LR numeric team ID (e.g. 5269) to our teams.id.
    Numeric ID is extracted from lr_pro_slug (e.g. "dukem-5269" → 5269).
    """
    rows = conn.execute(
        "SELECT id, lr_pro_slug FROM teams WHERE lr_pro_slug IS NOT NULL"
    ).fetchall()
    result = {}
    for row in rows:
        m = re.search(r"-(\d+)$", row["lr_pro_slug"])
        if m:
            result[int(m.group(1))] = row["id"]
    return result


def build_slug_name_map(conn: sqlite3.Connection) -> dict:
    """
    Build a mapping from game-slug team name to our teams.id.

    Pro slugs follow the pattern "{teamname}m-{digits}" (men's suffix).
    Game slugs use just "{teamname}" (without the "m-DDDD" suffix)
    AND with all hyphens removed.

    Examples:
      lr_pro_slug "dukem-5269"         → slug_name "duke"         → team_db_id
      lr_pro_slug "notre-damem-1853"   → slug_name "notredame"    → team_db_id
      lr_pro_slug "north-carolinam-..." → slug_name "northcarolina" → team_db_id

    Verified from game slugs like:
      "game-duke-vs-notredame-mlax-2024-6h86"
      "game-northcarolina-vs-pennstate-mlax-2024-..."
    """
    rows = conn.execute(
        "SELECT id, lr_pro_slug FROM teams WHERE lr_pro_slug IS NOT NULL"
    ).fetchall()
    result = {}
    for row in rows:
        m = re.match(r"^(.*?)m-\d+$", row["lr_pro_slug"])
        if m:
            # Strip hyphens: "notre-dame" → "notredame" to match game slug format
            slug_name = m.group(1).replace("-", "")
            result[slug_name] = row["id"]
    return result


def upsert_game(conn: sqlite3.Connection, game: dict) -> int:
    """
    Insert or update a game record. Returns game id.
    game dict keys: season, game_date, home_team_id, away_team_id,
                    espn_game_id, neutral_site, conference_game,
                    tournament_game, tournament_round
    """
    conn.execute(
        """
        INSERT INTO games (
            season, game_date, home_team_id, away_team_id,
            espn_game_id, neutral_site, conference_game,
            tournament_game, tournament_round
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(espn_game_id) DO UPDATE SET
            game_date       = excluded.game_date,
            home_team_id    = excluded.home_team_id,
            away_team_id    = excluded.away_team_id,
            neutral_site    = excluded.neutral_site,
            conference_game = excluded.conference_game,
            tournament_game = excluded.tournament_game,
            tournament_round= excluded.tournament_round
        """,
        (
            game["season"],
            game["game_date"],
            game["home_team_id"],
            game["away_team_id"],
            game.get("espn_game_id"),
            game.get("neutral_site", 0),
            game.get("conference_game", 0),
            game.get("tournament_game", 0),
            game.get("tournament_round"),
        ),
    )
    row = conn.execute(
        "SELECT id FROM games WHERE espn_game_id = ?",
        (game["espn_game_id"],),
    ).fetchone()
    return row["id"] if row else conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def upsert_lr_game(conn: sqlite3.Connection, game: dict) -> int:
    """
    Insert or update a game row sourced only from LacrosseReference (no espn_game_id).
    Uses lr_game_slug as the dedup key via a partial unique index.

    game dict keys: season, game_date, home_team_id, away_team_id,
                    lr_game_slug, neutral_site (optional, default 0)
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO games
            (season, game_date, home_team_id, away_team_id, lr_game_slug, source, neutral_site)
        VALUES (?, ?, ?, ?, ?, 'lr', ?)
        """,
        (
            game["season"],
            game["game_date"],
            game["home_team_id"],
            game["away_team_id"],
            game["lr_game_slug"],
            game.get("neutral_site", 0),
        ),
    )
    # Also update in case this is a re-run with corrected data
    conn.execute(
        """
        UPDATE games SET
            game_date    = ?,
            home_team_id = ?,
            away_team_id = ?
        WHERE lr_game_slug = ?
        """,
        (game["game_date"], game["home_team_id"], game["away_team_id"], game["lr_game_slug"]),
    )
    row = conn.execute(
        "SELECT id FROM games WHERE lr_game_slug = ?", (game["lr_game_slug"],)
    ).fetchone()
    return row["id"]


def upsert_result(conn: sqlite3.Connection, game_id: int, result: dict):
    """Insert or update a game result."""
    home_score = result.get("home_score")
    away_score = result.get("away_score")
    actual_margin = None
    if home_score is not None and away_score is not None:
        actual_margin = home_score - away_score
    winner_id = result.get("winner_team_id")
    conn.execute(
        """
        INSERT INTO results (game_id, home_score, away_score, actual_margin, winner_team_id, game_status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            home_score    = excluded.home_score,
            away_score    = excluded.away_score,
            actual_margin = excluded.actual_margin,
            winner_team_id= excluded.winner_team_id,
            game_status   = excluded.game_status,
            fetched_at    = datetime('now')
        """,
        (game_id, home_score, away_score, actual_margin, winner_id, result.get("game_status")),
    )


def upsert_game_stats(conn: sqlite3.Connection, game_id: int, box: dict):
    """
    Insert or update a game_stats row from a fetch_game_box_score() result dict.
    On conflict (same game_id), replaces all values.
    """
    conn.execute(
        """
        INSERT INTO game_stats (
            game_id, game_slug,
            home_goals, home_shots, home_sog, home_assists, home_possessions,
            home_turnovers, home_gbs, home_faceoff_wins, home_saves, home_top,
            away_goals, away_shots, away_sog, away_assists, away_possessions,
            away_turnovers, away_gbs, away_faceoff_wins, away_saves, away_top,
            faceoffs_total,
            home_fo_pct, home_shot_pct, home_sog_pct, home_save_pct,
            away_fo_pct, away_shot_pct, away_sog_pct, away_save_pct
        ) VALUES (
            ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?
        )
        ON CONFLICT(game_id) DO UPDATE SET
            game_slug         = excluded.game_slug,
            home_goals        = excluded.home_goals,
            home_shots        = excluded.home_shots,
            home_sog          = excluded.home_sog,
            home_assists      = excluded.home_assists,
            home_possessions  = excluded.home_possessions,
            home_turnovers    = excluded.home_turnovers,
            home_gbs          = excluded.home_gbs,
            home_faceoff_wins = excluded.home_faceoff_wins,
            home_saves        = excluded.home_saves,
            home_top          = excluded.home_top,
            away_goals        = excluded.away_goals,
            away_shots        = excluded.away_shots,
            away_sog          = excluded.away_sog,
            away_assists      = excluded.away_assists,
            away_possessions  = excluded.away_possessions,
            away_turnovers    = excluded.away_turnovers,
            away_gbs          = excluded.away_gbs,
            away_faceoff_wins = excluded.away_faceoff_wins,
            away_saves        = excluded.away_saves,
            away_top          = excluded.away_top,
            faceoffs_total    = excluded.faceoffs_total,
            home_fo_pct       = excluded.home_fo_pct,
            home_shot_pct     = excluded.home_shot_pct,
            home_sog_pct      = excluded.home_sog_pct,
            home_save_pct     = excluded.home_save_pct,
            away_fo_pct       = excluded.away_fo_pct,
            away_shot_pct     = excluded.away_shot_pct,
            away_sog_pct      = excluded.away_sog_pct,
            away_save_pct     = excluded.away_save_pct,
            fetched_at        = datetime('now')
        """,
        (
            game_id, box.get("game_slug"),
            box.get("home_goals"), box.get("home_shots"), box.get("home_sog"),
            box.get("home_assists"), box.get("home_possessions"),
            box.get("home_turnovers"), box.get("home_gbs"), box.get("home_faceoff_wins"),
            box.get("home_saves"), box.get("home_top"),
            box.get("away_goals"), box.get("away_shots"), box.get("away_sog"),
            box.get("away_assists"), box.get("away_possessions"),
            box.get("away_turnovers"), box.get("away_gbs"), box.get("away_faceoff_wins"),
            box.get("away_saves"), box.get("away_top"),
            box.get("faceoffs_total"),
            box.get("home_fo_pct"), box.get("home_shot_pct"), box.get("home_sog_pct"),
            box.get("home_save_pct"),
            box.get("away_fo_pct"), box.get("away_shot_pct"), box.get("away_sog_pct"),
            box.get("away_save_pct"),
        ),
    )
