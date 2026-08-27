"""
Betting market game environment: implied team totals from spread and total.

The strength-of-schedule bonus this server already computes is defense-quality
based — it ranks opponents by fantasy points allowed. That is a reasonable
four-week signal but a poor one-week signal, because it is unadjusted for volume
and pace and knows nothing about game script.

The betting market knows all of it, and prices it continuously. A spread and an
over/under decompose into what each team is expected to score:

    favorite_total = total/2 + |spread|/2
    underdog_total = total/2 - |spread|/2

That number is the single best public estimate of how many points an offense
will put up on Sunday, and it is free. The two signals do different jobs and
both are kept: defense-based SOS covers the multi-week horizon (no line exists
for Week 9 yet), while implied total covers the week in front of you.

Line movement matters too. A total that opened at 41 and sits at 47 means the
market learned something — usually about weather, or about who is playing.
"""

from typing import Any, Dict, Optional, Tuple

# League-average implied team total. Bonuses are symmetric around this.
BASELINE_TOTAL = 22.5

# Implied totals realistically span roughly 15 to 30. A team this far above or
# below the baseline earns the full bonus; beyond it, the bonus is capped.
TOTAL_SPAN = 7.0

# Movement of at least this many points is worth calling out rather than noise.
NOTABLE_LINE_MOVE = 1.5


def implied_totals(
    spread: Optional[float],
    over_under: Optional[float],
    *,
    home_favorite: Optional[bool] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """
    (home_total, away_total) from a spread and a game total.

    ESPN reports `spread` from the home team's perspective (negative means home
    is favored), but rather than trust that sign we prefer the explicit
    favorite flag when it is present, and fall back to the sign only when it is
    not. Getting this backwards would invert every recommendation the tool
    makes, so it is worth the belt and braces.
    """
    if over_under is None or spread is None:
        return None, None

    half_total = over_under / 2.0
    edge = abs(spread) / 2.0

    if home_favorite is None:
        home_favorite = spread < 0

    if home_favorite:
        return half_total + edge, half_total - edge
    return half_total - edge, half_total + edge


def total_bonus(implied_total: Optional[float], max_bonus: float) -> float:
    """
    Symmetric point swing from a team's implied total, capped at max_bonus.

    A team expected to score 29.5 gets the full bonus; one expected to score
    15.5 gets the full penalty. Everything between scales linearly.
    """
    if implied_total is None:
        return 0.0
    ratio = (implied_total - BASELINE_TOTAL) / TOTAL_SPAN
    return max(-1.0, min(1.0, ratio)) * max_bonus


def parse_odds(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Pull the fields we need out of ESPN's odds document.

    The document carries one item per sportsbook; we take the first, which the
    API orders by provider priority. Anything missing a total is useless here
    and returns None rather than a half-populated dict a caller might trust.
    """
    items = (payload or {}).get("items") or []
    if not items:
        return None
    item = items[0]

    over_under = item.get("overUnder")
    spread = item.get("spread")
    if over_under is None or spread is None:
        return None

    home_odds = item.get("homeTeamOdds") or {}
    away_odds = item.get("awayTeamOdds") or {}
    home_favorite = home_odds.get("favorite")
    if home_favorite is None and away_odds.get("favorite") is not None:
        home_favorite = not away_odds["favorite"]

    def point(side: Dict[str, Any], phase: str) -> Optional[float]:
        raw = ((side.get(phase) or {}).get("pointSpread") or {}).get("american")
        try:
            return float(str(raw).replace("+", ""))
        except (TypeError, ValueError):
            return None

    open_spread = point(home_odds, "open")
    current_spread = point(home_odds, "current")
    move = None
    if open_spread is not None and current_spread is not None:
        move = current_spread - open_spread

    return {
        "provider": ((item.get("provider") or {}).get("name")) or "unknown",
        "details": item.get("details"),
        "spread": float(spread),
        "over_under": float(over_under),
        "home_favorite": home_favorite,
        "open_spread": open_spread,
        "current_spread": current_spread,
        "spread_move": move,
    }


def describe_environment(implied_total: Optional[float], odds: Dict[str, Any], is_home: bool) -> str:
    """One-line read on what the market expects from this team's game."""
    if implied_total is None:
        return "no line posted"

    notes = []
    if implied_total >= 27:
        notes.append("high-scoring spot")
    elif implied_total <= 17:
        notes.append("low-scoring spot")

    spread = odds.get("spread")
    if spread is not None:
        favored = (spread < 0) if is_home else (spread > 0)
        margin = abs(spread)
        if margin >= 7:
            notes.append("big lead likely, run-heavy" if favored
                         else "trailing likely, pass-heavy")

    move = odds.get("spread_move")
    if move is not None and abs(move) >= NOTABLE_LINE_MOVE:
        toward = "toward" if ((move < 0) == is_home) else "away from"
        notes.append(f"line moved {abs(move):.1f} {toward} this team")

    return "; ".join(notes) if notes else "neutral game environment"
