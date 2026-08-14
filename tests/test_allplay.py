"""
Unit tests for the all play model.

These are possible only because league.py returns data instead of markdown. The
original codebase had no equivalent: every function ended in a string join, so
there was nothing to assert against short of parsing tables back out.

Run: python3 -m pytest tests/test_allplay.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper.league import (  # noqa: E402
    compute_all_play,
    compute_head_to_head_grid,
    compute_luck,
    summarize_real_records,
)


# ─────────────────────────────────────────────────────────────────────────────
# compute_all_play
# ─────────────────────────────────────────────────────────────────────────────


def test_single_week_four_teams():
    scores = {1: {1: 100.0, 2: 90.0, 3: 80.0, 4: 70.0}}
    out = compute_all_play(scores)

    assert out[1]["all_play_wins"] == 3 and out[1]["all_play_losses"] == 0
    assert out[2]["all_play_wins"] == 2 and out[2]["all_play_losses"] == 1
    assert out[3]["all_play_wins"] == 1 and out[3]["all_play_losses"] == 2
    assert out[4]["all_play_wins"] == 0 and out[4]["all_play_losses"] == 3
    assert out[1]["all_play_pct"] == 1.0
    assert out[4]["all_play_pct"] == 0.0


def test_wins_and_losses_are_conserved():
    """Every all play win is somebody else's loss; the totals must balance."""
    scores = {
        1: {1: 120.5, 2: 98.2, 3: 141.0, 4: 77.4, 5: 110.0},
        2: {1: 88.0, 2: 132.7, 3: 95.5, 4: 101.2, 5: 119.9},
        3: {1: 145.1, 2: 90.0, 3: 88.8, 4: 133.3, 5: 99.0},
    }
    out = compute_all_play(scores)
    assert sum(r["all_play_wins"] for r in out.values()) == sum(
        r["all_play_losses"] for r in out.values()
    )


def test_ties_counted_as_half():
    scores = {1: {1: 100.0, 2: 100.0, 3: 50.0}}
    out = compute_all_play(scores)
    # Team 1 beats team 3, ties team 2 -> 1 win, 1 tie of 2 games
    assert out[1]["all_play_wins"] == 1
    assert out[1]["all_play_ties"] == 1
    assert out[1]["all_play_pct"] == 0.75  # (1 + 0.5) / 2


def test_points_and_week_extremes():
    scores = {1: {1: 100.0}, 2: {1: 150.0}, 3: {1: 80.0}}
    out = compute_all_play(scores)
    assert out[1]["points_for"] == 330.0
    assert out[1]["weeks_played"] == 3
    assert out[1]["best_week"] == 150.0
    assert out[1]["worst_week"] == 80.0
    assert out[1]["avg_points"] == 110.0


def test_empty_input():
    assert compute_all_play({}) == {}


def test_team_missing_from_a_week():
    """A roster absent from one week should not be penalized for it."""
    scores = {1: {1: 100.0, 2: 90.0}, 2: {1: 100.0}}
    out = compute_all_play(scores)
    assert out[1]["weeks_played"] == 2
    assert out[2]["weeks_played"] == 1
    assert out[2]["all_play_games"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# compute_head_to_head_grid
# ─────────────────────────────────────────────────────────────────────────────


def test_grid_is_antisymmetric():
    scores = {
        1: {1: 100.0, 2: 90.0, 3: 80.0},
        2: {1: 70.0, 2: 120.0, 3: 95.0},
    }
    grid = compute_head_to_head_grid(scores)
    for a in grid:
        for b in grid[a]:
            assert grid[a][b]["wins"] == grid[b][a]["losses"]
            assert grid[a][b]["margin"] == -grid[b][a]["margin"]


def test_grid_has_no_self_cells():
    grid = compute_head_to_head_grid({1: {1: 100.0, 2: 90.0}})
    assert 1 not in grid[1]
    assert 2 not in grid[2]


def test_grid_margins():
    scores = {1: {1: 100.0, 2: 90.0}, 2: {1: 80.0, 2: 100.0}}
    grid = compute_head_to_head_grid(scores)
    # Team 1: +10 in week 1, -20 in week 2 -> net -10 over 2 games
    assert grid[1][2]["margin"] == -10.0
    assert grid[1][2]["avg_margin"] == -5.0
    assert grid[1][2]["wins"] == 1
    assert grid[1][2]["losses"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# luck
# ─────────────────────────────────────────────────────────────────────────────


def test_luck_positive_when_schedule_is_kind():
    """A team that wins every real game while scoring mid pack is lucky."""
    scores = {1: {1: 100.0, 2: 90.0, 3: 80.0}, 2: {1: 100.0, 2: 90.0, 3: 80.0}}
    all_play = compute_all_play(scores)
    real = {2: {"wins": 2, "losses": 0, "ties": 0}}  # team 2 won both real games
    luck = compute_luck(all_play, real)
    # Team 2 all play pct is 0.5, real pct is 1.0 -> lucky by 0.5
    assert luck[2] == 0.5


def test_luck_negative_when_schedule_is_cruel():
    scores = {1: {1: 100.0, 2: 90.0, 3: 80.0}, 2: {1: 100.0, 2: 90.0, 3: 80.0}}
    all_play = compute_all_play(scores)
    real = {1: {"wins": 0, "losses": 2, "ties": 0}}  # best scorer lost both
    luck = compute_luck(all_play, real)
    assert luck[1] == -1.0  # all play 1.0, real 0.0


def test_luck_zero_when_record_matches_scoring():
    scores = {1: {1: 100.0, 2: 50.0}}
    all_play = compute_all_play(scores)
    real = {1: {"wins": 1, "losses": 0, "ties": 0}}
    assert compute_luck(all_play, real)[1] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# real records
# ─────────────────────────────────────────────────────────────────────────────


def test_summarize_real_records():
    weekly = {
        1: [
            {"roster_id": 1, "opponent_id": 2, "points": 100, "opponent_points": 90, "result": "W"},
            {"roster_id": 2, "opponent_id": 1, "points": 90, "opponent_points": 100, "result": "L"},
        ],
        2: [
            {"roster_id": 1, "opponent_id": 2, "points": 80, "opponent_points": 80, "result": "T"},
            {"roster_id": 2, "opponent_id": 1, "points": 80, "opponent_points": 80, "result": "T"},
        ],
    }
    recs = summarize_real_records(weekly)
    assert recs[1] == {"wins": 1, "losses": 0, "ties": 1}
    assert recs[2] == {"wins": 0, "losses": 1, "ties": 1}


# ─────────────────────────────────────────────────────────────────────────────
# regression: the 2025 season, known good numbers
# ─────────────────────────────────────────────────────────────────────────────


def test_known_2025_shape():
    """
    Twelve teams over fourteen weeks must produce 11 all play games per team per
    week: 12 * 11 * 14 / 2 = 924 wins and 924 losses league wide. This is the
    invariant the Phase 0 script confirmed against live data.
    """
    scores = {
        week: {rid: 100.0 + rid + week for rid in range(1, 13)}
        for week in range(1, 15)
    }
    out = compute_all_play(scores)
    assert len(out) == 12
    assert sum(r["all_play_wins"] for r in out.values()) == 924
    assert sum(r["all_play_losses"] for r in out.values()) == 924
    assert all(r["all_play_games"] == 154 for r in out.values())  # 11 * 14
