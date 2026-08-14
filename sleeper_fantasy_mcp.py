#!/usr/bin/env python3
"""
Sleeper Fantasy Football MCP Server

FastMCP server for managing a Sleeper fantasy football team.
Pre-configured for GronkQuixote in The Chrysoloras Gang (12-team Full PPR).

No authentication required — uses the public Sleeper API.
"""

import json
import asyncio
from typing import Optional, List, Dict, Any, Union
from enum import Enum

import httpx
from pydantic import BaseModel, Field, ConfigDict
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

# Shared core (ADR 001). This module is now an adapter over sleeper/*.
from sleeper import client as core_client
from sleeper import league as core_league
from sleeper import render

# ─────────────────────────────────────────────────────────────────────────────
# Server initialization
# ─────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("sleeper_fantasy_mcp")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

API_BASE = "https://api.sleeper.app/v1"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

SLEEPER_USERNAME = "GronkQuixote"
CURRENT_SEASON = "2026"   # upcoming / active season
STATS_SEASON = "2025"     # most recently completed season

# Sleeper team abbreviations that differ from ESPN's
ESPN_ABBR_OVERRIDES = {"WAS": "WSH"}

# Position -> key suffix used in Sleeper's DEF "fan_pts_allow_<pos>" stat fields
DEF_ALLOWED_POS_KEY = {"QB": "qb", "RB": "rb", "WR": "wr", "TE": "te", "K": "k"}

# ── Composite ranking tuning ────────────────────────────────────────────────
# composite_score = proj_pts + sos_bonus + snap_trend_bonus
# Both bonuses are symmetric point swings (can be negative), capped so a great/
# terrible schedule or role trend can meaningfully move a ranking but never
# overwhelm actual projected production.
SOS_LOOKAHEAD_WEEKS = 4      # how many upcoming weeks feed the schedule bonus
SOS_MAX_BONUS = 2.0          # max +/- pts swing from strength of schedule
SNAP_TREND_WEEKS = 3         # how many recent completed weeks feed the trend bonus
SNAP_TREND_MAX_BONUS = 3.0   # max +/- pts swing from a rising/falling snap share
SNAP_TREND_CAP_PCT = 30.0    # a snap-share swing of this many pts = the max bonus
# Snap share isn't tracked for K/DEF; SOS via fan_pts_allow isn't meaningful for DEF
SNAP_TREND_ELIGIBLE_POS = {"QB", "RB", "WR", "TE"}

# 32 NFL stadiums: (latitude, longitude, indoor/dome flag, display name)
STADIUM_INFO: Dict[str, Dict[str, Any]] = {
    "ARI": {"lat": 33.5276, "lon": -112.2626, "indoor": True,  "name": "State Farm Stadium"},
    "ATL": {"lat": 33.7554, "lon": -84.4008,  "indoor": True,  "name": "Mercedes-Benz Stadium"},
    "BAL": {"lat": 39.2780, "lon": -76.6227,  "indoor": False, "name": "M&T Bank Stadium"},
    "BUF": {"lat": 42.7738, "lon": -78.7870,  "indoor": False, "name": "Highmark Stadium"},
    "CAR": {"lat": 35.2258, "lon": -80.8528,  "indoor": False, "name": "Bank of America Stadium"},
    "CHI": {"lat": 41.8623, "lon": -87.6167,  "indoor": False, "name": "Soldier Field"},
    "CIN": {"lat": 39.0955, "lon": -84.5160,  "indoor": False, "name": "Paycor Stadium"},
    "CLE": {"lat": 41.5061, "lon": -81.6995,  "indoor": False, "name": "Huntington Bank Field"},
    "DAL": {"lat": 32.7473, "lon": -97.0945,  "indoor": True,  "name": "AT&T Stadium"},
    "DEN": {"lat": 39.7439, "lon": -105.0201, "indoor": False, "name": "Empower Field at Mile High"},
    "DET": {"lat": 42.3400, "lon": -83.0456,  "indoor": True,  "name": "Ford Field"},
    "GB":  {"lat": 44.5013, "lon": -88.0622,  "indoor": False, "name": "Lambeau Field"},
    "HOU": {"lat": 29.6847, "lon": -95.4107,  "indoor": True,  "name": "NRG Stadium"},
    "IND": {"lat": 39.7601, "lon": -86.1639,  "indoor": True,  "name": "Lucas Oil Stadium"},
    "JAX": {"lat": 30.3239, "lon": -81.6373,  "indoor": False, "name": "EverBank Stadium"},
    "KC":  {"lat": 39.0489, "lon": -94.4839,  "indoor": False, "name": "GEHA Field at Arrowhead Stadium"},
    "LAC": {"lat": 33.9535, "lon": -118.3392, "indoor": True,  "name": "SoFi Stadium"},
    "LAR": {"lat": 33.9535, "lon": -118.3392, "indoor": True,  "name": "SoFi Stadium"},
    "LV":  {"lat": 36.0909, "lon": -115.1833, "indoor": True,  "name": "Allegiant Stadium"},
    "MIA": {"lat": 25.9580, "lon": -80.2389,  "indoor": False, "name": "Hard Rock Stadium"},
    "MIN": {"lat": 44.9738, "lon": -93.2578,  "indoor": True,  "name": "U.S. Bank Stadium"},
    "NE":  {"lat": 42.0909, "lon": -71.2643,  "indoor": False, "name": "Gillette Stadium"},
    "NO":  {"lat": 29.9511, "lon": -90.0812,  "indoor": True,  "name": "Caesars Superdome"},
    "NYG": {"lat": 40.8135, "lon": -74.0745,  "indoor": False, "name": "MetLife Stadium"},
    "NYJ": {"lat": 40.8135, "lon": -74.0745,  "indoor": False, "name": "MetLife Stadium"},
    "PHI": {"lat": 39.9008, "lon": -75.1675,  "indoor": False, "name": "Lincoln Financial Field"},
    "PIT": {"lat": 40.4468, "lon": -80.0158,  "indoor": False, "name": "Acrisure Stadium"},
    "SEA": {"lat": 47.5952, "lon": -122.3316, "indoor": False, "name": "Lumen Field"},
    "SF":  {"lat": 37.4030, "lon": -121.9700, "indoor": False, "name": "Levi's Stadium"},
    "TB":  {"lat": 27.9759, "lon": -82.5033,  "indoor": False, "name": "Raymond James Stadium"},
    "TEN": {"lat": 36.1665, "lon": -86.7713,  "indoor": False, "name": "Nissan Stadium"},
    "WAS": {"lat": 38.9077, "lon": -76.8645,  "indoor": False, "name": "Northwest Stadium"},
}

# Players, user_id, league, and NFL state are now cached by sleeper/cache.py
# with per data class TTLs (ADR 001). The caches below are still local because
# their consumers have not been migrated to the core yet; they are bounded by
# the lifetime of a single stdio session, which is acceptable for this adapter.
_weekly_stats_cache: Dict[tuple, Dict[str, Any]] = {}
_season_stats_cache: Dict[str, Dict[str, Any]] = {}
_weekly_proj_cache: Dict[tuple, Dict[str, Any]] = {}
_schedule_cache: Dict[str, Dict[str, Any]] = {}

# ─────────────────────────────────────────────────────────────────────────────
# The Chrysoloras Gang — Full-PPR scoring rules
# ─────────────────────────────────────────────────────────────────────────────

# Direct stat-field multipliers (Sleeper stat key → points per unit)
# Scoring now lives in sleeper/scoring.py (ADR 001). Re-exported here so any
# existing reference inside this module keeps working unchanged.
from sleeper.scoring import (  # noqa: E402
    SCORING_RULES,
    FG_SCORING,
    DEF_SCORING,
    DEF_PA_TIERS,
    DEF_YA_TIERS,
    calculate_fantasy_points,
    _calculate_def_points,
    _derive_milestone_bonuses,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared API helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get(endpoint: str, params: Optional[Union[Dict, List]] = None) -> Any:
    """
    GET against the Sleeper API via the shared pooled client (ADR 001).

    Previously opened a brand new AsyncClient per request, so no connection was
    ever reused. Now goes through sleeper.client, which adds pooling, retry with
    backoff, and a rate limit guard.
    """
    return await core_client.sleeper_get(endpoint, params)


def _handle_error(exc: Exception) -> str:
    """User friendly error string. Shared with the dashboard via sleeper.client."""
    return core_client.describe_error(exc)


def _sleeper_to_espn_abbr(team: str) -> str:
    return ESPN_ABBR_OVERRIDES.get(team, team)


def _espn_to_sleeper_abbr(team: str) -> str:
    for sleeper_abbr, espn_abbr in ESPN_ABBR_OVERRIDES.items():
        if espn_abbr == team:
            return sleeper_abbr
    return team


async def _fetch_espn_week(season: str, week: int) -> List[Dict[str, Any]]:
    """
    Fetch one week's real NFL schedule from ESPN's public scoreboard endpoint.
    Sleeper's API has no schedule endpoint, so this is the source of truth for
    opponents, game dates, and venues (used by SOS and weather tools).

    Returns a list of games, each: {home, away, date_utc, venue_name, indoor, week}
    Team abbreviations are normalized to Sleeper's convention (e.g. WSH -> WAS).
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{ESPN_BASE}/scoreboard",
                params={"seasontype": 2, "week": week, "year": season},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    games = []
    for event in data.get("events", []):
        try:
            comp = event["competitions"][0]
            venue = comp.get("venue", {})
            home = away = None
            for c in comp.get("competitors", []):
                abbr = _espn_to_sleeper_abbr(c["team"]["abbreviation"])
                if c.get("homeAway") == "home":
                    home = abbr
                else:
                    away = abbr
            if not home or not away:
                continue
            games.append({
                "home": home,
                "away": away,
                "date_utc": comp.get("date") or event.get("date"),
                "venue_name": venue.get("fullName", "Unknown venue"),
                "indoor": bool(venue.get("indoor", False)),
                "week": week,
            })
        except Exception:
            continue
    return games


async def _get_full_schedule(season: str) -> Dict[str, Any]:
    """
    Build (and cache) the full-season schedule: games by week, each team's
    opponent/home-away per week, and each team's bye week.

    Fetches weeks 1-18 from ESPN concurrently. Cached per season for the session.
    """
    if season in _schedule_cache:
        return _schedule_cache[season]

    weeks_data = await _parallel_fetch(*[_fetch_espn_week(season, wk) for wk in range(1, 19)])

    games_by_week: Dict[int, List[Dict[str, Any]]] = {}
    team_week_opponent: Dict[str, Dict[int, Dict[str, Any]]] = {}
    all_teams = set(STADIUM_INFO.keys())
    teams_seen_by_week: Dict[int, set] = {}

    for week, games in enumerate(weeks_data, start=1):
        games_by_week[week] = games
        teams_seen_by_week[week] = set()
        for g in games:
            teams_seen_by_week[week].add(g["home"])
            teams_seen_by_week[week].add(g["away"])
            team_week_opponent.setdefault(g["home"], {})[week] = {
                "opponent": g["away"], "is_home": True,
                "date_utc": g["date_utc"], "venue_name": g["venue_name"], "indoor": g["indoor"],
            }
            team_week_opponent.setdefault(g["away"], {})[week] = {
                "opponent": g["home"], "is_home": False,
                "date_utc": g["date_utc"], "venue_name": g["venue_name"], "indoor": g["indoor"],
            }

    team_bye: Dict[str, Optional[int]] = {}
    for team in all_teams:
        bye = None
        for week in range(1, 19):
            if teams_seen_by_week.get(week) and team not in teams_seen_by_week[week]:
                bye = week
                break
        team_bye[team] = bye

    result = {
        "games_by_week": games_by_week,
        "team_week_opponent": team_week_opponent,
        "team_bye": team_bye,
    }
    _schedule_cache[season] = result
    return result


async def _get_weather_forecast(lat: float, lon: float, target_date_iso: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a daily weather forecast for a specific date/location using Open-Meteo
    (free, no API key). Open-Meteo only forecasts ~16 days out; returns None if
    the target date is outside that window or in the past.
    """
    try:
        target_date = target_date_iso[:10]
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(OPEN_METEO_BASE, params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,windspeed_10m_max",
                "temperature_unit": "fahrenheit",
                "windspeed_unit": "mph",
                "forecast_days": 16,
                "timezone": "auto",
            })
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    if target_date not in dates:
        return None
    idx = dates.index(target_date)
    return {
        "temp_max_f": daily.get("temperature_2m_max", [None])[idx],
        "temp_min_f": daily.get("temperature_2m_min", [None])[idx],
        "precip_prob_pct": daily.get("precipitation_probability_max", [None])[idx],
        "wind_max_mph": daily.get("windspeed_10m_max", [None])[idx],
    }


