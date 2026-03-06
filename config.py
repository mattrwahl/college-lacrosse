import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR  = BASE_DIR / "logs"
DB_PATH  = DATA_DIR / "college_lacrosse.db"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

# The Odds API key — shared with baseball_betting
ODDS_API_KEY = os.environ["ODDS_API_KEY"]

# Current season year
SEASON_YEAR = 2026

# Seasons available for historical backfill
# Men's college lacrosse season runs Feb–May; 2020 season was cancelled (COVID)
ALL_SEASONS = list(range(2016, 2020)) + list(range(2021, 2027))

TRAIN_SEASONS = [s for s in range(2016, 2024) if s != 2020]
VAL_SEASONS   = [2024, 2025]
TEST_SEASONS  = [2026]

# ESPN API base — sport key confirmed for men's college lacrosse
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/lacrosse/mens-college-lacrosse/scoreboard"
ESPN_TEAMS_URL      = "https://site.api.espn.com/apis/site/v2/sports/lacrosse/mens-college-lacrosse/teams"

# lacrossereference.com — primary stats source
LACROSSE_REF_BASE  = "https://www.lacrossereference.com"
LACROSSE_REF_TEAMS = f"{LACROSSE_REF_BASE}/college/teams.html"

# The Odds API sport key for college lacrosse
# NOTE: verify with get_available_sports() — key may be "lacrosse_ncaa" or similar
ODDS_API_SPORT = "lacrosse_ncaa"

# Books to pull from (priority order)
PREFERRED_BOOKS = ["betonline_ag", "draftkings", "fanduel", "bovada", "betmgm"]

# Markets to fetch: h2h (moneyline) + spreads
ODDS_MARKETS = ["h2h", "spreads"]

# Rolling window for recent-form features (games)
ROLLING_WINDOW = 5

# Model parameters
MIN_GAMES_FOR_PREDICTION = 3   # min games played before we make a prediction
SPREAD_THRESHOLD = 2.0          # min predicted vs. market spread gap to flag a bet

LOG_LEVEL = "INFO"
