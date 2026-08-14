"""
Markdown rendering for the MCP adapter.

Per ADR 001 this is the only place in the core allowed to produce strings meant
for a human (or a model) to read. league.py returns data; this turns data into
the markdown the MCP tools emit. The dashboard skips this module entirely and
serializes the same structures to JSON.
"""

from typing import Any, Dict, List


def _record(wins: int, losses: int, ties: int = 0) -> str:
    return f"{wins}-{losses}" + (f"-{ties}" if ties else "")


def all_play_table(payload: Dict[str, Any]) -> str:
    """Render the all play standings as a markdown table."""
    league = payload.get("league") or {}
    teams: List[Dict[str, Any]] = payload.get("teams") or []

    header = f"# 🥊 All Play Standings — {league.get('name', 'League')}"
    if payload.get("state") == "no_games_played":
        return (
            f"{header}\n\n"
            "No games have been played yet this season, so there is no all play "
            "record to compute. Check back after week 1."
        )

    weeks = payload.get("weeks_available") or []
    lines = [
        header,
        "",
        f"*Through week {max(weeks) if weeks else 0} — every team scored against "
        f"every other team, every week.*",
        "",
        "| # | Team | Real | All Play | AP% | Luck | Avg Pts |",
        "|---|------|------|----------|-----|------|---------|",
    ]

    for t in teams:
        luck = t.get("luck", 0.0)
        luck_str = f"{luck:+.1%}"
        if luck >= 0.10:
            luck_str += " 🍀"
        elif luck <= -0.10:
            luck_str += " 💀"
        lines.append(
            f"| {t.get('all_play_rank', '?')} "
            f"| {t.get('team_name', '?')} "
            f"| {_record(t.get('real_wins', 0), t.get('real_losses', 0), t.get('real_ties', 0))} "
            f"| {_record(t.get('all_play_wins', 0), t.get('all_play_losses', 0))} "
            f"| {t.get('all_play_pct', 0):.1%} "
            f"| {luck_str} "
            f"| {t.get('avg_points', 0):.1f} |"
        )

    lines += [
        "",
        "*Luck = real win% minus all play win%. Positive means the schedule has "
        "been kind; negative means the team has been losing games it scored well "
        "enough to win.*",
    ]

    luckiest = max(teams, key=lambda t: t.get("luck", 0), default=None)
    unluckiest = min(teams, key=lambda t: t.get("luck", 0), default=None)
    if luckiest and unluckiest and luckiest is not unluckiest:
        lines += [
            "",
            f"**Luckiest:** {luckiest['team_name']} "
            f"({luckiest['luck']:+.1%} above what their scoring earned)",
            f"**Unluckiest:** {unluckiest['team_name']} "
            f"({unluckiest['luck']:+.1%} below what their scoring earned)",
        ]

    return "\n".join(lines)


def head_to_head_matrix(payload: Dict[str, Any], limit: int = 12) -> str:
    """Render the everyone vs everyone grid as a markdown matrix."""
    teams: List[Dict[str, Any]] = (payload.get("teams") or [])[:limit]
    grid = payload.get("grid") or {}
    if not teams or not grid:
        return "No head to head data available yet."

    def short(name: str) -> str:
        return (name[:9] + "…") if len(name) > 10 else name

    lines = [
        f"# ⚔️ Head to Head Grid — {(payload.get('league') or {}).get('name', 'League')}",
        "",
        "*Row team's record against column team, if they played every week.*",
        "",
    ]

    head = "| vs | " + " | ".join(short(t["team_name"]) for t in teams) + " |"
    sep = "|---|" + "---|" * len(teams)
    lines += [head, sep]

    for a in teams:
        row = [short(a["team_name"])]
        for b in teams:
            if a["roster_id"] == b["roster_id"]:
                row.append("—")
                continue
            cell = (grid.get(str(a["roster_id"])) or {}).get(str(b["roster_id"]))
            row.append(f"{cell['wins']}-{cell['losses']}" if cell else "·")
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)