def _weather_fantasy_note(weather: Dict[str, Any]) -> str:
    """Short heuristic note on fantasy impact for a forecast."""
    notes = []
    wind = weather.get("wind_max_mph") or 0
    precip = weather.get("precip_prob_pct") or 0
    temp_min = weather.get("temp_min_f")
    if wind >= 20:
        notes.append("high wind — passing/kicking accuracy at risk")
    elif wind >= 15:
        notes.append("breezy — mild passing/kicking impact")
    if precip >= 60:
        notes.append("high precip chance — favors rushing, ball security risk")
    if temp_min is not None and temp_min <= 20:
        notes.append("extreme cold — kicking distance/grip affected")
    return "; ".join(notes) if notes else "no significant weather concerns"


async def _get_players() -> Dict[str, Any]:
    """
    Full Sleeper NFL player map (~5 MB), via the shared core.

    The core caches this in memory and on disk with a 24 hour TTL, matching
    Sleeper's documented guidance to fetch it at most once per day. Previously
    this was an unbounded module global refetched on every process start.
    """
    return await core_league.get_players()


async def _get_user_id() -> str:
    """Resolve the configured Sleeper user_id, via the shared core."""
    return await core_league.get_user_id()


async def _get_league() -> Dict[str, Any]:
    """
    Resolve the league object, via the shared core.

    Identity is configurable now (SLEEPER_LEAGUE_ID / SLEEPER_LEAGUE_MATCH)
    rather than a hardcoded name match, per ADR 001 action item 12.
    """
    return await core_league.get_league()


async def _get_taken_player_ids(league_id: str) -> set:
    """Return the set of player_ids currently on any roster in the league."""
    rosters = await _get(f"/league/{league_id}/rosters")
    taken: set = set()
    for roster in rosters:
        for slot in ("players", "starters", "reserve"):
            for pid in roster.get(slot) or []:
                taken.add(pid)
    return taken


async def _get_current_week() -> tuple:
    """
    Return (season_type, week, season) from Sleeper's /state/nfl.

    Now on a 5 minute TTL via the core rather than cached forever. The old
    infinite cache meant a long lived process would believe it was still week 1
    in December, which is harmless for a short stdio session and a real bug for
    the dashboard.
    """
    s = await core_league.get_nfl_state()
    return (
        s.get("season_type", "regular"),
        s.get("display_week", 1),
        s.get("league_season", CURRENT_SEASON),
    )


async def _fetch_projections_for_week(season: str, week: int, positions: List[str]) -> Dict[str, Dict]:
    """
    Fetch per-player weekly projections for the given positions and week.
    Correct Sleeper URL shape: /projections/nfl/regular/{season}/{week} — season_type
    is a PATH segment, not a query parameter (a common source of silently-empty results).
    Cached per (season, week) since positions differ but the endpoint returns all requested
    positions in one call.
    """
    cache_key = (season, week, tuple(sorted(positions)))
    if cache_key in _weekly_proj_cache:
        return _weekly_proj_cache[cache_key]

    params: List[tuple] = [("position[]", pos) for pos in positions]
    try:
        data = await _get(f"/projections/nfl/regular/{season}/{week}", params=params)
    except Exception:
        data = {}
    _weekly_proj_cache[cache_key] = data or {}
    return data or {}


async def _fetch_projections(positions: List[str]) -> Dict[str, Dict]:
    """
    Fetch this week's projections for the given positions, using the current
    NFL week from /state/nfl. Falls back to Week 1 of CURRENT_SEASON if the
    season hasn't started (state season_type == 'pre') and no projections exist yet.
    """
    season_type, week, season = await _get_current_week()
    data = await _fetch_projections_for_week(season, week, positions)
    if not data and season_type != "regular":
        # Pre/post-season: try Week 1 of the regular season as a preview
        data = await _fetch_projections_for_week(season, 1, positions)
    return data


async def _get_weekly_stats(season: str, week: int) -> Dict[str, Any]:
    """
    Fetch (and cache) the full-league weekly stats blob for a given week.
    Correct URL shape: /stats/nfl/regular/{season}/{week} (season_type is a path segment).
    Includes per-player box scores, snap counts (off_snp/tm_off_snp), and for DEF
    entries, position-specific fantasy points allowed (fan_pts_allow_qb/rb/wr/te/k).
    """
    key = (season, week)
    if key not in _weekly_stats_cache:
        try:
            _weekly_stats_cache[key] = await _get(f"/stats/nfl/regular/{season}/{week}")
        except Exception:
            _weekly_stats_cache[key] = {}
    return _weekly_stats_cache[key]


async def _get_season_stats_all(season: str) -> Dict[str, Any]:
    """
    Fetch (and cache) full-league season-aggregate stats.
    Correct URL shape: /stats/nfl/regular/{season} (no week segment).
    """
    if season not in _season_stats_cache:
        try:
            _season_stats_cache[season] = await _get(f"/stats/nfl/regular/{season}")
        except Exception:
            _season_stats_cache[season] = {}
    return _season_stats_cache[season]


def _player_display_name(player: Dict[str, Any]) -> str:
    first = player.get("first_name", "")
    last = player.get("last_name", "")
    return f"{first} {last}".strip() or player.get("full_name", "Unknown")


def _resolve_positions(position: "PositionEnum") -> List[str]:
    """Expand FLEX into the three eligible positions."""
    if position == PositionEnum.FLEX:
        return ["RB", "WR", "TE"]
    return [position.value]

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic input models
# ─────────────────────────────────────────────────────────────────────────────

class PositionEnum(str, Enum):
    QB   = "QB"
    RB   = "RB"
    WR   = "WR"
    TE   = "TE"
    K    = "K"
    DEF  = "DEF"
    FLEX = "FLEX"


class GetAvailablePlayersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    position: PositionEnum = Field(
        ...,
        description="Position to filter: QB, RB, WR, TE, K, DEF, or FLEX (W/R/T eligible)"
    )
    limit: int = Field(
        default=20, ge=1, le=50,
        description="Number of players to return (1–50, default 20)"
    )


class GetWaiverRecommendationsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    positions: Optional[List[PositionEnum]] = Field(
        default=None,
        description="Positions to scan (default: QB, RB, WR, TE). E.g. ['RB', 'WR']"
    )
    limit: int = Field(
        default=15, ge=1, le=30,
        description="Total recommendations to return (1–30, default 15)"
    )


class GetPlayerStatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    player_name: str = Field(
        ..., min_length=2, max_length=100,
        description="Player's full or partial name, e.g. 'Patrick Mahomes' or 'Jefferson'"
    )


class GetTradeTargetsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    limit: int = Field(
        default=10, ge=1, le=25,
        description="Number of trade targets to surface (default 10)"
    )


class GetStrengthOfScheduleInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    player_name: str = Field(
        ..., min_length=2, max_length=100,
        description="Player's full or partial name, e.g. 'Bijan Robinson' or 'Jefferson'"
    )
    weeks_ahead: int = Field(
        default=8, ge=1, le=18,
        description="How many upcoming weeks of schedule to analyze (1–18, default 8)"
    )


class GetWeatherReportInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    player_name: Optional[str] = Field(
        default=None, max_length=100,
        description="Optional player name to check just their team's next game. "
                     "If omitted, checks the next upcoming game for every team on your roster."
    )


class GetDraftBestAvailableInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    position: Optional[PositionEnum] = Field(
        default=None,
        description="Filter to one position (QB/RB/WR/TE/K/DEF/FLEX). Omit for all positions."
    )
    limit: int = Field(
        default=20, ge=1, le=50,
        description="Number of players to return (1–50, default 20)"
    )


class GetSnapReportInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    player_name: Optional[str] = Field(
        default=None, max_length=100,
        description="Optional player name for a snap-share/usage trend report. "
                     "If omitted, reports on every skill-position (QB/RB/WR/TE) player on your roster."
    )
    weeks_back: int = Field(
        default=4, ge=1, le=10,
        description="Number of recent completed weeks to include in the trend (1–10, default 4)"
    )

# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — get_my_team
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_my_team",
    annotations={
        "title": "Get My Team Roster",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def sleeper_get_my_team() -> str:
    """Show GronkQuixote's current roster in The Chrysoloras Gang league.

    Retrieves the user's roster from Sleeper and groups players by slot:
    starters, bench, and IR. Also shows the team's current win/loss record
    and total points scored.

    Returns:
        str: Markdown roster report with player name, position, NFL team,
             and slot for each player, plus record and points summary.

    Example prompts:
        - "Show me my team"
        - "Who's on my roster?"
        - "What players do I have?"
    """
    try:
        user_id = await _get_user_id()
        league = await _get_league()
        league_id = league["league_id"]

        rosters, users, players = await _parallel_fetch(
            _get(f"/league/{league_id}/rosters"),
            _get(f"/league/{league_id}/users"),
            _get_players(),
        )

        user_map = {u["user_id"]: u.get("display_name", "?") for u in users}
        my_roster = next((r for r in rosters if r.get("owner_id") == user_id), None)

        if not my_roster:
            return (
                f"Error: Could not find GronkQuixote's roster in league '{league.get('name')}'.\n"
                f"League ID: {league_id}"
            )

        starters = my_roster.get("starters") or []
        all_players = my_roster.get("players") or []
        ir = my_roster.get("reserve") or []
        bench = [p for p in all_players if p not in starters and p not in ir]

        def fmt(pid: str) -> str:
            if not pid or pid == "0":
                return "*(empty slot)*"
            p = players.get(pid, {})
            name = _player_display_name(p)
            pos = p.get("position", "?")
            team = p.get("team") or "FA"
            return f"**{name}** — {pos}, {team}"

        settings = my_roster.get("settings", {})
        wins = settings.get("wins", 0)
        losses = settings.get("losses", 0)
        ties = settings.get("ties", 0)
        fpts = settings.get("fpts", 0)
        fpts_dec = settings.get("fpts_decimal", 0)

        lines = [
            f"# 📋 GronkQuixote — The Chrysoloras Gang",
            f"**Record:** {wins}–{losses}–{ties}  |  **Points For:** {fpts}.{fpts_dec:02d}",
            "",
            "## Starters",
        ]
        for pid in starters:
            lines.append(f"- {fmt(pid)}")

        lines += ["", "## Bench"]
        for pid in bench:
            lines.append(f"- {fmt(pid)}")

        if ir:
            lines += ["", "## IR"]
            for pid in ir:
                lines.append(f"- {fmt(pid)}")

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Composite ranking — folds SOS and snap trend into the ranking itself
# ─────────────────────────────────────────────────────────────────────────────

async def _build_def_rank_map(pos_key: str, def_season: str) -> Dict[str, int]:
    """Rank all 32 teams by fan_pts_allow_<pos_key>. Rank 1 = allows the most
    (easiest matchup), rank 32 = allows the least (hardest matchup)."""
    def_stats = await _get_season_stats_all(def_season)
    team_allowed = [
        (t, def_stats.get(t, {}).get(f"fan_pts_allow_{pos_key}"))
        for t in STADIUM_INFO
    ]
    team_allowed = [(t, v) for t, v in team_allowed if v is not None]
    team_allowed.sort(key=lambda x: x[1], reverse=True)
    return {t: i + 1 for i, (t, _) in enumerate(team_allowed)}


async def _resolve_def_season() -> str:
    """Use current-season defensive stats once games have been played, else fall back."""
    current = await _get_season_stats_all(CURRENT_SEASON)
    has_data = any(current.get(t, {}).get("fan_pts_allow_rb") for t in STADIUM_INFO)
    return CURRENT_SEASON if has_data else STATS_SEASON


def _sos_bonus_for_team(team: str, rank_map: Dict[str, int], schedule: Dict[str, Any],
                         start_week: int, weeks_ahead: int) -> tuple:
    """Average opponent matchup rank over the lookahead window -> (avg_rank, bonus)."""
    team_sched = schedule["team_week_opponent"].get(team, {})
    ranks = []
    for wk in range(start_week, start_week + weeks_ahead):
        info = team_sched.get(wk)
        if not info:
            continue
        r = rank_map.get(info["opponent"])
        if r is not None:
            ranks.append(r)
    if not ranks:
        return None, 0.0
    avg_rank = sum(ranks) / len(ranks)
    total = len(rank_map) or 32
    midpoint = (total + 1) / 2
    bonus = (midpoint - avg_rank) / midpoint * SOS_MAX_BONUS
    return avg_rank, bonus


def _snap_trend_bonus_for_player(pid: str, weekly_blobs: Dict[int, Dict[str, Any]],
                                  weeks: List[int]) -> tuple:
    """Snap-share delta (first available week -> last) across the trend window -> (delta_pct, bonus)."""
    snaps = []
    for wk in weeks:
        stat = weekly_blobs.get(wk, {}).get(pid, {})
        off_snp = stat.get("off_snp")
        tm_off_snp = stat.get("tm_off_snp")
        if off_snp is not None and tm_off_snp:
            snaps.append(off_snp / tm_off_snp * 100)
    if len(snaps) < 2:
        return None, 0.0
    delta = snaps[-1] - snaps[0]
    capped = max(-SNAP_TREND_CAP_PCT, min(SNAP_TREND_CAP_PCT, delta))
    bonus = capped / SNAP_TREND_CAP_PCT * SNAP_TREND_MAX_BONUS
    return delta, bonus


async def _compute_composite_scores(candidates: List[tuple], proj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Rank candidates by composite_score = proj_pts + sos_bonus + snap_trend_bonus,
    instead of raw projected points alone.

    candidates: [(player_id, player_dict), ...]
    proj: this week's projections blob (player_id -> stat dict)

    Returns a list of dicts (one per candidate) sorted by composite score descending,
    each carrying the individual components so callers can show a transparent breakdown:
    {pid, player, position, team, proj_pts, avg_opp_rank, sos_bonus, snap_delta, snap_bonus, composite}
    """
    season_type, current_week, season = await _get_current_week()
    start_week = current_week if season_type == "regular" else 1

    schedule = await _get_full_schedule(CURRENT_SEASON)
    def_season = await _resolve_def_season()

    positions_present = {p.get("position") for _, p in candidates}
    rank_maps: Dict[str, Dict[str, int]] = {}
    for pos in positions_present:
        pos_key = DEF_ALLOWED_POS_KEY.get(pos)
        if pos_key:
            rank_maps[pos] = await _build_def_rank_map(pos_key, def_season)

    # Snap trend uses recent COMPLETED weeks only
    if season_type == "regular" and current_week > 1:
        trend_weeks = list(range(max(1, current_week - SNAP_TREND_WEEKS), current_week))
        trend_season = season
    else:
        trend_weeks = list(range(max(1, 18 - SNAP_TREND_WEEKS + 1), 19))
        trend_season = STATS_SEASON
    weekly_blobs = dict(zip(
        trend_weeks,
        await _parallel_fetch(*[_get_weekly_stats(trend_season, w) for w in trend_weeks]),
    ))

    results = []
    for pid, p in candidates:
        pos = p.get("position", "")
        team = p.get("team")
        proj_pts = calculate_fantasy_points(proj.get(pid, {}), pos)

        avg_rank, sos_bonus = (None, 0.0)
        if pos in rank_maps and team:
            avg_rank, sos_bonus = _sos_bonus_for_team(team, rank_maps[pos], schedule, start_week, SOS_LOOKAHEAD_WEEKS)

        snap_delta, snap_bonus = (None, 0.0)
        if pos in SNAP_TREND_ELIGIBLE_POS:
            snap_delta, snap_bonus = _snap_trend_bonus_for_player(pid, weekly_blobs, trend_weeks)

        composite = proj_pts + sos_bonus + snap_bonus
        results.append({
            "pid": pid, "player": p, "position": pos, "team": team,
            "proj_pts": proj_pts, "avg_opp_rank": avg_rank, "sos_bonus": sos_bonus,
            "snap_delta": snap_delta, "snap_bonus": snap_bonus, "composite": composite,
        })

    results.sort(key=lambda x: x["composite"], reverse=True)
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — get_available_players
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_available_players",
    annotations={
        "title": "Get Available Players by Position",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_available_players(params: GetAvailablePlayersInput) -> str:
    """List the top unrostered players for a position, ranked by composite score.

    Fetches all players not on any roster in The Chrysoloras Gang and ranks them
    by a composite score rather than raw projected points alone:

        composite = proj_pts + strength_of_schedule_bonus + snap_trend_bonus

    The SOS bonus (+/- up to 2.0 pts) rewards players facing weak defenses over
    the next few weeks; the snap trend bonus (+/- up to 3.0 pts) rewards players
    whose offensive snap share is climbing (an early breakout signal) and
    penalizes those whose role is shrinking. This surfaces players the field
    hasn't caught up to yet, not just this week's raw projection.

    Args:
        params (GetAvailablePlayersInput):
            - position (PositionEnum): QB | RB | WR | TE | K | DEF | FLEX
            - limit (int): Players to return (1–50, default 20)

    Returns:
        str: Markdown table of top available players with rank, name, position,
             NFL team, composite score, raw projected points, SOS matchup rank,
             snap-share trend, and ownership %.

    Error response:
        "Error: <message>" if the API call fails.
        "No available <pos> players found." if the position has no free agents.

    Example prompts:
        - "What top RBs are available?"
        - "Show me the best WRs on waivers"
        - "Who are the top 10 available TEs, factoring in schedule and role trend?"
    """
    try:
        league = await _get_league()
        league_id = league["league_id"]

        taken, players = await _parallel_fetch(
            _get_taken_player_ids(league_id),
            _get_players(),
        )

        pos_filter = _resolve_positions(params.position)

        candidates = [
            (pid, p) for pid, p in players.items()
            if p.get("position") in pos_filter
            and p.get("active", False)
            and pid not in taken
            and p.get("team")
        ]

        if not candidates:
            return f"No available {params.position.value} players found in The Chrysoloras Gang."

        projections = await _fetch_projections(pos_filter)
        ranked = await _compute_composite_scores(candidates, projections)
        ranked = ranked[: params.limit]

        lines = [
            f"# Top Available {params.position.value} — The Chrysoloras Gang (Full PPR)",
            "*Ranked by composite score: projection + strength of schedule + snap trend*",
            "",
            f"{'#':<3} {'Player':<22} {'Pos':<4} {'Team':<5} {'Comp':>6} {'Proj':>6} {'SOS':>9} {'Snap Δ':>8} {'Own%':>6}",
            "─" * 78,
        ]
        for rank, r in enumerate(ranked, 1):
            name = _player_display_name(r["player"])[:22]
            team = (r["team"] or "FA")[:4]
            own_pct = r["player"].get("ownership", {}).get("percentage_owned", 0.0)
            sos_str = f"{r['avg_opp_rank']:.0f}/32" if r["avg_opp_rank"] is not None else "—"
            snap_str = f"{r['snap_delta']:+.0f}%" if r["snap_delta"] is not None else "—"
            lines.append(
                f"{rank:<3} {name:<22} {r['position']:<4} {team:<5} "
                f"{r['composite']:>6.1f} {r['proj_pts']:>6.1f} {sos_str:>9} {snap_str:>8} {own_pct:>5.1f}%"
            )

        lines += [
            "",
            "*SOS = avg. matchup rank over next "
            f"{SOS_LOOKAHEAD_WEEKS} weeks (1=easiest/32=hardest). "
            f"Snap Δ = offensive snap-share change over the last {SNAP_TREND_WEEKS} completed weeks.*",
        ]

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — get_waiver_recommendations
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_waiver_recommendations",
    annotations={
        "title": "Get Waiver Wire Recommendations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_waiver_recommendations(params: GetWaiverRecommendationsInput) -> str:
    """Identify the best waiver wire / free-agent pickups for The Chrysoloras Gang.

    Cross-references league rosters to find unowned players with meaningful
    projected output (> 2 raw projected pts), then ranks survivors by a
    composite score: proj_pts + strength_of_schedule_bonus + snap_trend_bonus.
    A player trending up in snap share facing a soft upcoming schedule can
    rank ahead of a higher-projected player who's losing his role or facing a
    brutal slate — this is meant to catch adds before the field does.

    Waiver schedule reminder:
      - Claims clear:   Wednesday 1 AM MDT
      - Process times:  Wednesday / Thursday / Friday at 8 AM MDT

    Args:
        params (GetWaiverRecommendationsInput):
            - positions (List[PositionEnum]): Positions to include (default: QB, RB, WR, TE)
            - limit (int): Recommendations to return (1–30, default 15)

    Returns:
        str: Markdown list of top pickups with name, team, position, composite
             score, raw projected points, SOS matchup rank, snap-share trend,
             and ownership %.
    """
    try:
        league = await _get_league()
        league_id = league["league_id"]

        taken, players = await _parallel_fetch(
            _get_taken_player_ids(league_id),
            _get_players(),
        )

        positions = (
            [p.value for p in params.positions]
            if params.positions
            else ["QB", "RB", "WR", "TE"]
        )

        candidates = [
            (pid, p) for pid, p in players.items()
            if p.get("position") in positions
            and p.get("active", False)
            and pid not in taken
            and p.get("team")
        ]

        projections = await _fetch_projections(positions)
        ranked_all = await _compute_composite_scores(candidates, projections)
        ranked = [r for r in ranked_all if r["proj_pts"] > 2.0][: params.limit]

        lines = [
            "# 🏈 Waiver Wire Recommendations — The Chrysoloras Gang",
            "*Ranked by composite score: projection + strength of schedule + snap trend*",
            "",
            "> **Waivers clear:** Wednesday 1 AM MDT",
            "> **Process:** Wed / Thu / Fri at 8 AM MDT",
            "",
            f"{'#':<3} {'Player':<22} {'Pos':<4} {'Team':<5} {'Comp':>6} {'Proj':>6} {'SOS':>9} {'Snap Δ':>8} {'Own%':>6}",
            "─" * 78,
        ]
        for rank, r in enumerate(ranked, 1):
            name = _player_display_name(r["player"])[:22]
            team = (r["team"] or "FA")[:4]
            own_pct = r["player"].get("ownership", {}).get("percentage_owned", 0.0)
            sos_str = f"{r['avg_opp_rank']:.0f}/32" if r["avg_opp_rank"] is not None else "—"
            snap_str = f"{r['snap_delta']:+.0f}%" if r["snap_delta"] is not None else "—"
            lines.append(
                f"{rank:<3} {name:<22} {r['position']:<4} {team:<5} "
                f"{r['composite']:>6.1f} {r['proj_pts']:>6.1f} {sos_str:>9} {snap_str:>8} {own_pct:>5.1f}%"
            )

        if not ranked:
            lines.append("No players with meaningful projections found. Projections may not yet be available for this week.")
        else:
            lines += [
                "",
                f"*SOS = avg. matchup rank over next {SOS_LOOKAHEAD_WEEKS} weeks (1=easiest/32=hardest). "
                f"Snap Δ = offensive snap-share change over the last {SNAP_TREND_WEEKS} completed weeks.*",
            ]

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 4 — get_league_standings
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_league_standings",
    annotations={
        "title": "Get League Standings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def sleeper_get_league_standings() -> str:
    """Show current standings for all 12 teams in The Chrysoloras Gang.

    Fetches all rosters and their season records, sorts by wins (tiebroken
    by total points scored), and marks the playoff picture. Top 6 teams
    make the playoffs; playoffs start Week 15.

    Returns:
        str: Markdown standings table with rank, team name, record,
             points for, points against, and playoff status.
             GronkQuixote's team is marked with ◀.

    Example prompts:
        - "What are the standings?"
        - "Who's in the playoffs right now?"
        - "What's my record?"
    """
    try:
        user_id = await _get_user_id()
        league = await _get_league()
        league_id = league["league_id"]

        rosters, users = await _parallel_fetch(
            _get(f"/league/{league_id}/rosters"),
            _get(f"/league/{league_id}/users"),
        )

        user_map = {u["user_id"]: u.get("display_name", "Unknown") for u in users}

        standings = []
        for roster in rosters:
            s = roster.get("settings", {})
            wins = s.get("wins", 0)
            losses = s.get("losses", 0)
            ties = s.get("ties", 0)
            pf = s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100
            pa = s.get("fpts_against", 0) + s.get("fpts_against_decimal", 0) / 100
            owner_id = roster.get("owner_id", "")
            team = user_map.get(owner_id, f"Team {roster.get('roster_id')}")
            standings.append(
                dict(team=team, wins=wins, losses=losses, ties=ties, pf=pf, pa=pa, is_me=(owner_id == user_id))
            )

        standings.sort(key=lambda x: (-x["wins"], -x["pf"]))

        lines = [
            "# 🏆 The Chrysoloras Gang — Standings",
            f"*(Top 6 make playoffs | Playoffs start Week 15)*",
            "",
            f"{'Rk':<4} {'Team':<22} {'W-L-T':<9} {'PF':>8} {'PA':>8}  Playoff",
            "─" * 64,
        ]
        for rank, s in enumerate(standings, 1):
            record = f"{s['wins']}-{s['losses']}-{s['ties']}"
            playoff = "✅ IN " if rank <= 6 else "❌ OUT"
            me = " ◀" if s["is_me"] else ""
            team = (s["team"][:20] + ".." if len(s["team"]) > 22 else s["team"])
            lines.append(f"{rank:<4} {team:<22} {record:<9} {s['pf']:>8.1f} {s['pa']:>8.1f}  {playoff}{me}")

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 5 — get_player_stats
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_player_stats",
    annotations={
        "title": "Get Player Stats and Projections",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def sleeper_get_player_stats(params: GetPlayerStatsInput) -> str:
    """Fetch detailed season stats and projected points for a named player.

    Searches the Sleeper player database for the given name (partial matches
    accepted), fetches their 2025 season statistics, and calculates total
    fantasy points under Chrysoloras Gang full-PPR scoring. Also shows
    current-season projections if available.

    Args:
        params (GetPlayerStatsInput):
            - player_name (str): Full or partial player name,
              e.g. 'Patrick Mahomes', 'Jefferson', 'Kelce'

    Returns:
        str: Markdown report with player bio, position-relevant season stats,
             calculated fantasy points, and current projections.

        If multiple players match: lists candidates and asks to be more specific.
        If no player found: returns a "not found" message.

    Example prompts:
        - "Get stats for Justin Jefferson"
        - "How did Lamar Jackson do last season?"
        - "What are Travis Kelce's projections?"
    """
    try:
        players = await _get_players()
        query = params.player_name.lower().strip()

        matches = [
            (pid, p) for pid, p in players.items()
            if query in _player_display_name(p).lower()
        ]

        if not matches:
            return (
                f"No player found matching '{params.player_name}'.\n"
                "Try a different spelling or use just the last name."
            )

        if len(matches) > 8:
            names = ", ".join(_player_display_name(p) for _, p in matches[:10])
            return (
                f"Too many players match '{params.player_name}' ({len(matches)} results).\n"
                f"Examples: {names}\nPlease be more specific."
            )

        # Best match: prefer exact full-name match, then shortest name
        pid, player = min(
            matches,
            key=lambda x: (
                0 if _player_display_name(x[1]).lower() == query else 1,
                len(_player_display_name(x[1])),
            ),
        )
        position = player.get("position", "?")
        name = _player_display_name(player)
        team = player.get("team") or "Free Agent"
        age = player.get("age", "N/A")
        exp = player.get("years_exp", "N/A")
        injury_status = player.get("injury_status") or "Healthy"

        # Fetch season stats and projections concurrently
        pos_filter = _resolve_positions(
            PositionEnum(position) if position in PositionEnum.__members__ else PositionEnum.WR
        )

        season_stats_raw, projections = await _parallel_fetch(
            _get_season_stats(pid),
            _fetch_projections(pos_filter),
        )

        season_stats = season_stats_raw.get("stats", season_stats_raw) if isinstance(season_stats_raw, dict) else {}
        proj = projections.get(pid, {})

        season_pts = calculate_fantasy_points(season_stats, position)
        proj_pts = calculate_fantasy_points(proj, position)

        lines = [
            f"# {name}",
            f"**{position} | {team}** | Age: {age} | Exp: {exp} yr | Status: {injury_status}",
            "",
            f"## {STATS_SEASON} Season — **{season_pts:.1f} fantasy pts** (Full PPR)",
        ]
        lines += _format_stats_block(season_stats, position)

        if proj:
            lines += [
                "",
                f"## Current Projections — **{proj_pts:.1f} fantasy pts** (this week)",
            ]
            lines += _format_stats_block(proj, position)
        else:
            lines += ["", "*No projections available yet for this week.*"]

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)


async def _get_season_stats(player_id: str) -> Dict[str, Any]:
    """Look up one player's aggregate season stats from the cached season blob."""
    all_stats = await _get_season_stats_all(STATS_SEASON)
    return all_stats.get(player_id, {})


def _format_stats_block(stats: Dict[str, Any], position: str) -> List[str]:
    """Return position-relevant stat lines for a player."""
    if not stats:
        return ["*No stats available.*"]

    lines = []
    if position == "QB":
        lines = [
            f"- **Passing:** {stats.get('pass_yd', 0):.0f} yds, {stats.get('pass_td', 0):.0f} TD, "
            f"{stats.get('pass_int', 0):.0f} INT, {stats.get('pass_cmp', 0):.0f}/{stats.get('pass_att', 0):.0f} cmp",
            f"- **Rushing:** {stats.get('rush_yd', 0):.0f} yds, {stats.get('rush_td', 0):.0f} TD, "
            f"{stats.get('rush_att', 0):.0f} att",
        ]
    elif position == "RB":
        lines = [
            f"- **Rushing:** {stats.get('rush_yd', 0):.0f} yds, {stats.get('rush_td', 0):.0f} TD, "
            f"{stats.get('rush_att', 0):.0f} att",
            f"- **Receiving:** {stats.get('rec', 0):.0f} rec / {stats.get('rec_tgt', 0):.0f} tgt, "
            f"{stats.get('rec_yd', 0):.0f} yds, {stats.get('rec_td', 0):.0f} TD",
        ]
    elif position in ("WR", "TE"):
        lines = [
            f"- **Receiving:** {stats.get('rec', 0):.0f} rec / {stats.get('rec_tgt', 0):.0f} tgt, "
            f"{stats.get('rec_yd', 0):.0f} yds, {stats.get('rec_td', 0):.0f} TD",
            f"- **Rushing:** {stats.get('rush_yd', 0):.0f} yds, {stats.get('rush_td', 0):.0f} TD",
        ]
    elif position == "K":
        lines = [
            f"- **FG Made:** 0-19: {stats.get('fgm_0_19', 0):.0f} | 20-29: {stats.get('fgm_20_29', 0):.0f} | "
            f"30-39: {stats.get('fgm_30_39', 0):.0f} | 40-49: {stats.get('fgm_40_49', 0):.0f} | "
            f"50+: {stats.get('fgm_50p', 0):.0f}",
            f"- **FG Missed:** {stats.get('fgmiss', 0):.0f}",
            f"- **PAT:** {stats.get('xpm', 0):.0f} made, {stats.get('xpmiss', 0):.0f} missed",
        ]
    elif position == "DEF":
        lines = [
            f"- **Sacks:** {stats.get('sack', 0):.0f} | **INTs:** {stats.get('def_int', 0):.0f} | "
            f"**Fum Rec:** {stats.get('def_fum_rec', 0):.0f} | **FF:** {stats.get('def_ff', 0):.0f}",
            f"- **Def TD:** {stats.get('def_td', 0):.0f} | **Safeties:** {stats.get('safe', 0):.0f} | "
            f"**Blk Kick:** {stats.get('blk_kick', 0):.0f}",
            f"- **Points Allowed:** {stats.get('pts_allow', 'N/A')} | **Yards Allowed:** {stats.get('yds_allow', 'N/A')}",
        ]

    return lines or ["*Stats format not recognised for this position.*"]

# ─────────────────────────────────────────────────────────────────────────────
# Tool 6 — get_trade_targets
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_trade_targets",
    annotations={
        "title": "Get Trade Targets and Waiver Value Plays",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_trade_targets(params: GetTradeTargetsInput) -> str:
    """Surface undervalued free agents worth targeting on waivers or in trades.

    Identifies players who are producing (or projected to produce) more than
    their ownership percentage suggests. Sorts by a value score:
      Value Score = Projected Points ÷ Ownership% (higher = more undervalued).

    Players already rostered in the league are excluded. Only players with
    meaningful projected output (> 2 pts) are included.

    ⚠️ Trade deadline is Week 12. Act before then!

    Args:
        params (GetTradeTargetsInput):
            - limit (int): Players to return (1–25, default 10)

    Returns:
        str: Markdown table with player name, position, NFL team, projected
             weekly points, ownership %, and value score.

    Example prompts:
        - "Who are my best trade targets?"
        - "Find me undervalued players to add"
        - "What are the best waiver pickups this week?"
    """
    try:
        league = await _get_league()
        league_id = league["league_id"]

        taken, players = await _parallel_fetch(
            _get_taken_player_ids(league_id),
            _get_players(),
        )

        positions = ["QB", "RB", "WR", "TE"]
        candidates = [
            (pid, p) for pid, p in players.items()
            if p.get("position") in positions
            and p.get("active", False)
            and pid not in taken
            and p.get("team")
        ]

        projections = await _fetch_projections(positions)

        scored = []
        for pid, p in candidates:
            proj = projections.get(pid, {})
            pts = calculate_fantasy_points(proj, p.get("position", ""))
            if pts <= 2.0:
                continue
            own_pct = max(p.get("ownership", {}).get("percentage_owned", 0.0), 0.1)
            value = pts / own_pct
            scored.append((value, pts, pid, p, own_pct))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: params.limit]

        lines = [
            "# 💡 Trade Targets & Undervalued Adds",
            "*(The Chrysoloras Gang — Free Agents only)*",
            "",
            "**Value Score** = Projected Pts ÷ Ownership%  (higher → more undervalued)",
            f"**⚠️ Trade deadline: Week 12**",
            "",
            f"{'#':<3} {'Player':<25} {'Pos':<5} {'Team':<5} {'Proj':>7} {'Own%':>6}  {'Value':>7}",
            "─" * 62,
        ]
        for rank, (value, pts, pid, p, own_pct) in enumerate(top, 1):
            name = _player_display_name(p)[:25]
            pos = p.get("position", "?")
            team = (p.get("team") or "FA")[:4]
            lines.append(f"{rank:<3} {name:<25} {pos:<5} {team:<5} {pts:>7.1f} {own_pct:>5.1f}%  {value:>7.1f}")

        if not top:
            lines.append(
                "No undervalued players found. Projections may not yet be available "
                f"for the {CURRENT_SEASON} season — try again once the season begins."
            )

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Shared roster helper (used by Tools 7–12)
# ─────────────────────────────────────────────────────────────────────────────

async def _get_my_roster_context() -> tuple:
    """
    Fetch everything needed about GronkQuixote's roster in one shot:
    (league, my_roster, players_dict, roster_player_ids)
    """
    user_id = await _get_user_id()
    league = await _get_league()
    league_id = league["league_id"]

    rosters, players = await _parallel_fetch(
        _get(f"/league/{league_id}/rosters"),
        _get_players(),
    )
    my_roster = next((r for r in rosters if r.get("owner_id") == user_id), None)
    if not my_roster:
        raise ValueError(f"Could not find GronkQuixote's roster in league '{league.get('name')}'.")

    player_ids = my_roster.get("players") or []
    return league, my_roster, players, player_ids


def _resolve_player_by_name(players: Dict[str, Any], query: str) -> tuple:
    """
    Resolve a player name to (player_id, player_dict). Raises ValueError with a
    helpful message on no-match or too-many-matches.
    """
    q = query.lower().strip()
    matches = [(pid, p) for pid, p in players.items() if q in _player_display_name(p).lower()]
    if not matches:
        raise ValueError(f"No player found matching '{query}'.")
    if len(matches) > 8:
        names = ", ".join(_player_display_name(p) for _, p in matches[:10])
        raise ValueError(f"Too many players match '{query}' ({len(matches)} results). Examples: {names}")
    return min(
        matches,
        key=lambda x: (0 if _player_display_name(x[1]).lower() == q else 1, len(_player_display_name(x[1]))),
    )

# ─────────────────────────────────────────────────────────────────────────────
# Tool 7 — get_optimal_lineup
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_optimal_lineup",
    annotations={
        "title": "Get Optimal Start/Sit Lineup",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_optimal_lineup() -> str:
    """Recommend the highest-projected starting lineup from your current roster.

    Computes the mathematically optimal lineup for this week under Chrysoloras
    Gang scoring (1 QB, 2 RB, 2 WR, 1 TE, 2 FLEX, 1 K, 1 DEF), then compares it
    against your currently-set Sleeper starters and flags any recommended swaps.

    Returns:
        str: Markdown table with slot, recommended player, projected points,
             and a flag if it differs from your current starter in that slot.
             Also lists your bench.

    Error response: "Error: <message>" if roster/projection data can't be fetched.

    Example prompts:
        - "What's my optimal lineup this week?"
        - "Should I start anyone different?"
        - "Set my best possible lineup"
    """
    try:
        league, my_roster, players, player_ids = await _get_my_roster_context()
        if not player_ids:
            return "Your roster is empty (no players drafted/added yet) — nothing to optimize."
        season_type, week, season = await _get_current_week()

        proj = await _fetch_projections_for_week(season, week, ["QB", "RB", "WR", "TE", "K", "DEF"])
        if not proj and season_type != "regular":
            proj = await _fetch_projections_for_week(season, 1, ["QB", "RB", "WR", "TE", "K", "DEF"])

        by_pos: Dict[str, List[tuple]] = {"QB": [], "RB": [], "WR": [], "TE": [], "K": [], "DEF": []}
        for pid in player_ids:
            p = players.get(pid, {})
            pos = p.get("position")
            if pos in by_pos:
                pts = calculate_fantasy_points(proj.get(pid, {}), pos)
                by_pos[pos].append((pts, pid, p))

        for pos in by_pos:
            by_pos[pos].sort(key=lambda x: x[0], reverse=True)

        used_ids = set()

        def take(pos: str, n: int) -> List[tuple]:
            avail = [x for x in by_pos[pos] if x[1] not in used_ids][:n]
            for x in avail:
                used_ids.add(x[1])
            return avail

        starters_by_slot: List[tuple] = []
        starters_by_slot += [("QB", x) for x in take("QB", 1)]
        starters_by_slot += [("RB", x) for x in take("RB", 2)]
        starters_by_slot += [("WR", x) for x in take("WR", 2)]
        starters_by_slot += [("TE", x) for x in take("TE", 1)]

        flex_pool = sorted(
            [x for pos in ("RB", "WR", "TE") for x in by_pos[pos] if x[1] not in used_ids],
            key=lambda x: x[0], reverse=True,
        )[:2]
        for x in flex_pool:
            used_ids.add(x[1])
        starters_by_slot += [("FLEX", x) for x in flex_pool]

        starters_by_slot += [("K", x) for x in take("K", 1)]
        starters_by_slot += [("DEF", x) for x in take("DEF", 1)]

        current_starters = set(my_roster.get("starters") or [])

        lines = [
            f"# 🎯 Optimal Lineup — Week {week} ({season_type})",
            "",
            f"{'Slot':<6} {'Player':<25} {'Pos':<5} {'Proj':>7}  Status",
            "─" * 55,
        ]
        for slot, (pts, pid, p) in starters_by_slot:
            name = _player_display_name(p)[:25]
            pos = p.get("position", "?")
            status = "✅ already starting" if pid in current_starters else "🔄 RECOMMEND SWAP IN"
            lines.append(f"{slot:<6} {name:<25} {pos:<5} {pts:>7.1f}  {status}")

        missing_slots = []
        filled_positions = [s for s, _ in starters_by_slot]
        needed = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "DEF": 1}
        for slot, count in needed.items():
            have = filled_positions.count(slot)
            if have < count:
                missing_slots.append(f"{slot} (need {count - have} more — check waivers)")
        if missing_slots:
            lines += ["", "⚠️ **Understaffed slots:** " + ", ".join(missing_slots)]

        bench_ids = [pid for pid in player_ids if pid not in used_ids and pid not in (my_roster.get("reserve") or [])]
        if bench_ids:
            lines += ["", "## Bench"]
            for pid in bench_ids:
                p = players.get(pid, {})
                pos = p.get("position", "?")
                pts = calculate_fantasy_points(proj.get(pid, {}), pos) if pos in by_pos else 0.0
                lines.append(f"- {_player_display_name(p)} ({pos}) — {pts:.1f} proj pts")

        currently_starting_not_optimal = [
            pid for pid in current_starters if pid not in used_ids and pid in player_ids
        ]
        if currently_starting_not_optimal:
            lines += ["", "## ⚠️ Currently starting but NOT in the optimal lineup"]
            for pid in currently_starting_not_optimal:
                p = players.get(pid, {})
                pos = p.get("position", "?")
                pts = calculate_fantasy_points(proj.get(pid, {}), pos) if pos in by_pos else 0.0
                lines.append(f"- {_player_display_name(p)} ({pos}) — {pts:.1f} proj pts — consider benching")

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 8 — get_injury_report
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_injury_report",
    annotations={
        "title": "Get Roster Injury Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_injury_report() -> str:
    """Scan your roster for players with an active injury designation.

    Checks each rostered player's Sleeper injury_status (Questionable, Doubtful,
    Out, IR, PUP, etc.) and body part. Flags whether the player is currently in
    your starting lineup (higher urgency) or on the bench.

    Note: Sleeper's player data includes game-status injury designations but not
    daily practice participation (limited/full/DNP) — for that level of detail,
    check a source like ESPN or the team's official injury report.

    Returns:
        str: Markdown list of flagged players grouped by severity (Out/IR first,
             then Doubtful, then Questionable), or a note that the roster is
             fully healthy.

    Example prompts:
        - "Is anyone on my team injured?"
        - "Check my roster for injuries"
        - "Who's questionable this week?"
    """
    try:
        league, my_roster, players, player_ids = await _get_my_roster_context()
        if not player_ids:
            return "Your roster is empty (no players drafted/added yet) — nothing to check."
        starters = set(my_roster.get("starters") or [])

        severity_order = {"Out": 0, "IR": 0, "PUP": 0, "Doubtful": 1, "Questionable": 2, "Suspended": 0}
        flagged = []
        for pid in player_ids:
            p = players.get(pid, {})
            status = p.get("injury_status")
            if status:
                flagged.append((severity_order.get(status, 3), pid, p, status))

        if not flagged:
            return "✅ Your entire roster is healthy — no injury designations reported."

        flagged.sort(key=lambda x: x[0])

        lines = ["# 🏥 Injury Report — GronkQuixote's Roster", ""]
        for _, pid, p, status in flagged:
            name = _player_display_name(p)
            pos = p.get("position", "?")
            team = p.get("team", "?")
            body_part = p.get("injury_body_part") or "unspecified"
            slot = "🟢 STARTER" if pid in starters else "⚪ Bench"
            emoji = {"Out": "🔴", "IR": "🔴", "PUP": "🔴", "Doubtful": "🟠", "Questionable": "🟡"}.get(status, "⚪")
            lines.append(f"{emoji} **{name}** ({pos}, {team}) — {status}, {body_part} — {slot}")

        lines += [
            "",
            "*Body-part detail only; daily practice participation isn't available via the Sleeper API.*",
        ]
        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 9 — get_bye_week_report
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_bye_week_report",
    annotations={
        "title": "Get Bye Week Collision Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_bye_week_report() -> str:
    """Map every roster player's bye week and flag collisions.

    Bye weeks aren't in Sleeper's data, so this derives them from the real NFL
    schedule (via ESPN's public schedule) by finding the week each team doesn't
    play. With only 2 FLEX slots and a 6-man bench, multiple starters sharing a
    bye week can leave you scrambling — this flags those weeks specifically.

    Returns:
        str: Markdown report grouped by week, showing which of your players are
             on bye, with high-severity flags when 2+ *starters* share a week.

    Example prompts:
        - "When are my players' bye weeks?"
        - "Do I have any bye week collisions?"
        - "Which week will hurt my roster the most?"
    """
    try:
        league, my_roster, players, player_ids = await _get_my_roster_context()
        if not player_ids:
            return "Your roster is empty (no players drafted/added yet) — nothing to check."
        starters = set(my_roster.get("starters") or [])
        schedule = await _get_full_schedule(CURRENT_SEASON)
        team_bye = schedule["team_bye"]

        by_week: Dict[int, List[tuple]] = {}
        no_bye_data = []
        for pid in player_ids:
            p = players.get(pid, {})
            team = p.get("team")
            pos = p.get("position")
            if pos == "DEF" or not team:
                continue
            bye = team_bye.get(team)
            if bye is None:
                no_bye_data.append(p)
                continue
            by_week.setdefault(bye, []).append((pid, p))

        if not by_week:
            return "No bye week data available yet — the ESPN schedule may not be published for this season."

        lines = ["# 📅 Bye Week Report — GronkQuixote's Roster", ""]
        for week in sorted(by_week.keys()):
            entries = by_week[week]
            starter_count = sum(1 for pid, _ in entries if pid in starters)
            flag = ""
            if starter_count >= 2:
                flag = "  🚨 **MULTIPLE STARTERS ON BYE**"
            elif starter_count == 1:
                flag = "  ⚠️ 1 starter on bye"
            lines.append(f"## Week {week}{flag}")
            for pid, p in entries:
                name = _player_display_name(p)
                pos = p.get("position", "?")
                team = p.get("team", "?")
                slot = "STARTER" if pid in starters else "bench"
                lines.append(f"- {name} ({pos}, {team}) — {slot}")
            lines.append("")

        if no_bye_data:
            lines.append(f"*Bye week not yet determined for: {', '.join(_player_display_name(p) for p in no_bye_data)}*")

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 10 — get_strength_of_schedule
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_strength_of_schedule",
    annotations={
        "title": "Get Player Strength of Schedule",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_strength_of_schedule(params: GetStrengthOfScheduleInput) -> str:
    """Rank a player's upcoming opponents from easiest to hardest matchup.

    Uses the real NFL schedule (via ESPN) plus each opponent's season fantasy
    points allowed to that position (Sleeper's fan_pts_allow_<pos> defensive
    stat) to rank all 32 defenses, then shows where each upcoming opponent
    falls. Rank 1 = easiest matchup (allows the most points to this position),
    rank 32 = hardest. Specifically flags Weeks 15-17 since Chrysoloras Gang
    playoffs start Week 15 — a favorable playoff stretch is a real edge.

    Note: defensive strength is measured using Sleeper's own scoring for the
    fan_pts_allow stat (not recalculated under this league's exact rules), so
    treat ranks as directional, not exact point projections.

    Args:
        params (GetStrengthOfScheduleInput):
            - player_name (str): Player to analyze, e.g. 'Bijan Robinson'
            - weeks_ahead (int): How many upcoming weeks to show (1-18, default 8)

    Returns:
        str: Markdown table of Week, Opponent, Home/Away, matchup rank (1-32),
             and season fantasy points allowed to that position.

    Example prompts:
        - "What's Bijan Robinson's strength of schedule?"
        - "Does my WR1 have a good playoff schedule?"
        - "Rank my RB's upcoming matchups"
    """
    try:
        players = await _get_players()
        pid, player = _resolve_player_by_name(players, params.player_name)
        position = player.get("position", "")
        team = player.get("team")

        if position not in DEF_ALLOWED_POS_KEY:
            return f"Strength of schedule isn't supported for position '{position}' (supported: QB, RB, WR, TE, K)."
        if not team:
            return f"{_player_display_name(player)} is a free agent with no current NFL team."

        pos_key = DEF_ALLOWED_POS_KEY[position]
        season_type, current_week, season = await _get_current_week()

        schedule = await _get_full_schedule(CURRENT_SEASON)
        team_sched = schedule["team_week_opponent"].get(team, {})
        if not team_sched:
            return f"No schedule data found for {team}. The season schedule may not be published yet."

        # Prefer current-season defensive stats; fall back to most recent completed season
        def_season = CURRENT_SEASON
        def_stats = await _get_season_stats_all(CURRENT_SEASON)
        # Heuristic: if current season has no meaningful DEF data yet (pre-season), use STATS_SEASON
        if not any(k in STADIUM_INFO and def_stats.get(k, {}).get(f"fan_pts_allow_{pos_key}") for k in STADIUM_INFO):
            def_season = STATS_SEASON
            def_stats = await _get_season_stats_all(STATS_SEASON)

        team_allowed = []
        for t in STADIUM_INFO:
            allowed = def_stats.get(t, {}).get(f"fan_pts_allow_{pos_key}")
            if allowed is not None:
                team_allowed.append((t, allowed))
        team_allowed.sort(key=lambda x: x[1], reverse=True)  # most allowed = easiest = rank 1
        rank_map = {t: i + 1 for i, (t, _) in enumerate(team_allowed)}
        allowed_map = dict(team_allowed)
        total_teams = len(team_allowed) or 32

        start_week = current_week if season_type == "regular" else 1
        weeks = [w for w in range(start_week, min(start_week + params.weeks_ahead, 19))]

        lines = [
            f"# 🗓️ Strength of Schedule — {_player_display_name(player)} ({position}, {team})",
            f"*Defensive strength based on {def_season} season fan pts allowed to {position}*",
            "",
            f"{'Wk':<4} {'Opp':<5} {'H/A':<4} {'Rank (1=easy,32=hard)':<24} {'Pts Allowed to Pos':>18}",
            "─" * 60,
        ]
        playoff_ranks = []
        for wk in weeks:
            info = team_sched.get(wk)
            if not info:
                lines.append(f"{wk:<4} {'BYE':<5}")
                continue
            opp = info["opponent"]
            ha = "vs" if info["is_home"] else "@"
            rank = rank_map.get(opp, "N/A")
            allowed = allowed_map.get(opp)
            allowed_str = f"{allowed:.1f}" if allowed is not None else "N/A"
            lines.append(f"{wk:<4} {opp:<5} {ha:<4} {str(rank) + f'/{total_teams}':<24} {allowed_str:>18}")
            if 15 <= wk <= 17 and isinstance(rank, int):
                playoff_ranks.append(rank)

        if playoff_ranks:
            avg_rank = sum(playoff_ranks) / len(playoff_ranks)
            verdict = "favorable 🟢" if avg_rank <= total_teams / 3 else ("tough 🔴" if avg_rank >= total_teams * 2 / 3 else "middling 🟡")
            lines += ["", f"**Playoff stretch (Wk 15-17) avg matchup rank: {avg_rank:.1f}/{total_teams} — {verdict}**"]

        return "\n".join(lines)

    except ValueError as ve:
        return f"Error: {ve}"
    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 11 — get_weather_report
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_weather_report",
    annotations={
        "title": "Get Game Weather Forecast",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_weather_report(params: GetWeatherReportInput) -> str:
    """Fetch the weather forecast for a player's (or your whole roster's) next game.

    Combines the real NFL schedule (ESPN) with stadium locations to fetch a
    16-day forecast (Open-Meteo, free/no-key) for the game venue. Dome/indoor
    stadiums are flagged as not weather-dependent. Includes a short heuristic
    note on fantasy impact (wind hurts passing/kicking, cold hurts kicking,
    high precip favors rushing).

    Note: forecasts are only available ~16 days out — games further away will
    show "not yet available" until closer to kickoff.

    Args:
        params (GetWeatherReportInput):
            - player_name (Optional[str]): Check one player's team's next game.
              If omitted, checks the next game for every team on your roster.

    Returns:
        str: Markdown weather report per game — date, venue, temps, wind,
             precip chance, and fantasy impact note.

    Example prompts:
        - "What's the weather looking like for my QB's game?"
        - "Check weather for all my players' upcoming games"
        - "Is it going to be windy for Buffalo this week?"
    """
    try:
        schedule = await _get_full_schedule(CURRENT_SEASON)
        season_type, current_week, season = await _get_current_week()
        start_week = current_week if season_type == "regular" else 1

        teams_to_check: List[str] = []
        header_names: Dict[str, str] = {}

        if params.player_name:
            players = await _get_players()
            pid, player = _resolve_player_by_name(players, params.player_name)
            team = player.get("team")
            if not team:
                return f"{_player_display_name(player)} is a free agent with no current NFL team."
            teams_to_check = [team]
            header_names[team] = _player_display_name(player)
        else:
            league, my_roster, players, player_ids = await _get_my_roster_context()
            seen = set()
            for pid in player_ids:
                p = players.get(pid, {})
                team = p.get("team")
                pos = p.get("position")
                if team and pos != "DEF" and team not in seen:
                    seen.add(team)
                    teams_to_check.append(team)

        if not teams_to_check:
            return "No teams found to check — your roster may be empty."

        lines = ["# 🌦️ Weather Report", ""]
        for team in teams_to_check:
            team_sched = schedule["team_week_opponent"].get(team, {})
            next_game = None
            next_week = None
            for wk in range(start_week, 19):
                if wk in team_sched:
                    next_game = team_sched[wk]
                    next_week = wk
                    break

            label = header_names.get(team, team)
            if not next_game:
                lines.append(f"## {label} ({team}) — no upcoming game found")
                continue

            opp = next_game["opponent"]
            ha = "vs" if next_game["is_home"] else "@"
            home_team = team if next_game["is_home"] else opp
            venue_info = STADIUM_INFO.get(home_team, {})
            venue_name = next_game.get("venue_name") or venue_info.get("name", "Unknown venue")
            indoor = next_game.get("indoor", venue_info.get("indoor", False))
            date_str = (next_game.get("date_utc") or "")[:10]

            lines.append(f"## {label} ({team}) {ha} {opp} — Week {next_week}, {date_str}")
            lines.append(f"*{venue_name}*")

            if indoor:
                lines.append("🏟️ Indoor/dome stadium — not a weather factor.")
            else:
                weather = await _get_weather_forecast(venue_info["lat"], venue_info["lon"], next_game["date_utc"])
                if weather is None:
                    lines.append("*Forecast not yet available (game is more than ~16 days out, or already past).*")
                else:
                    lines.append(
                        f"🌡️ {weather['temp_min_f']:.0f}–{weather['temp_max_f']:.0f}°F  "
                        f"💨 wind up to {weather['wind_max_mph']:.0f} mph  "
                        f"🌧️ {weather['precip_prob_pct']:.0f}% precip chance"
                    )
                    lines.append(f"*Fantasy note: {_weather_fantasy_note(weather)}*")
            lines.append("")

        return "\n".join(lines)

    except ValueError as ve:
        return f"Error: {ve}"
    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 12 — get_snap_report
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_snap_report",
    annotations={
        "title": "Get Snap Share and Usage Split Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_snap_report(params: GetSnapReportInput) -> str:
    """Show recent snap share and rush/target usage trend for a player or your roster.

    Pulls Sleeper's per-week box scores (off_snp / tm_off_snp for snap share,
    rush_att and rec_tgt for usage split) across the last N completed weeks to
    surface role trends — a rising snap share or target share is often the
    earliest signal of a breakout, well before season-long ownership catches up.

    Args:
        params (GetSnapReportInput):
            - player_name (Optional[str]): Single player to analyze. If omitted,
              reports on every QB/RB/WR/TE on your roster.
            - weeks_back (int): Recent completed weeks to include (1-10, default 4)

    Returns:
        str: Markdown table per player showing week-by-week snap %, rush attempts,
             targets, receptions, and a simple trend read (rising/falling/stable).

    Example prompts:
        - "What's my RB2's snap share trend?"
        - "Check usage splits for my whole roster"
        - "Is anyone's role changing lately?"
    """
    try:
        season_type, current_week, season = await _get_current_week()

        # Determine which weeks have completed data to look back over
        if season_type == "regular" and current_week > 1:
            end_week = current_week - 1
            weeks = list(range(max(1, end_week - params.weeks_back + 1), end_week + 1))
            stats_season = season
        else:
            # Pre-season or Week 1: fall back to the end of the most recent completed season
            weeks = list(range(max(1, 18 - params.weeks_back + 1), 19))
            stats_season = STATS_SEASON

        players = await _get_players()

        if params.player_name:
            pid, player = _resolve_player_by_name(players, params.player_name)
            target_players = [(pid, player)]
        else:
            league, my_roster, players, player_ids = await _get_my_roster_context()
            target_players = [
                (pid, players.get(pid, {})) for pid in player_ids
                if players.get(pid, {}).get("position") in ("QB", "RB", "WR", "TE")
            ]

        if not target_players:
            return "No skill-position players found to report on."

        weekly_blobs = dict(zip(weeks, await _parallel_fetch(*[_get_weekly_stats(stats_season, w) for w in weeks])))

        lines = [f"# 📈 Snap Share & Usage Report", f"*Weeks {weeks[0]}–{weeks[-1]}, {stats_season} season*", ""]

        for pid, player in target_players:
            name = _player_display_name(player)
            pos = player.get("position", "?")
            team = player.get("team", "FA")

            rows = []
            for wk in weeks:
                stat = weekly_blobs.get(wk, {}).get(pid, {})
                if not stat:
                    continue
                off_snp = stat.get("off_snp")
                tm_off_snp = stat.get("tm_off_snp")
                snap_pct = (off_snp / tm_off_snp * 100) if off_snp is not None and tm_off_snp else None
                rows.append({
                    "week": wk,
                    "snap_pct": snap_pct,
                    "rush_att": stat.get("rush_att", 0) or 0,
                    "tgt": stat.get("rec_tgt", 0) or 0,
                    "rec": stat.get("rec", 0) or 0,
                })

            lines.append(f"## {name} ({pos}, {team})")
            if not rows:
                lines.append("*No snap/usage data found for these weeks.*")
                lines.append("")
                continue

            lines.append(f"{'Wk':<4} {'Snap%':>7} {'Rush':>6} {'Tgt':>5} {'Rec':>5}")
            for r in rows:
                snap_str = f"{r['snap_pct']:.0f}%" if r["snap_pct"] is not None else "N/A"
                lines.append(f"{r['week']:<4} {snap_str:>7} {r['rush_att']:>6.0f} {r['tgt']:>5.0f} {r['rec']:>5.0f}")

            valid_snaps = [r["snap_pct"] for r in rows if r["snap_pct"] is not None]
            if len(valid_snaps) >= 2:
                delta = valid_snaps[-1] - valid_snaps[0]
                if delta >= 8:
                    trend = f"↑ rising ({valid_snaps[0]:.0f}% → {valid_snaps[-1]:.0f}%)"
                elif delta <= -8:
                    trend = f"↓ falling ({valid_snaps[0]:.0f}% → {valid_snaps[-1]:.0f}%)"
                else:
                    trend = f"→ stable (~{valid_snaps[-1]:.0f}%)"
                lines.append(f"*Snap trend: {trend}*")
            lines.append("")

        return "\n".join(lines)

    except ValueError as ve:
        return f"Error: {ve}"
    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Tool 13 — get_draft_best_available
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_draft_best_available",
    annotations={
        "title": "Get Live Draft Best Available",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sleeper_get_draft_best_available(params: GetDraftBestAvailableInput) -> str:
    """Live draft-day assistant: best remaining players ranked by ADP, not weekly matchup value.

    Reads your league's real Sleeper draft board — players already picked come from
    the live draft's pick list (not season rosters), so this works correctly mid-draft,
    before rosters have updated. Remaining players are ranked by ADP (average draft
    position, PPR format), which is the right lens for draft day: overall value and
    positional scarcity, not this week's matchup.

    During an in-progress snake draft, also reports who's currently on the clock and
    shows a positional-scarcity snapshot (how many of each position remain in the top
    50 overall ADP) so you can see when a position is about to dry up.

    Use `sleeper_get_available_players` instead once the season is underway and you're
    making weekly lineup/waiver decisions — that tool factors in schedule and snap trend,
    which don't matter yet on draft day.

    Args:
        params (GetDraftBestAvailableInput):
            - position (Optional[PositionEnum]): Filter to one position, or omit for all
            - limit (int): Players to return (1–50, default 20)

    Returns:
        str: Markdown draft board — status/on-the-clock header, then a table of best
             available players with overall ADP, positional ADP rank, and a Week 1
             preview projection. Ends with a positional scarcity snapshot.

    Error response: "No draft found for this league yet." if the league has no draft set up.

    Example prompts:
        - "Who's the best player available in my draft?"
        - "Best available RBs right now"
        - "Who's on the clock, and is a position about to dry up?"
    """
    try:
        league = await _get_league()
        league_id = league["league_id"]
        drafts = await _get(f"/league/{league_id}/drafts")
        if not drafts:
            return "No draft found for this league yet."
        draft = drafts[0]
        draft_id = draft["draft_id"]
        status = draft.get("status", "unknown")

        picks, players, users = await _parallel_fetch(
            _get(f"/draft/{draft_id}/picks"),
            _get_players(),
            _get(f"/league/{league_id}/users"),
        )
        drafted_ids = {p["player_id"] for p in picks if p.get("player_id")}

        user_map = {u["user_id"]: u.get("display_name", "?") for u in users}
        slot_to_user = {v: k for k, v in (draft.get("draft_order") or {}).items()}
        teams = draft.get("settings", {}).get("teams", 12)

        header_lines = ["# 🏈 Draft Board — The Chrysoloras Gang", f"*Status: {status}*"]

        if status in ("drafting", "pre_draft") and slot_to_user:
            pick_no = len(picks) + 1
            rnd = (pick_no - 1) // teams + 1
            pos_in_round = (pick_no - 1) % teams
            slot = pos_in_round + 1 if rnd % 2 == 1 else teams - pos_in_round
            on_clock_user = slot_to_user.get(slot)
            on_clock_name = user_map.get(on_clock_user, "Unknown") if on_clock_user else "Unknown"
            header_lines.append(f"*Round {rnd}, Pick {pick_no} — on the clock: **{on_clock_name}***")
        elif status == "complete":
            header_lines.append("*Draft is complete — try `sleeper_get_my_team` or the waiver tools instead.*")

        pos_filter = _resolve_positions(params.position) if params.position else \
            list(DEF_ALLOWED_POS_KEY.keys()) + ["DEF"]

        proj = await _fetch_projections_for_week(CURRENT_SEASON, 1, pos_filter)

        candidates = [
            (pid, p) for pid, p in players.items()
            if p.get("position") in pos_filter
            and p.get("active", False)
            and pid not in drafted_ids
            and p.get("team")
        ]

        def sort_key(item: tuple) -> float:
            pid, p = item
            adp = proj.get(pid, {}).get("adp_dd_ppr")
            if adp is None:
                adp = p.get("search_rank") or 9999
            return adp

        candidates.sort(key=sort_key)
        top = candidates[: params.limit]

        lines = header_lines + [
            "",
            f"{'#':<4} {'Player':<22} {'Pos':<4} {'Team':<5} {'ADP':>6} {'Pos Rank':>9} {'Wk1 Proj':>9}",
            "─" * 65,
        ]
        for rank, (pid, p) in enumerate(top, 1):
            name = _player_display_name(p)[:22]
            pos = p.get("position", "?")
            team = (p.get("team") or "FA")[:4]
            pdata = proj.get(pid, {})
            adp = pdata.get("adp_dd_ppr")
            pos_adp = pdata.get("pos_adp_dd_ppr")
            adp_str = f"{adp:.1f}" if adp is not None else "—"
            pos_rank_str = f"{pos}{pos_adp:.0f}" if pos_adp is not None else "—"
            wk1 = calculate_fantasy_points(pdata, pos)
            lines.append(f"{rank:<4} {name:<22} {pos:<4} {team:<5} {adp_str:>6} {pos_rank_str:>9} {wk1:>9.1f}")

        if not top:
            lines.append("No undrafted players match this filter.")
        elif params.position is None:
            # Scarcity snapshot only makes sense across the full (unfiltered) board
            top50 = candidates[:50]
            pos_counts: Dict[str, int] = {}
            for pid, p in top50:
                pos_counts[p.get("position", "?")] = pos_counts.get(p.get("position", "?"), 0) + 1
            scarcity_line = " · ".join(f"{pos}: {cnt}" for pos, cnt in sorted(pos_counts.items(), key=lambda x: -x[1]))
            lines += ["", f"*Remaining in top 50 overall ADP, by position — {scarcity_line}*"]

        return "\n".join(lines)

    except Exception as exc:
        return _handle_error(exc)

# ─────────────────────────────────────────────────────────────────────────────
# Async utilities
# ─────────────────────────────────────────────────────────────────────────────

async def _parallel_fetch(*coroutines):
    """Run multiple coroutines concurrently and return results in order."""
    return await asyncio.gather(*coroutines)


# ─────────────────────────────────────────────────────────────────────────────
# All play tools — same core the dashboard uses (ADR 001)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="sleeper_get_all_play_standings",
    description=(
        "Show all play standings: how every team would fare if it played every "
        "other team every week, instead of just its scheduled opponent. Reveals "
        "which teams are winning on schedule luck and which are genuinely good. "
        "Use for 'who is actually the best team', 'am I unlucky', 'power rankings'."
    ),
)
async def sleeper_get_all_play_standings() -> str:
    """
    All play standings with a luck column.

    All play record is each team scored against all 11 other teams each week.
    The gap between real record and all play record is schedule luck.

    Returns:
        str: Markdown table of rank, real record, all play record, all play
             percentage, luck, and average points, sorted by all play strength.

    Example prompts:
        - "Who's actually the best team in the league?"
        - "Have I been unlucky this season?"
        - "Show me the all play standings"
    """
    try:
        payload = await core_league.build_dashboard_payload()
        return render.all_play_table(payload)
    except Exception as exc:
        return _handle_error(exc)


@mcp.tool(
    name="sleeper_get_head_to_head_grid",
    description=(
        "Show the everyone vs everyone matrix: each team's record against each "
        "other team across every week of the season. Use for 'who owns who', "
        "'head to head grid', 'who matches up well against me'."
    ),
)
async def sleeper_get_head_to_head_grid() -> str:
    """
    Everyone vs everyone head to head matrix.

    Returns:
        str: Markdown matrix where each cell is the row team's record against
             the column team, computed as if they played every single week.

    Example prompts:
        - "Show me the head to head grid"
        - "Which teams do I beat most often?"
    """
    try:
        payload = await core_league.build_dashboard_payload()
        return render.head_to_head_matrix(payload)
    except Exception as exc:
        return _handle_error(exc)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration tools — change username / league at runtime
# ─────────────────────────────────────────────────────────────────────────────

from sleeper import config as sleeper_config  # noqa: E402 (used only in tools below)


@mcp.resource("sleeper://setup")
def sleeper_setup_guide() -> str:
    """Startup guide — instructs the user to configure identity before use."""
    configured = bool(sleeper_config.SLEEPER_USERNAME and
                      (sleeper_config.LEAGUE_ID or sleeper_config.LEAGUE_NAME_MATCH))
    if configured:
        return (
            f"Sleeper MCP is ready.\n"
            f"Username: {sleeper_config.SLEEPER_USERNAME}\n"
            f"League: {sleeper_config.LEAGUE_ID or sleeper_config.LEAGUE_NAME_MATCH}"
        )
    return (
        "Welcome to Sleeper Fantasy MCP!\n\n"
        "Before using any tools, please configure your identity:\n\n"
        "1. Set your username:\n"
        "   > Call sleeper_set_username with your Sleeper display name.\n\n"
        "2. Set your league:\n"
        "   > Call sleeper_set_league with your league name or league ID.\n\n"
        "You only need to do this once per session. "
        "Use environment variables SLEEPER_USERNAME and SLEEPER_LEAGUE_MATCH "
        "to pre-configure across sessions."
    )


@mcp.tool(
    name="sleeper_get_config",
    annotations={
        "title": "Get Active Sleeper Configuration",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def sleeper_get_config() -> str:
    """Show the active Sleeper username and league identity settings.

    Returns the username, league name match fragment, and pinned league ID
    (if any) that the server is currently using.

    Example prompts:
        - "What username is the Sleeper MCP using?"
        - "Which league am I connected to?"
        - "Show current Sleeper config"
    """
    lines = [
        "## Active Sleeper Configuration",
        f"- **Username:** {sleeper_config.SLEEPER_USERNAME}",
        f"- **League name match:** `{sleeper_config.LEAGUE_NAME_MATCH}`",
        f"- **Pinned league ID:** {sleeper_config.LEAGUE_ID or '*(none — using name match)*'}",
        f"- **Season:** {sleeper_config.CURRENT_SEASON}",
    ]
    return "\n".join(lines)


@mcp.tool(
    name="sleeper_set_username",
    annotations={
        "title": "Set Sleeper Username",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def sleeper_set_username(username: str) -> str:
    """Change the active Sleeper username used by all tools.

    Updates the username and clears cached user/league data so the next
    tool call resolves fresh data for the new user.

    Args:
        username: Sleeper display name (case sensitive).

    Returns:
        str: Confirmation message.

    Example prompts:
        - "Switch to username JohnDoe"
        - "Change the Sleeper user to TDMachine99"
    """
    sleeper_config.set_username(username)
    # Invalidate cached identity so subsequent calls re-resolve for the new user.
    from sleeper import cache as sleeper_cache
    for prefix in ("user_id:", "league:"):
        for key in list(sleeper_cache.memory._data.keys()):
            if key.startswith(prefix):
                sleeper_cache.memory.invalidate(key)
    return f"Username updated to **{username}**. League cache cleared."


@mcp.tool(
    name="sleeper_set_league",
    annotations={
        "title": "Set Fantasy League",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def sleeper_set_league(
    league_name: Optional[str] = None,
    league_id: Optional[str] = None,
) -> str:
    """Change the active fantasy league used by all tools.

    Provide either a league_name fragment (matched case-insensitively against
    the user's leagues) or an exact league_id. If both are given, league_id
    takes precedence.

    Args:
        league_name: Partial or full league name to match (e.g. "chrysoloras").
        league_id:   Exact Sleeper league ID (overrides name matching).

    Returns:
        str: Confirmation with the resolved league name and ID.

    Example prompts:
        - "Switch to my league called Dynasty Kings"
        - "Set the league to ID 1234567890"
    """
    if not league_name and not league_id:
        return "Provide at least one of `league_name` or `league_id`."

    if league_id:
        sleeper_config.set_league_id(league_id)
    else:
        sleeper_config.set_league_match(league_name)  # type: ignore[arg-type]

    # Clear cached league so the next call re-resolves under the new identity.
    from sleeper import cache as sleeper_cache
    for key in list(sleeper_cache.memory._data.keys()):
        if key.startswith("league:"):
            sleeper_cache.memory.invalidate(key)

    if league_id:
        try:
            league = await core_league.get_league()
            name = league.get("name", league_id)
            return f"League updated to **{name}** (ID: `{league_id}`)."
        except Exception as exc:
            return f"League ID set to `{league_id}` but could not verify: {_handle_error(exc)}"
    else:
        try:
            league = await core_league.get_league()
            name = league.get("name", "?")
            lid = league.get("league_id", "?")
            return f"League updated to **{name}** (ID: `{lid}`)."
        except Exception as exc:
            return f"League match set to `{league_name}` but could not verify: {_handle_error(exc)}"


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
