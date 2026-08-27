"""
The FAAB market, reconstructed from league transactions.

Sleeper's transactions endpoint is the most underused thing in its API. It
records every add, drop, and waiver claim in the league — including the ones
that *failed*, with the bid attached. Failed claims are the valuable half: a
winning bid tells you what one manager was willing to pay, while the losing bids
underneath it tell you what the player actually cleared at and how many rivals
wanted him.

Two things fall out of that, and neither is available anywhere else:

  - Real prices. "Bid aggressively" is not advice. "The last four RBs of this
    tier went for 14, 18, 9, and 22, and the highest losing bid all season was
    31" is.
  - Drop mining. Every drop is timestamped, so a player cut three days ago whose
    role has since improved is findable — free talent, already passed over.

Unlike the snapshot store, none of this needs history to accumulate. The
transactions endpoint is retroactive: a league's entire season is available the
first time you ask.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence

# Sleeper reports transaction timestamps in epoch milliseconds.
MS_PER_DAY = 86_400_000


def _bid(transaction: Dict[str, Any]) -> Optional[int]:
    settings = transaction.get("settings") or {}
    value = settings.get("waiver_bid")
    return int(value) if value is not None else None


def percentiles(values: Sequence[float], points: Iterable[float] = (0.5, 0.75, 0.9)) -> Dict[str, float]:
    """
    Nearest-rank percentiles of a small sample.

    Deliberately not interpolated: these are dollar bids in a league of twelve,
    so reporting a clearing price of $13.50 would imply a precision the sample
    does not have.
    """
    ordered = sorted(values)
    if not ordered:
        return {}
    out = {}
    for point in points:
        index = min(len(ordered) - 1, max(0, int(round(point * len(ordered))) - 1))
        out[f"p{int(point * 100)}"] = ordered[index]
    return out


def bid_distribution(transactions: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """
    What waiver claims have actually cost in this league.

    Splits winning from losing bids. The losing side is what tells you whether a
    price was contested or simply the only bid on the board.
    """
    won: List[int] = []
    lost: List[int] = []
    for t in transactions:
        if t.get("type") != "waiver":
            continue
        amount = _bid(t)
        if amount is None:
            continue
        (won if t.get("status") == "complete" else lost).append(amount)

    return {
        "won": won,
        "lost": lost,
        "claims": len(won) + len(lost),
        "won_percentiles": percentiles(won),
        "max_won": max(won, default=0),
        "max_lost": max(lost, default=0),
        "free_claims": sum(1 for b in won if b == 0),
    }


def budget_state(
    rosters: Iterable[Dict[str, Any]],
    total_budget: int,
) -> Dict[int, Dict[str, Any]]:
    """
    Remaining FAAB per roster, from waiver_budget_used.

    Knowing a rival is down to $4 is the difference between winning a claim for
    $6 and overpaying $40 for it.
    """
    out: Dict[int, Dict[str, Any]] = {}
    for roster in rosters:
        settings = roster.get("settings") or {}
        used = int(settings.get("waiver_budget_used") or 0)
        roster_id = roster.get("roster_id")
        if roster_id is None:
            continue
        out[roster_id] = {
            "roster_id": roster_id,
            "owner_id": roster.get("owner_id"),
            "used": used,
            "remaining": max(0, total_budget - used),
            "pct_remaining": (max(0, total_budget - used) / total_budget * 100) if total_budget else 0.0,
        }
    return out


def player_bid_history(transactions: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Per-player claim history: {player_id: {winning_bid, losing_bids, contenders}}.

    `contenders` counts distinct rosters that bid at all, which is the honest
    measure of how contested a player was — a $30 winning bid against nobody is
    a very different market signal from $30 against four rivals.
    """
    history: Dict[str, Dict[str, Any]] = {}
    for t in transactions:
        if t.get("type") != "waiver":
            continue
        amount = _bid(t)
        for pid in (t.get("adds") or {}):
            entry = history.setdefault(
                pid, {"winning_bid": None, "losing_bids": [], "contenders": set(), "week": t.get("leg")}
            )
            for roster_id in (t.get("roster_ids") or []):
                entry["contenders"].add(roster_id)
            if amount is None:
                continue
            if t.get("status") == "complete":
                entry["winning_bid"] = amount
            else:
                entry["losing_bids"].append(amount)

    for entry in history.values():
        entry["contenders"] = len(entry["contenders"])
        entry["losing_bids"].sort(reverse=True)
    return history


def recent_drops(
    transactions: Iterable[Dict[str, Any]],
    *,
    since_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Players dropped in the league, most recent first.

    Only successful transactions count — a failed waiver claim never actually
    dropped anybody, and counting those would fill the list with players who are
    still rostered.
    """
    drops: List[Dict[str, Any]] = []
    for t in transactions:
        if t.get("status") != "complete":
            continue
        created = t.get("created") or 0
        if since_ms is not None and created < since_ms:
            continue
        for pid, roster_id in (t.get("drops") or {}).items():
            drops.append({
                "player_id": pid,
                "roster_id": roster_id,
                "week": t.get("leg"),
                "created": created,
                "type": t.get("type"),
            })
    drops.sort(key=lambda d: d["created"], reverse=True)
    return drops


def still_dropped(drops: Iterable[Dict[str, Any]], rostered: Iterable[str]) -> List[Dict[str, Any]]:
    """
    Filter a drop list to players nobody has picked back up.

    A player dropped and re-added the same day is noise; one dropped a week ago
    and still sitting there is the actual opportunity.
    """
    taken = set(rostered)
    seen = set()
    out = []
    for drop in drops:
        pid = drop["player_id"]
        if pid in taken or pid in seen:
            continue
        seen.add(pid)
        out.append(drop)
    return out


def suggest_bid(
    distribution: Dict[str, Any],
    total_budget: int,
    remaining: int,
    *,
    aggression: float = 0.75,
) -> Dict[str, Any]:
    """
    A bid anchored to this league's own clearing prices, not a rule of thumb.

    aggression picks which percentile of historical winning bids to target.
    The result is capped at what the manager actually has left, and reported
    alongside the highest losing bid on record — the number that says what it
    takes to not be outbid on something contested.
    """
    won = distribution.get("won") or []
    if not won:
        return {"suggested": None, "reason": "no completed waiver claims on record yet"}

    key = f"p{int(aggression * 100)}"
    anchor = distribution["won_percentiles"].get(key)
    if anchor is None:
        anchor = percentiles(won, (aggression,)).get(key, 0)

    suggested = int(min(remaining, max(1, round(anchor))))
    return {
        "suggested": suggested,
        "anchor_percentile": key,
        "anchor": anchor,
        "max_losing_bid": distribution.get("max_lost", 0),
        "capped_by_budget": suggested >= remaining and remaining < round(anchor),
        "pct_of_budget": (suggested / total_budget * 100) if total_budget else 0.0,
    }
