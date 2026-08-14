"""
Fantasy scoring calculator.

Moved verbatim from sleeper_fantasy_mcp.py per ADR 001 phase 1, step 3. These
functions were already pure, so they transfer without modification. This is now
the only implementation of scoring in the project: both the MCP tools and the
dashboard call it, so the two surfaces cannot disagree about a player's points.

League: 12 team Full PPR with milestone bonuses.
"""

from typing import Any, Dict

SCORING_RULES: Dict[str, float] = {
    # Passing
    "pass_yd": 0.04,
    "pass_td": 6.0,
    "pass_int": -2.0,
    "pass_2pt": 2.0,
    "pass_cmp_40p": 1.0,   # per 40+ yd completion
    "pass_td_40p": 1.0,    # per 40+ yd pass TD bonus
    "pass_td_50p": 1.0,    # per 50+ yd pass TD (stacks with 40+ bonus)
    # Rushing
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rush_2pt": 2.0,
    "rush_att": 0.2,
    "rush_40p": 1.0,       # per 40+ yd rush
    "rush_td_40p": 1.0,    # per 40+ yd rush TD bonus
    "rush_td_50p": 1.0,    # per 50+ yd rush TD (stacks with 40+ bonus)
    # Receiving (Full PPR)
    "rec": 1.0,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "rec_2pt": 2.0,
    "rec_40p": 1.0,        # per 40+ yd reception
    "rec_td_40p": 1.0,     # per 40+ yd rec TD bonus
    "rec_td_50p": 1.0,     # per 50+ yd rec TD (stacks with 40+ bonus)
    # Miscellaneous
    "fum_lost": -2.0,
    "fum_rec_td": 6.0,
    # Kicking (FG ranges handled in FG_SCORING below)
    "xpm": 1.0,
    "xpmiss": -1.0,
    "fgmiss": -1.0,
    # Milestone game bonuses — Sleeper may return these directly
    "rush_yd_100_199": 1.0,    # +1 for 100-199 rush game
    "rush_yd_200p": 1.0,       # +1 more for 200+ rush game (cumulative: 2 total)
    "rec_yd_100_199": 1.0,     # +1 for 100-199 rec game
    "rec_yd_200p": 1.0,        # +1 more for 200+ rec game
    "pass_yd_300_399": 1.0,    # +1 for 300-399 pass game
    "pass_yd_400p": 2.0,       # +2 for 400+ pass game (cumulative: 3 total with 300 bonus)
    "rush_rec_yd_200p": 1.0,   # +1 for 200+ combined rush+rec
    "pass_cmp_25p": 1.0,       # +1 for 25+ completions
}

FG_SCORING: Dict[str, float] = {
    "fgm_0_19": 3.0,
    "fgm_20_29": 3.0,
    "fgm_30_39": 3.0,
    "fgm_40_49": 4.0,
    "fgm_50p": 5.0,
}

DEF_SCORING: Dict[str, float] = {
    "def_td": 6.0,
    "sack": 1.0,
    "def_int": 2.0,
    "def_fum_rec": 2.0,
    "safe": 2.0,
    "def_ff": 1.0,
    "blk_kick": 2.0,
    "def_4dstop": 1.0,
}

# Points-allowed tiers: (min, max, fantasy pts)
DEF_PA_TIERS = [
    (0,   0,   7.0),
    (1,   6,   6.0),
    (7,   13,  5.0),
    (14,  20,  2.0),
    (21,  27,  0.0),
    (28,  34, -1.0),
    (35, 9999, -4.0),
]

# Yards-allowed tiers: (min, max, fantasy pts)
DEF_YA_TIERS = [
    (0,    99,   5.0),
    (100,  199,  3.0),
    (200,  299,  2.0),
    (300,  349,  1.0),
    (350,  399,  0.0),
    (400,  449, -1.0),
    (450,  499, -3.0),
    (500,  549, -5.0),
    (550, 9999, -6.0),
]

# ─────────────────────────────────────────────────────────────────────────────
# Scoring calculator
# ─────────────────────────────────────────────────────────────────────────────

def calculate_fantasy_points(stats: Dict[str, Any], position: str) -> float:
    """
    Calculate fantasy points for a player given a stats or projections dict.

    Works for both season aggregate stats and single-week projections.
    For projections, milestone bonus fields (e.g. rush_yd_100_199) may be
    fractional expected-value numbers — the multiplier still applies correctly.
    """
    if not stats:
        return 0.0

    if position == "DEF":
        return _calculate_def_points(stats)

    pts = 0.0

    # Direct multiplier fields
    for field, mult in SCORING_RULES.items():
        pts += stats.get(field, 0.0) * mult

    # FG range scoring
    for field, value in FG_SCORING.items():
        pts += stats.get(field, 0.0) * value

    # If Sleeper didn't return the milestone bonus fields, derive them from raw totals.
    # This is the correct approach for single-week projections.
    has_bonus_fields = any(
        field in stats for field in ("rush_yd_100_199", "rec_yd_100_199", "pass_yd_300_399")
    )
    if not has_bonus_fields:
        pts += _derive_milestone_bonuses(stats)

    return round(pts, 2)


def _calculate_def_points(stats: Dict[str, Any]) -> float:
    pts = 0.0

    for field, value in DEF_SCORING.items():
        pts += stats.get(field, 0.0) * value

    # Points allowed — prefer the aggregate field; fall back to bucket flags
    pa = stats.get("pts_allow")
    if pa is not None:
        for lo, hi, bonus in DEF_PA_TIERS:
            if lo <= pa <= hi:
                pts += bonus
                break
    else:
        bucket_map = {
            "pts_allow_0":    7.0,
            "pts_allow_1_6":  6.0,
            "pts_allow_7_13": 5.0,
            "pts_allow_14_20": 2.0,
            "pts_allow_28_34": -1.0,
            "pts_allow_35p":  -4.0,
        }
        for field, bonus in bucket_map.items():
            if stats.get(field, 0):
                pts += bonus
                break

    # Yards allowed
    ya = stats.get("yds_allow")
    if ya is not None:
        for lo, hi, bonus in DEF_YA_TIERS:
            if lo <= ya <= hi:
                pts += bonus
                break

    return round(pts, 2)


def _derive_milestone_bonuses(stats: Dict[str, Any]) -> float:
    """
    Compute milestone game bonuses from raw totals when Sleeper's pre-computed
    bonus fields are absent (common for weekly projections).
    """
    pts = 0.0
    rush_yd = stats.get("rush_yd", 0.0)
    rec_yd = stats.get("rec_yd", 0.0)
    pass_yd = stats.get("pass_yd", 0.0)
    pass_cmp = stats.get("pass_cmp", 0.0)

    if rush_yd >= 100:
        pts += 1.0   # 100-199 rush bonus
    if rush_yd >= 200:
        pts += 1.0   # 200+ rush bonus (cumulative)
    if rec_yd >= 100:
        pts += 1.0   # 100-199 rec bonus
    if rec_yd >= 200:
        pts += 1.0   # 200+ rec bonus (cumulative)
    if pass_yd >= 300:
        pts += 1.0   # 300-399 pass bonus
    if pass_yd >= 400:
        pts += 2.0   # 400+ pass bonus (cumulative; extra 2, total 3 from 300)
    if (rush_yd + rec_yd) >= 200:
        pts += 1.0   # 200+ combined rush+rec
    if pass_cmp >= 25:
        pts += 1.0   # 25+ completions

    return pts