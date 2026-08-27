"""
Unit tests for the breakout radar.

The two things worth pinning down are that shares are computed against the
team's own week (not raw counts), and that the obscurity gate actually demotes
players the field has already found.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper import breakout  # noqa: E402


def test_split_window_gives_the_extra_week_to_the_recent_half():
    assert breakout.split_window([1, 2, 3, 4]) == ([1, 2], [3, 4])
    assert breakout.split_window([1, 2, 3, 4, 5]) == ([1, 2], [3, 4, 5])
    assert breakout.split_window([5, 1, 3]) == ([1], [3, 5])       # sorts first
    assert breakout.split_window([1]) == ([], [1])


def test_team_totals_sum_by_team_and_week():
    blobs = {
        1: {"a": {"rec_tgt": 6}, "b": {"rec_tgt": 4}, "c": {"rec_tgt": 10}},
        2: {"a": {"rec_tgt": 8}, "b": {"rec_tgt": 2}},
    }
    teams = {"a": "AAA", "b": "AAA", "c": "BBB"}
    totals = breakout.team_totals(blobs, teams, ["rec_tgt"])
    assert totals[1]["AAA"]["rec_tgt"] == 10
    assert totals[1]["BBB"]["rec_tgt"] == 10
    assert totals[2]["AAA"]["rec_tgt"] == 10


def test_team_totals_ignore_players_with_no_team():
    blobs = {1: {"a": {"rec_tgt": 6}, "ghost": {"rec_tgt": 99}}}
    totals = breakout.team_totals(blobs, {"a": "AAA"}, ["rec_tgt"])
    assert totals[1]["AAA"]["rec_tgt"] == 6
    assert "ghost" not in str(totals)


def _wr_season(riser_targets, teammate_targets):
    """
    Four weeks, one team. The riser's target count is given per week; a single
    teammate absorbs the rest so team volume can be held constant or varied.
    """
    players = {
        "riser": {"position": "WR", "team": "AAA", "full_name": "Riser"},
        "mate": {"position": "WR", "team": "AAA", "full_name": "Teammate"},
    }
    blobs = {}
    for i, week in enumerate([1, 2, 3, 4]):
        blobs[week] = {
            "riser": {
                "rec_tgt": riser_targets[i], "off_snp": 40 + 5 * i, "tm_off_snp": 65,
                "rec_air_yd": riser_targets[i] * 10, "rec_rz_tgt": 1 + i,
            },
            "mate": {
                "rec_tgt": teammate_targets[i], "off_snp": 50, "tm_off_snp": 65,
                "rec_air_yd": teammate_targets[i] * 10, "rec_rz_tgt": 2,
            },
        }
    return blobs, players


def test_rising_target_share_produces_a_positive_trend():
    blobs, players = _wr_season([3, 4, 9, 11], [12, 11, 6, 4])
    rows = breakout.build_candidates(blobs, players, [1, 2, 3, 4], positions=("WR",))
    by_pid = {r["pid"]: r for r in rows}
    assert by_pid["riser"]["trend_score"] > 0
    assert by_pid["mate"]["trend_score"] < 0
    opportunity = by_pid["riser"]["components"]["opportunity_share"]
    assert opportunity["after"] > opportunity["before"]


def test_share_is_measured_against_team_volume_not_raw_counts():
    # The player's raw targets rise, but the whole offense rose with him — his
    # share is flat, so this must not register as a role change.
    blobs, players = _wr_season([5, 5, 10, 10], [5, 5, 10, 10])
    rows = breakout.build_candidates(blobs, players, [1, 2, 3, 4], positions=("WR",))
    riser = next(r for r in rows if r["pid"] == "riser")
    assert abs(riser["components"]["opportunity_share"]["delta"]) < 1e-6


def test_low_volume_players_are_excluded():
    blobs, players = _wr_season([0, 1, 1, 2], [20, 20, 20, 20])
    rows = breakout.build_candidates(
        blobs, players, [1, 2, 3, 4], positions=("WR",), min_recent_opportunity=3.0
    )
    assert "riser" not in {r["pid"] for r in rows}


def test_players_missing_from_a_half_are_skipped():
    blobs, players = _wr_season([5, 6, 7, 8], [8, 7, 6, 5])
    del blobs[1]["riser"]
    del blobs[2]["riser"]      # no earlier-half appearances at all
    rows = breakout.build_candidates(blobs, players, [1, 2, 3, 4], positions=("WR",))
    assert "riser" not in {r["pid"] for r in rows}


def test_missing_component_renormalizes_instead_of_penalizing():
    components = {
        "snap_share": {"normalized": 1.0},
        "opportunity_share": {"normalized": 1.0},
        "air_share": {"normalized": None},
        "rz_share": {"normalized": 1.0},
    }
    weights = breakout.POSITION_PROFILES["WR"]["weights"]
    # Three components all maxed should still score 100, not 80.
    assert abs(breakout.trend_score(components, weights) - 100.0) < 1e-9


def test_trend_score_is_zero_without_usable_components():
    assert breakout.trend_score({}, {"snap_share": 1.0}) == 0.0
    assert breakout.trend_score({"snap_share": {"normalized": None}}, {"snap_share": 1.0}) == 0.0


def test_gate_demotes_players_the_field_already_found():
    quiet = breakout.apply_gate(50.0, attention_norm=0.0)
    hyped = breakout.apply_gate(50.0, attention_norm=1.0)
    assert quiet["breakout_score"] == 50.0
    assert hyped["breakout_score"] < quiet["breakout_score"]
    assert abs(hyped["breakout_score"] - 50.0 * (1 - breakout.ATTENTION_DISCOUNT)) < 1e-9


def test_depth_chart_bonus_is_capped_and_gated_with_everything_else():
    one_slot = breakout.apply_gate(0.0, depth_slots_gained=1)
    many = breakout.apply_gate(0.0, depth_slots_gained=9)
    assert one_slot["depth_bonus"] == breakout.DEPTH_MOVE_BONUS
    assert many["depth_bonus"] == breakout.DEPTH_MOVE_CAP
    # A promotion everyone reacted to is discounted like any other signal.
    noticed = breakout.apply_gate(0.0, depth_slots_gained=2, attention_norm=1.0)
    assert noticed["breakout_score"] < many["breakout_score"]
    # Sliding down the depth chart is never a bonus.
    assert breakout.apply_gate(10.0, depth_slots_gained=-2)["depth_bonus"] == 0.0


def test_tiny_team_denominators_are_ignored():
    """
    Air yards is the one field that can be negative — targets behind the line of
    scrimmage subtract from the team total. An offense with almost no net air
    yards would otherwise hand a receiver a share in the hundreds of percent and
    let one weird afternoon dominate his average.
    """
    players = {
        "wr": {"position": "WR", "team": "AAA", "full_name": "Deep Threat"},
        "mate": {"position": "WR", "team": "AAA", "full_name": "Screen Guy"},
    }
    blobs = {}
    for week in [1, 2, 3, 4]:
        blobs[week] = {
            "wr": {"rec_tgt": 8, "off_snp": 55, "tm_off_snp": 65,
                   "rec_air_yd": 150, "rec_rz_tgt": 2},
            "mate": {"rec_tgt": 6, "off_snp": 50, "tm_off_snp": 65,
                     "rec_air_yd": -20, "rec_rz_tgt": 2},
        }
    # Week 3: the whole offense played behind the line. Team air yards nets to 6,
    # which would put this receiver's "share" at 167%.
    blobs[3]["wr"]["rec_air_yd"] = 10
    blobs[3]["mate"]["rec_air_yd"] = -4

    rows = breakout.build_candidates(blobs, players, [1, 2, 3, 4], positions=("WR",))
    wr = next(r for r in rows if r["pid"] == "wr")
    air = wr["components"]["air_share"]

    # Week 4 alone: 150 of the team's 130 net air yards. Over 100% is legitimate
    # here — teammates ran negative — and the delta cap handles the extremes.
    week4_share = 150 / 130 * 100
    assert abs(air["after"] - week4_share) < 1e-6

    # The point of the guard: had week 3 been kept, its 167% share would have
    # pulled the recent average up to ~141 instead.
    week3_share = 10 / 6 * 100
    assert air["after"] < (week3_share + week4_share) / 2


def test_component_caps_are_documented_with_a_denominator_floor():
    for name, spec in breakout.COMPONENT_SPECS.items():
        assert spec["cap"] > 0, name
        assert spec.get("min_denominator", 0) >= 0, name
