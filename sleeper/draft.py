"""
Live draft board model.

Sleeper exposes a draft as an immutable header (order, settings) plus a pick
list that grows one entry at a time. This module turns those into a board:
rounds x draft slots, every cell either a completed pick or the slot that is
next on the clock, plus an ADP-ranked list of who is still on the table.

The fetches below cache in the shared TTL store; the assembly on top of them is
a plain transform with no I/O, so the snake maths and the board layout unit
test directly without touching the network.
"""

from typing import Any, Dict, List, Optional, Tuple

from . import cache, client, config, league

# The positions that carry a redraft ADP worth showing on a board.
ADP_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]

# Sleeper parks players with no consensus ADP at 999/1000 on the projections
# endpoint. Treat anything at or above this as "unranked" rather than a real
# late pick.
_NO_ADP = 900.0


# ─────────────────────────────────────────────────────────────────────────────
# Fetches
# ─────────────────────────────────────────────────────────────────────────────


async def get_draft(league_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The league's most recent draft header, or None if none exists yet."""
    lid = league_id or await league.get_league_id()

    async def fetch() -> Optional[Dict[str, Any]]:
        drafts = await client.sleeper_get(f"/league/{lid}/drafts")
        return drafts[0] if drafts else None

    return await cache.memory.get_or_fetch(f"draft:{lid}", fetch, config.TTL_DRAFT)


async def get_draft_picks(draft_id: str, *, final: bool) -> List[Dict[str, Any]]:
    """
    Picks for a draft.

    A completed draft's pick list never changes, so it is cached forever. A live
    draft is fetched every call and left to the poller's cadence to rate limit —
    the endpoint is small and this is the one piece of draft state that is hot.
    """
    if not final:
        return await client.sleeper_get(f"/draft/{draft_id}/picks") or []
    return await cache.memory.get_or_fetch(
        f"draft_picks:{draft_id}",
        lambda: client.sleeper_get(f"/draft/{draft_id}/picks"),
        config.TTL_DRAFT_PICKS_FINAL,
    )


async def get_adp() -> Dict[str, Dict[str, Any]]:
    """
    Preseason ADP and a week-1 point projection per player, keyed by player_id.

    Sleeper carries average draft position on its projections endpoint under
    ``adp_dd_ppr`` — dynasty/devy PPR is the closest thing it publishes to a
    redraft consensus, and it tracks public ADP closely enough for a board.
    Cached for hours because preseason ADP moves slowly.
    """
    season = config.CURRENT_SEASON

    async def fetch() -> Dict[str, Dict[str, Any]]:
        params = [("position[]", pos) for pos in ADP_POSITIONS]
        data = await client.sleeper_get(
            f"/projections/nfl/regular/{season}/1", params
        )
        return data or {}

    return await cache.memory.get_or_fetch(
        f"draft_adp:{season}", fetch, config.TTL_DRAFT_ADP
    )


# ─────────────────────────────────────────────────────────────────────────────
# Snake maths (pure)
# ─────────────────────────────────────────────────────────────────────────────


def round_is_forward(rnd: int, snake: bool, reversal_round: int = 0) -> bool:
    """
    True when round ``rnd`` runs from slot 1 toward slot N.

    Plain snake alternates by round parity. ``reversal_round`` (Sleeper's third
    round reversal option) flips the parity from that round on; this is the
    common approximation and is exact for the standard 3RR case.
    """
    if not snake:
        return True
    forward = rnd % 2 == 1
    if reversal_round and rnd >= reversal_round:
        forward = not forward
    return forward


def slot_for_pick(
    pick_no: int, teams: int, snake: bool, reversal_round: int = 0
) -> Tuple[int, int]:
    """Return (draft_slot, round) for a 1-indexed overall pick number."""
    rnd = (pick_no - 1) // teams + 1
    idx = (pick_no - 1) % teams  # 0-based position within the round
    forward = round_is_forward(rnd, snake, reversal_round)
    return (idx + 1 if forward else teams - idx), rnd


def pick_no_for(
    rnd: int, slot: int, teams: int, snake: bool, reversal_round: int = 0
) -> int:
    """Inverse of :func:`slot_for_pick`: the overall pick number for a cell."""
    forward = round_is_forward(rnd, snake, reversal_round)
    idx = slot - 1 if forward else teams - slot
    return (rnd - 1) * teams + idx + 1


# ─────────────────────────────────────────────────────────────────────────────
# Assembly
# ─────────────────────────────────────────────────────────────────────────────


def _player_name(p: Dict[str, Any]) -> str:
    name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    return name or p.get("full_name") or "Unknown"


def _slot_directory(
    draft: Dict[str, Any],
    rosters: List[Dict[str, Any]],
    users: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    One row per draft slot: which team and manager pick there.

    ``draft_order`` (user_id -> slot) is only populated once the order is set —
    often at draft start. Before that every slot is just a number.
    """
    teams = (draft.get("settings") or {}).get("teams", 12)
    order = draft.get("draft_order") or {}
    slot_to_roster = {
        int(k): v for k, v in (draft.get("slot_to_roster_id") or {}).items()
    }
    user_by_id = {u["user_id"]: u for u in users}
    roster_by_owner = {r.get("owner_id"): r for r in rosters}
    slot_to_user = {slot: uid for uid, slot in order.items()}

    rows: List[Dict[str, Any]] = []
    for slot in range(1, teams + 1):
        uid = slot_to_user.get(slot)
        user = user_by_id.get(uid) or {}
        meta = user.get("metadata") or {}
        rid = slot_to_roster.get(slot)
        if rid is None and uid:
            roster = roster_by_owner.get(uid)
            rid = roster.get("roster_id") if roster else None
        rows.append({
            "slot": slot,
            "roster_id": rid,
            "team_name": (
                meta.get("team_name") or user.get("display_name") or f"Slot {slot}"
            ),
            "manager": user.get("display_name") or "—",
        })
    return rows


def _pick_cell(pick: Dict[str, Any], players: Dict[str, Any]) -> Dict[str, Any]:
    meta = pick.get("metadata") or {}
    p = players.get(pick.get("player_id")) or {}
    first = meta.get("first_name") or p.get("first_name") or ""
    last = meta.get("last_name") or p.get("last_name") or ""
    name = f"{first} {last}".strip() or p.get("full_name") or "—"
    return {
        "pick_no": pick.get("pick_no"),
        "round": pick.get("round"),
        "slot": pick.get("draft_slot"),
        "roster_id": pick.get("roster_id"),
        "player": name,
        "pos": meta.get("position") or p.get("position") or "?",
        "team": meta.get("team") or p.get("team") or "FA",
        "is_keeper": bool(pick.get("is_keeper")),
    }


def _best_available(
    players: Dict[str, Any],
    adp: Dict[str, Dict[str, Any]],
    drafted_ids: set,
    limit: int,
) -> List[Dict[str, Any]]:
    """Undrafted players ranked by ADP, with a positional rank tag."""
    rows: List[Dict[str, Any]] = []
    for pid, p in players.items():
        if pid in drafted_ids:
            continue
        pos = p.get("position")
        if pos not in ADP_POSITIONS:
            continue
        # DEF entries in the player map carry no active flag or NFL team slot.
        if pos != "DEF" and (not p.get("active") or not p.get("team")):
            continue

        entry = adp.get(pid) or {}
        raw_adp = entry.get("adp_dd_ppr")
        has_adp = raw_adp is not None and raw_adp < _NO_ADP
        if has_adp:
            sort_val = raw_adp
        else:
            # No consensus ADP: fall back to Sleeper's own search_rank so known
            # players still sort ahead of camp-body noise, but always behind
            # anyone who has a real ADP.
            sr = p.get("search_rank")
            sort_val = (sr + 1000) if sr else 99999

        proj = entry.get("pts_ppr")
        rows.append({
            "player_id": pid,
            "player": _player_name(p),
            "pos": pos,
            "team": p.get("team") or "FA",
            "adp": round(raw_adp, 1) if has_adp else None,
            "proj": round(proj, 1) if proj else None,
            "_sort": sort_val,
        })

    rows.sort(key=lambda r: r["_sort"])
    seen: Dict[str, int] = {}
    for r in rows:
        seen[r["pos"]] = seen.get(r["pos"], 0) + 1
        r["pos_rank"] = f"{r['pos']}{seen[r['pos']]}"
        r.pop("_sort", None)
    return rows[:limit]


def build_board(
    draft: Dict[str, Any],
    picks: List[Dict[str, Any]],
    players: Dict[str, Any],
    slots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    The rounds x slots grid plus the on-the-clock pointer. Pure.

    Picks are assumed to arrive in overall order with no gaps, which is how
    Sleeper writes them; the next pick is therefore ``len(picks) + 1``.
    """
    settings = draft.get("settings") or {}
    teams = settings.get("teams", 12)
    rounds = settings.get("rounds", 15)
    snake = draft.get("type") == "snake"
    reversal_round = settings.get("reversal_round") or 0
    status = draft.get("status", "unknown")
    total_picks = teams * rounds

    made = {
        (p.get("round"), p.get("draft_slot")): _pick_cell(p, players)
        for p in picks
        if p.get("round") and p.get("draft_slot")
    }
    picks_made = len(picks)

    slot_name = {s["slot"]: s["team_name"] for s in slots}
    slot_rid = {s["slot"]: s["roster_id"] for s in slots}

    on_clock: Optional[Dict[str, Any]] = None
    if status in ("drafting", "paused") and picks_made < total_picks:
        next_no = picks_made + 1
        slot, rnd = slot_for_pick(next_no, teams, snake, reversal_round)
        on_clock = {
            "pick_no": next_no,
            "round": rnd,
            "slot": slot,
            "team_name": slot_name.get(slot, f"Slot {slot}"),
            "roster_id": slot_rid.get(slot),
        }

    board: List[Dict[str, Any]] = []
    for rnd in range(1, rounds + 1):
        row: List[Dict[str, Any]] = []
        for slot in range(1, teams + 1):
            cell = made.get((rnd, slot))
            if cell is None:
                cell = {
                    "pick_no": pick_no_for(rnd, slot, teams, snake, reversal_round),
                    "round": rnd,
                    "slot": slot,
                    "player": None,
                }
            cell["on_clock"] = bool(
                on_clock
                and on_clock["round"] == rnd
                and on_clock["slot"] == slot
            )
            row.append(cell)
        board.append({
            "round": rnd,
            "forward": round_is_forward(rnd, snake, reversal_round),
            "picks": row,
        })

    return {
        "board": board,
        "on_clock": on_clock,
        "picks_made": picks_made,
        "total_picks": total_picks,
    }


async def build_draft_payload(league_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Everything the dashboard's draft tab needs, as plain JSON.

    Returns ``{"exists": False}`` when the league has no draft, so the web layer
    can simply hide the tab.
    """
    lid = league_id or await league.get_league_id()
    draft = await get_draft(lid)
    if not draft:
        return {"exists": False}

    status = draft.get("status", "unknown")
    settings = draft.get("settings") or {}

    picks, players, rosters, users, adp = await client.gather(
        get_draft_picks(draft["draft_id"], final=(status == "complete")),
        league.get_players(),
        league.get_rosters(lid),
        league.get_users(lid),
        get_adp(),
    )

    slots = _slot_directory(draft, rosters, users)
    board = build_board(draft, picks, players, slots)

    drafted_ids = {p.get("player_id") for p in picks if p.get("player_id")}
    best_available = _best_available(players, adp, drafted_ids, limit=18)

    return {
        "exists": True,
        "status": status,
        "type": draft.get("type"),
        "name": (draft.get("metadata") or {}).get("name"),
        "teams": settings.get("teams", 12),
        "rounds": settings.get("rounds", 15),
        "reversal_round": settings.get("reversal_round") or 0,
        "pick_timer": settings.get("pick_timer"),
        "last_picked": draft.get("last_picked"),
        "start_time": draft.get("start_time"),
        "slots": slots,
        "best_available": best_available,
        **board,
    }
