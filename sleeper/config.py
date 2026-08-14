"""
Configuration for the Sleeper core.

Identity (username + league) has no hardcoded defaults. The values must be
provided via environment variables or set at runtime via the MCP config tools
(sleeper_set_username / sleeper_set_league) before any league-aware tool works.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Identity ────────────────────────────────────────────────────────────────
SLEEPER_USERNAME: str = os.getenv("SLEEPER_USERNAME", "GronkQuixote")

# Name fragment used to pick the right league when a user has several.
LEAGUE_NAME_MATCH: str = os.getenv("SLEEPER_LEAGUE_MATCH", "chrysoloras")

# Set this to pin a specific league and skip name matching entirely.
LEAGUE_ID: str | None = os.getenv("SLEEPER_LEAGUE_ID") or None


def set_username(username: str) -> None:
    """Override the active Sleeper username at runtime."""
    global SLEEPER_USERNAME
    SLEEPER_USERNAME = username


def set_league_match(match: str) -> None:
    """Override the league name fragment used for matching at runtime."""
    global LEAGUE_NAME_MATCH, LEAGUE_ID
    LEAGUE_NAME_MATCH = match
    LEAGUE_ID = None  # clear any pinned ID so name matching takes effect


def set_league_id(league_id: str) -> None:
    """Pin a specific league ID, bypassing name matching."""
    global LEAGUE_ID
    LEAGUE_ID = league_id

# ── Seasons ─────────────────────────────────────────────────────────────────
CURRENT_SEASON = os.getenv("SLEEPER_SEASON", "2026")
STATS_SEASON = os.getenv("SLEEPER_STATS_SEASON", "2025")

# ── Upstream APIs ───────────────────────────────────────────────────────────
API_BASE = "https://api.sleeper.app/v1"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

# ── HTTP behavior ───────────────────────────────────────────────────────────
HTTP_TIMEOUT = float(os.getenv("SLEEPER_HTTP_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("SLEEPER_MAX_RETRIES", "3"))

# Sleeper documents a 1,000 calls per minute ceiling before IP blocking.
# We hold well under it; this is a guard rail, not a throttle we expect to hit.
RATE_LIMIT_PER_MINUTE = int(os.getenv("SLEEPER_RATE_LIMIT", "600"))

# ── Cache TTLs, seconds (ADR 001 cache policy table) ────────────────────────
TTL_PLAYERS = 24 * 60 * 60   # docs: fetch at most once per day, ~5 MB
TTL_NFL_STATE = 5 * 60       # drives current week; the old infinite cache was a bug
TTL_LEAGUE = 10 * 60         # changes on waivers and trades
TTL_ROSTERS = 10 * 60
TTL_MATCHUPS_LIVE = 30       # the live path, current week only
TTL_MATCHUPS_FINAL = None    # completed weeks are immutable, cache forever
TTL_STATS = 10 * 60
TTL_PROJECTIONS = 60 * 60
TTL_SCHEDULE = 24 * 60 * 60

# ── Disk cache ──────────────────────────────────────────────────────────────
CACHE_DIR = Path(os.getenv("SLEEPER_CACHE_DIR", Path.home() / ".cache" / "sleeper-mcp"))

# ── Dashboard polling ───────────────────────────────────────────────────────
POLL_INTERVAL_LIVE = int(os.getenv("SLEEPER_POLL_LIVE", "30"))     # during games
POLL_INTERVAL_IDLE = int(os.getenv("SLEEPER_POLL_IDLE", "900"))    # otherwise

WEB_HOST = os.getenv("SLEEPER_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("SLEEPER_WEB_PORT", "8080"))
