"""
Real NFL game state, per team, for one week.

A fantasy matchup is only decided once every starter on both sides has finished
playing, and neither the league nor the matchup endpoints say whether a given
game has kicked off or gone final. Sleeper publishes that separately, on the
same public API and with the same team abbreviations it uses on player records,
so that is the primary source here. ESPN's public scoreboard is the fallback,
with its abbreviations translated.

Everything fails closed. If neither source can be read, the answer is None and
callers must treat every starter as still to play. Showing a winner late is an
annoyance; showing one early is the bug this module exists to prevent.
"""

from typing import Any, Dict, List, Optional

from . import cache, client

SLEEPER_SCORES = "https://api.sleeper.app/scores/nfl/{season_type}/{season}/{week}"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

# ESPN abbreviation -> Sleeper abbreviation, where the two disagree. Sleeper's
# own scores endpoint needs no translation, which is half the reason it leads.
ESPN_TO_SLEEPER = {"WSH": "WAS", "OAK": "LV"}

PRE, IN, POST, BYE = "pre", "in", "post", "bye"
DONE_STATES = frozenset({POST, BYE})

# A team absent from the game list is read as on bye, so a truncated response
# could retire starters who are in fact still to play. The NFL never schedules
# fewer than 12 games in a regular season week (six byes at most), so anything
# shorter is treated as unusable rather than as a week full of byes.
MIN_GAMES = 12

TTL_GAME_STATE = 30  # seconds; matches the live matchup poll

# ESPN answers or refuses this endpoint depending on the User-Agent and the
# machine asking: the same request that succeeds from one host returns 403 from
# another, and a browser string is refused as readily as a bot one. Since a
# refusal is indistinguishable from a week nobody has played, the fallback tries
# each agent known to have worked before giving up. This is exactly why Sleeper,
# which serves every other call this project makes, leads instead.
ESPN_USER_AGENTS = (
    None,                      # whatever the pooled client already announces
    "python-httpx/0.28.1",     # a plain client's default, which ESPN has accepted
)


def normalize_team(abbr: Optional[str]) -> Optional[str]:
    if not abbr:
        return None
    abbr = abbr.upper()
    return ESPN_TO_SLEEPER.get(abbr, abbr)


def parse_sleeper_scores(games: Any) -> Dict[str, str]:
    """
    Collapse Sleeper's scores response into {team: state}. Pure function.

    A cancelled game counts as finished, because nobody in it will score again.
    A postponed one counts as not started, which keeps the matchup open rather
    than declaring a winner over points that may still be coming.
    """
    states: Dict[str, str] = {}
    for game in games or []:
        if not isinstance(game, dict):
            continue
        meta = game.get("metadata") or {}
        status = str(game.get("status") or meta.get("status") or "").lower()
        home = normalize_team(meta.get("home_team"))
        away = normalize_team(meta.get("away_team"))
        if not home or not away:
            continue

        if status in ("complete", "closed", "final") or meta.get("is_over") or meta.get("canceled"):
            state = POST
        elif status in ("in_game", "inprogress", "in_progress", "halftime") or meta.get("is_in_progress"):
            state = IN
        elif meta.get("has_started") and status not in ("pre_game", "scheduled", "postponed"):
            state = IN
        else:
            state = PRE
        states[home] = state
        states[away] = state
    return states


def parse_scoreboard(data: Dict[str, Any]) -> Dict[str, str]:
    """
    Collapse an ESPN scoreboard response into {team: state}. Pure function.

    A postponed or cancelled game reports as post, which is right for this
    purpose: nobody on those teams is going to score any more points this week.
    """
    states: Dict[str, str] = {}
    for event in (data or {}).get("events") or []:
        try:
            status_type = (event.get("status") or {}).get("type") or {}
            state = status_type.get("state")
            if status_type.get("completed"):
                state = POST
            if state not in (PRE, IN, POST):
                state = PRE  # unknown means not finished
            for comp in event.get("competitions") or []:
                for side in comp.get("competitors") or []:
                    team = normalize_team((side.get("team") or {}).get("abbreviation"))
                    if team:
                        states[team] = state
        except Exception:  # noqa: BLE001  one malformed event must not sink the week
            continue
    return states


def _usable(states: Dict[str, str]) -> bool:
    return bool(states) and len(states) >= MIN_GAMES * 2


async def _fetch_states(season: str, week: int, season_type: str) -> Dict[str, Any]:
    """Try Sleeper, then ESPN. Returns the report shape described below."""
    errors: List[str] = []

    try:
        games = await client.request_json(
            SLEEPER_SCORES.format(season_type=season_type, season=season, week=week)
        )
        states = parse_sleeper_scores(games)
        if _usable(states):
            return {"states": states, "source": "sleeper", "error": None}
        errors.append(f"sleeper returned {len(states)} teams")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"sleeper: {client.describe_error(exc)}")

    for agent in ESPN_USER_AGENTS:
        try:
            data = await client.request_json(
                ESPN_SCOREBOARD,
                {"seasontype": 2, "week": week, "year": season},
                headers={"User-Agent": agent} if agent else None,
            )
            states = parse_scoreboard(data)
            if _usable(states):
                return {"states": states, "source": "espn", "error": None}
            errors.append(f"espn returned {len(states)} teams")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"espn: {client.describe_error(exc)}")

    return {"states": None, "source": None, "error": "; ".join(errors)}


async def get_week_game_states(
    season: str,
    week: int,
    season_type: str = "regular",
) -> Dict[str, Any]:
    """
    Game state for one week: {"states", "source", "error"}.

    ``states`` maps {team: "pre" | "in" | "post"} for every team playing this
    week; a team missing from it is on bye. It is None when neither provider
    could be read, which callers must treat as "nothing is final yet". The
    report is what the dashboard shows when that happens, so a provider outage
    is visible rather than silently looking like a week nobody has played.
    """
    try:
        return await cache.memory.get_or_fetch(
            f"gamestate:{season}:{season_type}:{week}",
            lambda: _fetch_states(season, week, season_type),
            TTL_GAME_STATE,
        )
    except Exception as exc:  # noqa: BLE001
        return {"states": None, "source": None, "error": client.describe_error(exc)}


async def get_team_states(
    season: str,
    week: int,
    season_type: str = "regular",
) -> Optional[Dict[str, str]]:
    """Just the {team: state} map, or None when it could not be read."""
    return (await get_week_game_states(season, week, season_type))["states"]


def starter_state(
    player_id: Any,
    players: Dict[str, Any],
    team_states: Optional[Dict[str, str]],
) -> str:
    """State of one starter slot: pre, in, post, or bye. Pure function."""
    if team_states is None:
        return PRE
    pid = str(player_id or "0")
    if pid in ("0", ""):
        return BYE  # empty slot: nothing left to score
    info = players.get(pid) or {}
    # Team defenses are keyed by their abbreviation and may have no record.
    team = normalize_team(info.get("team") or (pid if pid.isalpha() else None))
    if not team:
        return BYE  # free agent: not playing this week
    return team_states.get(team, BYE)


def summarize_states(states: List[str]) -> Dict[str, int]:
    total = len(states)
    done = sum(1 for s in states if s in DONE_STATES)
    playing = sum(1 for s in states if s == IN)
    return {"total": total, "done": done, "playing": playing, "yet": total - done - playing}
