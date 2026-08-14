"""
League data access and the all play model.

Everything here returns typed structures, never display strings. Rendering to
markdown for the MCP tools lives in render.py; rendering to JSON for the
dashboard happens in web/app.py.

The all play grid is the core idea: rather than only scoring a team against its
scheduled opponent, score it against every other team's score that same week. In
a 12 team league that is 11 phantom matchups per team per week. The gap between
a team's real record and its all play record is schedule luck.
"""

from typing import Any, Dict, List, Optional, Tuple

from . import cache, client, config

# ─────────────────────────────────────────────────────────────────────────────
# Primitive fetches, each with the TTL its data class warrants
# ─────────────────────────────────────────────────────────────────────────────


async def get_nfl_state() -> Dict[str, Any]:
    """Current season, week, and season type. Short TTL: this drives everything."""
    return await cache.memory.get_or_fetch(
        "nfl_state",
        lambda: client.sleeper_get("/state/nfl"),
        config.TTL_NFL_STATE,
    )


async def get_user_id(username: Optional[str] = None) -> str:
    name = username or config.SLEEPER_USERNAME
    if not name:
        raise client.SleeperAPIError(
            "No Sleeper username configured. "
            "Call sleeper_set_username with your Sleeper display name first."
        )

    async def fetch() -> str:
        data = await client.sleeper_get(f"/user/{name}")
        if not data or not data.get("user_id"):
            raise client.SleeperAPIError(f"No Sleeper user found for '{name}'")
        return data["user_id"]

    return await cache.memory.get_or_fetch(f"user_id:{name}", fetch, config.TTL_LEAGUE)


async def get_league(season: Optional[str] = None) -> Dict[str, Any]:
    """
    Resolve the league object.

    If SLEEPER_LEAGUE_ID is configured, fetch it directly. Otherwise list the
    user's leagues for the season and match on the configured name fragment,
    falling back to the first league.
    """
    season = season or config.CURRENT_SEASON

    async def fetch() -> Dict[str, Any]:
        if config.LEAGUE_ID:
            return await client.sleeper_get(f"/league/{config.LEAGUE_ID}")

        user_id = await get_user_id()
        leagues = await client.sleeper_get(f"/user/{user_id}/leagues/nfl/{season}")
        if not leagues:
            raise client.SleeperAPIError(
                f"No leagues found for {config.SLEEPER_USERNAME} in {season}."
            )
        match = config.LEAGUE_NAME_MATCH.lower()
        if not match:
            raise client.SleeperAPIError(
                "No league configured. "
                "Call sleeper_set_league with a league name or league ID first."
            )
        return next(
            (lg for lg in leagues if match in (lg.get("name") or "").lower()),
            leagues[0],
        )

    return await cache.memory.get_or_fetch(
        f"league:{season}", fetch, config.TTL_LEAGUE
    )


async def get_league_id(season: Optional[str] = None) -> str:
    return (await get_league(season))["league_id"]


async def get_rosters(league_id: Optional[str] = None) -> List[Dict[str, Any]]:
    lid = league_id or await get_league_id()
    return await cache.memory.get_or_fetch(
        f"rosters:{lid}",
        lambda: client.sleeper_get(f"/league/{lid}/rosters"),
        config.TTL_ROSTERS,
    )


async def get_users(league_id: Optional[str] = None) -> List[Dict[str, Any]]:
    lid = league_id or await get_league_id()
    return await cache.memory.get_or_fetch(
        f"users:{lid}",
        lambda: client.sleeper_get(f"/league/{lid}/users"),
        config.TTL_LEAGUE,
    )


async def get_players() -> Dict[str, Any]:
    """
    The full ~5 MB Sleeper player map.

    Sleeper's docs say fetch this at most once per day and store it yourself, so
    it is cached both in memory and on disk. The disk copy is what makes a
    server restart cheap instead of a 5 MB download.
    """
    hit = cache.memory.get("players")
    if hit is not None:
        return hit

    on_disk = cache.disk.get("players_nfl", config.TTL_PLAYERS)
    if on_disk is not None:
        cache.memory.set("players", on_disk, config.TTL_PLAYERS)
        return on_disk

    data = await client.sleeper_get("/players/nfl")
    cache.memory.set("players", data, config.TTL_PLAYERS)
    cache.disk.set("players_nfl", data)
    return data


