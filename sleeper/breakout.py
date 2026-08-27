"""
The breakout radar: role change, measured in shares, before anyone notices.

Every other signal in this project answers "who is good right now". This one
answers "whose role is changing", which is the only question whose answer is
still cheap to act on. A player whose snap share, target share, air-yards share,
and red zone looks are all climbing is being promoted inside his own offense,
and that shows up in usage two to four weeks before it shows up in a box score
the rest of the league is reading.

Two design decisions carry most of the weight here:

**Shares, not counts.** A team that ran fifteen more plays than usual inflates
every raw number on its roster. Six targets means something very different on a
45-play afternoon than on a 75-play one. So every component is a share of the
team's own opportunity that week, which makes players on different offenses
comparable and strips out pace.

**Halves, not endpoints.** Comparing the first week of a window to the last
makes the whole score hostage to two games, one of which might have been a
blowout. Splitting the window in half and comparing averages is far steadier,
and when the window is odd the extra week goes to the recent half — that is the
half we actually care about.

The score is then gated on how hard the field is already adding the player. That
gate is the point of the tool, not a garnish: a rising role everyone has already
noticed is not a find, it is an auction.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Per component: the stat fields that make up a player's numerator, the team
# denominator they are shared against, the change (in percentage points of
# share) that earns a full score, and the smallest team total for which the
# share means anything at all.
#
# Caps are set at roughly the 90th percentile of observed absolute change, so
# about one in ten qualifying moves saturates. Setting them tighter — an
# earlier version of this used 15 for air share — compressed most of the board
# against the ceiling and made the ranking unable to separate a strong trend
# from an extraordinary one.
#
# min_denominator matters most for air yards, which is the one field that can be
# negative: targets behind the line of scrimmage subtract from a team's total.
# An offense with 6 net air yards on the day makes every receiver's "share"
# meaningless, and one such week can otherwise dominate a player's average.
COMPONENT_SPECS: Dict[str, Dict[str, Any]] = {
    "snap_share": {"fields": ["off_snp"], "denominator": "tm_off_snp",
                   "cap": 27.0, "min_denominator": 20.0},
    "opportunity_share": {"fields": None, "denominator": "team",
                          "cap": 19.0, "min_denominator": 10.0},
    "air_share": {"fields": ["rec_air_yd"], "denominator": "team",
                  "cap": 45.0, "min_denominator": 80.0},
    "rz_share": {"fields": ["rec_rz_tgt", "rush_rz_att"], "denominator": "team",
                 "cap": 21.0, "min_denominator": 2.0},
}

# Which components apply, and how much each counts, per position. Weights are
# renormalized over whatever components actually have data, so a player missing
# one is scored on the rest rather than silently penalized.
POSITION_PROFILES: Dict[str, Dict[str, Any]] = {
    "QB": {
        "opportunity_fields": ["pass_att", "rush_att"],
        "weights": {"snap_share": 0.3, "opportunity_share": 0.45, "rz_share": 0.25},
    },
    "RB": {
        "opportunity_fields": ["rush_att", "rec_tgt"],
        "weights": {"snap_share": 0.25, "opportunity_share": 0.35, "rz_share": 0.4},
    },
    "WR": {
        "opportunity_fields": ["rec_tgt"],
        "weights": {"snap_share": 0.2, "opportunity_share": 0.35, "air_share": 0.2, "rz_share": 0.25},
    },
    "TE": {
        "opportunity_fields": ["rec_tgt"],
        "weights": {"snap_share": 0.2, "opportunity_share": 0.35, "air_share": 0.2, "rz_share": 0.25},
    },
}

# Score points awarded per depth chart slot climbed, and the ceiling on that.
# A promotion is corroborating evidence for a usage trend, not a substitute for
# one — a player who moved up but is not being used more has not broken out.
DEPTH_MOVE_BONUS = 12.0
DEPTH_MOVE_CAP = 24.0

# How much of the score a fully hyped player forfeits. Deliberately harsher than
# the discount the trade-target tool applies, because "nobody has noticed yet"
# is this tool's entire premise rather than one factor among several.
ATTENTION_DISCOUNT = 0.6


def split_window(weeks: Sequence[int]) -> Tuple[List[int], List[int]]:
    """
    Split a week window into (earlier, recent) halves.

    An odd window gives the extra week to the recent half, since that is the one
    being asked about.
    """
    ordered = sorted(weeks)
    midpoint = len(ordered) // 2
    return ordered[:midpoint], ordered[midpoint:]


def team_totals(
    weekly_blobs: Dict[int, Dict[str, Any]],
    player_team: Dict[str, str],
    fields: Iterable[str],
) -> Dict[int, Dict[str, Dict[str, float]]]:
    """
    Sum each field by team for each week: {week: {team: {field: total}}}.

    Team membership comes from the current player map rather than the box score,
    which does not carry it. Over a window of a few weeks that is right nearly
    always; a player traded mid-window has his earlier weeks attributed to his
    new team, which understates his old share slightly. Not worth a second API
    call to fix for the handful of players it touches each season.
    """
    field_list = list(fields)
    totals: Dict[int, Dict[str, Dict[str, float]]] = {}
    for week, stats_by_pid in weekly_blobs.items():
        week_totals: Dict[str, Dict[str, float]] = {}
        for pid, stat in (stats_by_pid or {}).items():
            team = player_team.get(pid)
            if not team:
                continue
            bucket = week_totals.setdefault(team, {f: 0.0 for f in field_list})
            for field in field_list:
                bucket[field] += float(stat.get(field) or 0.0)
        totals[week] = week_totals
    return totals


def _numerator(stat: Dict[str, Any], fields: Sequence[str]) -> float:
    return sum(float(stat.get(f) or 0.0) for f in fields)


def _mean_share(
    pid: str,
    team: str,
    weeks: Sequence[int],
    weekly_blobs: Dict[int, Dict[str, Any]],
    totals: Dict[int, Dict[str, Dict[str, float]]],
    fields: Sequence[str],
    *,
    per_player_denominator: Optional[str] = None,
    min_denominator: float = 0.0,
) -> Optional[float]:
    """
    Average share of team opportunity across the given weeks, as a percentage.

    Weeks in which the player did not appear are skipped rather than counted as
    zero: a missed game is missing information, not a role change. Weeks whose
    team denominator is too small to divide by meaningfully are skipped for the
    same reason — a share of almost nothing is not a share.
    """
    shares = []
    for week in weeks:
        stat = (weekly_blobs.get(week) or {}).get(pid)
        if not stat:
            continue
        if per_player_denominator:
            denominator = float(stat.get(per_player_denominator) or 0.0)
        else:
            denominator = sum(
                (totals.get(week, {}).get(team, {}) or {}).get(f, 0.0) for f in fields
            )
        if denominator <= 0 or denominator < min_denominator:
            continue
        shares.append(_numerator(stat, fields) / denominator * 100.0)
    if not shares:
        return None
    return sum(shares) / len(shares)


def _mean_per_game(
    pid: str,
    weeks: Sequence[int],
    weekly_blobs: Dict[int, Dict[str, Any]],
    fields: Sequence[str],
) -> Tuple[Optional[float], int]:
    """Average raw opportunity per appearance, and the number of appearances."""
    values = []
    for week in weeks:
        stat = (weekly_blobs.get(week) or {}).get(pid)
        if not stat:
            continue
        values.append(_numerator(stat, fields))
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def trend_score(components: Dict[str, Dict[str, float]], weights: Dict[str, float]) -> float:
    """
    Weighted average of the normalized component deltas, scaled to 0-100.

    Weights are renormalized across only the components that produced a value,
    so a receiver with no air-yards data is judged on his other three rather
    than scored as though his air share collapsed.
    """
    usable = {
        name: comp for name, comp in components.items()
        if comp.get("normalized") is not None and name in weights
    }
    if not usable:
        return 0.0
    total_weight = sum(weights[name] for name in usable)
    if total_weight <= 0:
        return 0.0
    weighted = sum(usable[name]["normalized"] * weights[name] for name in usable)
    return weighted / total_weight * 100.0


def apply_gate(
    score: float,
    *,
    attention_norm: float = 0.0,
    depth_slots_gained: Optional[int] = None,
) -> Dict[str, float]:
    """
    Fold in the depth chart move and the obscurity gate.

    The depth bonus is added before the gate, not after, so that a promotion the
    whole league has already reacted to is discounted along with everything else.
    """
    depth_bonus = 0.0
    if depth_slots_gained and depth_slots_gained > 0:
        depth_bonus = min(DEPTH_MOVE_CAP, depth_slots_gained * DEPTH_MOVE_BONUS)

    pre_gate = score + depth_bonus
    multiplier = 1.0 - ATTENTION_DISCOUNT * max(0.0, min(1.0, attention_norm))
    return {
        "trend_score": score,
        "depth_bonus": depth_bonus,
        "pre_gate": pre_gate,
        "gate_multiplier": multiplier,
        "breakout_score": pre_gate * multiplier,
    }


def build_candidates(
    weekly_blobs: Dict[int, Dict[str, Any]],
    players: Dict[str, Any],
    weeks: Sequence[int],
    *,
    positions: Iterable[str] = ("QB", "RB", "WR", "TE"),
    min_recent_opportunity: float = 3.0,
    min_games_each_half: int = 1,
) -> List[Dict[str, Any]]:
    """
    Score every eligible player's role trend across the window.

    Returns one row per player carrying every component's before, after, and
    delta, so a caller can show why a player surfaced rather than asking to be
    trusted. Sorted by trend score descending; the attention gate is applied by
    the caller, which is where the trending data lives.

    min_recent_opportunity is the floor that keeps this from being a list of
    players who went from one touch to three. A share can double off a base too
    small to matter.
    """
    earlier, recent = split_window(weeks)
    if not earlier or not recent:
        return []

    wanted = {p for p in positions if p in POSITION_PROFILES}
    player_team = {
        pid: p.get("team")
        for pid, p in players.items()
        if p.get("position") in wanted and p.get("team")
    }
    if not player_team:
        return []

    # One pass over the box scores covers every denominator any component needs.
    denominator_fields = {"rec_tgt", "rush_att", "pass_att", "rec_air_yd", "rec_rz_tgt", "rush_rz_att"}
    totals = team_totals(weekly_blobs, player_team, denominator_fields)

    rows: List[Dict[str, Any]] = []
    for pid, team in player_team.items():
        player = players[pid]
        profile = POSITION_PROFILES[player["position"]]
        opportunity_fields = profile["opportunity_fields"]

        recent_opportunity, recent_games = _mean_per_game(
            pid, recent, weekly_blobs, opportunity_fields
        )
        _, earlier_games = _mean_per_game(pid, earlier, weekly_blobs, opportunity_fields)
        if recent_games < min_games_each_half or earlier_games < min_games_each_half:
            continue
        if recent_opportunity is None or recent_opportunity < min_recent_opportunity:
            continue

        components: Dict[str, Dict[str, float]] = {}
        for name, weight in profile["weights"].items():
            spec = COMPONENT_SPECS[name]
            fields = opportunity_fields if spec["fields"] is None else spec["fields"]
            per_player = spec["denominator"] if spec["denominator"] != "team" else None

            floor = spec.get("min_denominator", 0.0)
            before = _mean_share(pid, team, earlier, weekly_blobs, totals, fields,
                                 per_player_denominator=per_player, min_denominator=floor)
            after = _mean_share(pid, team, recent, weekly_blobs, totals, fields,
                                per_player_denominator=per_player, min_denominator=floor)
            if before is None or after is None:
                components[name] = {"before": before, "after": after,
                                    "delta": None, "normalized": None}
                continue
            delta = after - before
            components[name] = {
                "before": before,
                "after": after,
                "delta": delta,
                "normalized": max(-1.0, min(1.0, delta / spec["cap"])),
            }

        rows.append({
            "pid": pid,
            "player": player,
            "position": player["position"],
            "team": team,
            "games_earlier": earlier_games,
            "games_recent": recent_games,
            "recent_opportunity": recent_opportunity,
            "components": components,
            "trend_score": trend_score(components, profile["weights"]),
        })

    rows.sort(key=lambda r: r["trend_score"], reverse=True)
    return rows