async def get_matchups(
    week: int,
    league_id: Optional[str] = None,
    *,
    is_final: bool = False,
) -> List[Dict[str, Any]]:
    """
    Matchups for one week.

    The is_final split is the highest leverage caching decision in the project:
    a completed week never changes, so it is cached forever and an entire season
    of history costs at most 17 upstream calls. Only the current week is hot.
    """
    lid = league_id or await get_league_id()
    ttl = config.TTL_MATCHUPS_FINAL if is_final else config.TTL_MATCHUPS_LIVE
    return await cache.memory.get_or_fetch(
        f"matchups:{lid}:{week}",
        lambda: client.sleeper_get(f"/league/{lid}/matchups/{week}"),
        ttl,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Derived views
# ─────────────────────────────────────────────────────────────────────────────


async def get_team_directory(league_id: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
    """
    Map roster_id to display information for every team in the league.

    Prefers the manager's custom team name, then their display name, then a
    generic label, because Sleeper leaves team_name unset unless it was edited.
    """
    lid = league_id or await get_league_id()
    rosters, users = await client.gather(get_rosters(lid), get_users(lid))
    user_by_id = {u["user_id"]: u for u in users}

    directory: Dict[int, Dict[str, Any]] = {}
    for roster in rosters:
        rid = roster["roster_id"]
        user = user_by_id.get(roster.get("owner_id")) or {}
        meta = user.get("metadata") or {}
        settings = roster.get("settings") or {}
        directory[rid] = {
            "roster_id": rid,
            "owner_id": roster.get("owner_id"),
            "team_name": (
                meta.get("team_name")
                or user.get("display_name")
                or f"Roster {rid}"
            ),
            "manager": user.get("display_name") or "Unknown",
            "avatar": user.get("avatar"),
            "wins": settings.get("wins", 0),
            "losses": settings.get("losses", 0),
            "ties": settings.get("ties", 0),
            "points_for": settings.get("fpts", 0) + settings.get("fpts_decimal", 0) / 100,
            "points_against": (
                settings.get("fpts_against", 0)
                + settings.get("fpts_against_decimal", 0) / 100
            ),
        }
    return directory


async def get_completed_week_range(
    league_id: Optional[str] = None,
) -> Tuple[int, int]:
    """
    Return (last_completed_week, current_week) for the league being viewed.

    The league's own state takes priority over global NFL state. A finished
    league from a prior season is fully complete no matter what week the live
    NFL calendar says, and reading the live calendar for a historical league was
    an early bug that reported zero weeks played for a season with a champion.
    """
    league, state = await client.gather(get_league(), get_nfl_state())
    settings = league.get("settings") or {}
    playoff_start = settings.get("playoff_week_start") or 15
    last_regular = max(playoff_start - 1, 1)

    status = (league.get("status") or "").lower()
    league_season = str(league.get("season") or "")
    live_season = str(state.get("league_season") or state.get("season") or "")

    # A completed league, or any league from a season that has already ended,
    # has its full regular season on the books.
    if status == "complete" or (league_season and live_season and league_season < live_season):
        return last_regular, last_regular

    # Drafted but not yet playing, or still in the preseason.
    if status in ("pre_draft", "drafting") or state.get("season_type") == "pre":
        return 0, 0

    current = int(state.get("display_week") or state.get("week") or 1)
    current = min(current, last_regular)
    # The current week is still in progress, so the last completed one is before it.
    return max(current - 1, 0), current


async def get_weekly_scores(
    through_week: int,
    league_id: Optional[str] = None,
    current_week: Optional[int] = None,
) -> Dict[int, Dict[int, float]]:
    """
    Scores for every roster for weeks 1..through_week.

    Returns {week: {roster_id: points}}. Weeks before current_week are treated
    as final and cached permanently; the current week is fetched live.
    """
    if through_week < 1:
        return {}
    lid = league_id or await get_league_id()

    weeks = list(range(1, through_week + 1))
    results = await client.gather(*[
        get_matchups(w, lid, is_final=(current_week is None or w < current_week))
        for w in weeks
    ])

    scores: Dict[int, Dict[int, float]] = {}
    for week_no, entries in zip(weeks, results):
        week_scores = {
            m["roster_id"]: float(m.get("points") or 0.0)
            for m in (entries or [])
            if m.get("roster_id") is not None
        }
        # A week where nobody scored has not been played; excluding it keeps
        # preseason and future weeks from polluting the all play record.
        if week_scores and any(v > 0 for v in week_scores.values()):
            scores[week_no] = week_scores
    return scores


async def get_real_results(
    through_week: int,
    league_id: Optional[str] = None,
    current_week: Optional[int] = None,
) -> Dict[int, List[Dict[str, Any]]]:
    """
    Actual head to head results by week, derived from matchup_id pairing.

    Returns {week: [{roster_id, opponent_id, points, opponent_points, result}]}.
    """
    if through_week < 1:
        return {}
    lid = league_id or await get_league_id()

    weeks = list(range(1, through_week + 1))
    results = await client.gather(*[
        get_matchups(w, lid, is_final=(current_week is None or w < current_week))
        for w in weeks
    ])

    by_week: Dict[int, List[Dict[str, Any]]] = {}
    for week_no, entries in zip(weeks, results):
        if not entries:
            continue
        pairs: Dict[Any, List[Dict[str, Any]]] = {}
        for m in entries:
            pairs.setdefault(m.get("matchup_id"), []).append(m)

        week_rows: List[Dict[str, Any]] = []
        for matchup_id, side in pairs.items():
            if matchup_id is None or len(side) != 2:
                continue  # bye weeks and malformed entries
            a, b = side
            pa = float(a.get("points") or 0.0)
            pb = float(b.get("points") or 0.0)
            for me, them, mine, theirs in ((a, b, pa, pb), (b, a, pb, pa)):
                week_rows.append({
                    "roster_id": me["roster_id"],
                    "opponent_id": them["roster_id"],
                    "points": mine,
                    "opponent_points": theirs,
                    "result": "W" if mine > theirs else "L" if mine < theirs else "T",
                })
        if week_rows and any(r["points"] > 0 for r in week_rows):
            by_week[week_no] = week_rows
    return by_week


# ─────────────────────────────────────────────────────────────────────────────
# All play
# ─────────────────────────────────────────────────────────────────────────────


def compute_all_play(
    weekly_scores: Dict[int, Dict[int, float]],
) -> Dict[int, Dict[str, Any]]:
    """
    Compute the all play record for every roster.

    Pure function: takes {week: {roster_id: points}} and returns per roster
    totals. Kept free of I/O so it is directly unit testable, which the original
    codebase had no equivalent of.
    """
    roster_ids = {rid for week in weekly_scores.values() for rid in week}
    totals: Dict[int, Dict[str, Any]] = {
        rid: {
            "roster_id": rid,
            "all_play_wins": 0,
            "all_play_losses": 0,
            "all_play_ties": 0,
            "points_for": 0.0,
            "weeks_played": 0,
            "best_week": 0.0,
            "worst_week": None,
        }
        for rid in roster_ids
    }

    for scores in weekly_scores.values():
        for rid, pts in scores.items():
            row = totals[rid]
            row["points_for"] += pts
            row["weeks_played"] += 1
            row["best_week"] = max(row["best_week"], pts)
            row["worst_week"] = pts if row["worst_week"] is None else min(row["worst_week"], pts)
            for other, other_pts in scores.items():
                if other == rid:
                    continue
                if pts > other_pts:
                    row["all_play_wins"] += 1
                elif pts < other_pts:
                    row["all_play_losses"] += 1
                else:
                    row["all_play_ties"] += 1

    for row in totals.values():
        games = row["all_play_wins"] + row["all_play_losses"] + row["all_play_ties"]
        row["all_play_games"] = games
        row["all_play_pct"] = (
            (row["all_play_wins"] + 0.5 * row["all_play_ties"]) / games if games else 0.0
        )
        row["points_for"] = round(row["points_for"], 2)
        row["avg_points"] = (
            round(row["points_for"] / row["weeks_played"], 2) if row["weeks_played"] else 0.0
        )
        row["worst_week"] = row["worst_week"] or 0.0

    return totals


def compute_head_to_head_grid(
    weekly_scores: Dict[int, Dict[int, float]],
) -> Dict[int, Dict[int, Dict[str, Any]]]:
    """
    The everyone vs everyone matrix.

    grid[a][b] describes how team a fared against team b across every week, as
    if they played each other every single week. Pure function.
    """
    roster_ids = sorted({rid for week in weekly_scores.values() for rid in week})
    grid: Dict[int, Dict[int, Dict[str, Any]]] = {
        a: {
            b: {"wins": 0, "losses": 0, "ties": 0, "margin": 0.0}
            for b in roster_ids
            if b != a
        }
        for a in roster_ids
    }

    for scores in weekly_scores.values():
        present = [rid for rid in roster_ids if rid in scores]
        for a in present:
            for b in present:
                if a == b:
                    continue
                diff = scores[a] - scores[b]
                cell = grid[a][b]
                cell["margin"] += diff
                if diff > 0:
                    cell["wins"] += 1
                elif diff < 0:
                    cell["losses"] += 1
                else:
                    cell["ties"] += 1

    for row in grid.values():
        for cell in row.values():
            cell["margin"] = round(cell["margin"], 2)
            played = cell["wins"] + cell["losses"] + cell["ties"]
            cell["avg_margin"] = round(cell["margin"] / played, 2) if played else 0.0
    return grid


def compute_luck(
    all_play: Dict[int, Dict[str, Any]],
    real_records: Dict[int, Dict[str, int]],
) -> Dict[int, float]:
    """
    Luck is real win percentage minus all play win percentage.

    Positive means the schedule has been kind: the team won more than its scoring
    deserved. Negative means it has been beaten by good opponents on good weeks.
    """
    luck: Dict[int, float] = {}
    for rid, row in all_play.items():
        real = real_records.get(rid) or {}
        real_games = real.get("wins", 0) + real.get("losses", 0) + real.get("ties", 0)
        real_pct = (
            (real.get("wins", 0) + 0.5 * real.get("ties", 0)) / real_games
            if real_games
            else 0.0
        )
        luck[rid] = round(real_pct - row["all_play_pct"], 4)
    return luck


def summarize_real_records(
    real_results: Dict[int, List[Dict[str, Any]]],
) -> Dict[int, Dict[str, int]]:
    """Collapse weekly head to head rows into a win/loss/tie record per roster."""
    records: Dict[int, Dict[str, int]] = {}
    for rows in real_results.values():
        for row in rows:
            rec = records.setdefault(
                row["roster_id"], {"wins": 0, "losses": 0, "ties": 0}
            )
            if row["result"] == "W":
                rec["wins"] += 1
            elif row["result"] == "L":
                rec["losses"] += 1
            else:
                rec["ties"] += 1
    return records


async def build_dashboard_payload(
    league_id: Optional[str] = None,
    season: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Assemble everything the dashboard needs in one shot.

    This is the single function the web layer calls. It returns plain JSON
    serializable data with no rendering decisions baked in.
    """
    league = await get_league(season)
    lid = league_id or league["league_id"]

    last_completed, current_week = await get_completed_week_range(lid)
    through = max(last_completed, current_week)

    directory = await get_team_directory(lid)

    if through < 1:
        return {
            "league": {
                "id": lid,
                "name": league.get("name"),
                "season": league.get("season"),
                "status": league.get("status"),
                "total_rosters": league.get("total_rosters"),
            },
            "current_week": current_week,
            "last_completed_week": last_completed,
            "weeks_available": [],
            "teams": _rank_teams({}, {}, {}, directory),
            "grid": {},
            "weekly_scores": {},
            "state": "no_games_played",
        }

    weekly_scores, real_results = await client.gather(
        get_weekly_scores(through, lid, current_week),
        get_real_results(through, lid, current_week),
    )

    all_play = compute_all_play(weekly_scores)
    grid = compute_head_to_head_grid(weekly_scores)
    real_records = summarize_real_records(real_results)
    luck = compute_luck(all_play, real_records)

    return {
        "league": {
            "id": lid,
            "name": league.get("name"),
            "season": league.get("season"),
            "status": league.get("status"),
            "total_rosters": league.get("total_rosters"),
        },
        "current_week": current_week,
        "last_completed_week": last_completed,
        "weeks_available": sorted(weekly_scores.keys()),
        "teams": _rank_teams(all_play, real_records, luck, directory),
        "grid": {str(a): {str(b): c for b, c in row.items()} for a, row in grid.items()},
        "weekly_scores": {
            str(w): {str(r): p for r, p in s.items()} for w, s in weekly_scores.items()
        },
        "state": "ok",
    }


def _rank_teams(
    all_play: Dict[int, Dict[str, Any]],
    real_records: Dict[int, Dict[str, int]],
    luck: Dict[int, float],
    directory: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge every per team metric into one list, sorted by all play percentage."""
    rows: List[Dict[str, Any]] = []
    for rid, info in directory.items():
        ap = all_play.get(rid, {})
        real = real_records.get(rid) or {
            "wins": info.get("wins", 0),
            "losses": info.get("losses", 0),
            "ties": info.get("ties", 0),
        }
        rows.append({
            **info,
            "real_wins": real.get("wins", 0),
            "real_losses": real.get("losses", 0),
            "real_ties": real.get("ties", 0),
            "all_play_wins": ap.get("all_play_wins", 0),
            "all_play_losses": ap.get("all_play_losses", 0),
            "all_play_ties": ap.get("all_play_ties", 0),
            "all_play_pct": ap.get("all_play_pct", 0.0),
            "avg_points": ap.get("avg_points", 0.0),
            "best_week": ap.get("best_week", 0.0),
            "worst_week": ap.get("worst_week", 0.0),
            "weeks_played": ap.get("weeks_played", 0),
            "luck": luck.get(rid, 0.0),
        })

    rows.sort(key=lambda r: (r["all_play_pct"], r["avg_points"]), reverse=True)
    for i, row in enumerate(rows, start=1):
        row["all_play_rank"] = i
    return rows
