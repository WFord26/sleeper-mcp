"""
Unit tests for the opportunity (xFP) model.

The fit is the part worth testing directly: if solve_ols is wrong, every number
the report prints is wrong in a way that still looks plausible.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper import opportunity  # noqa: E402


def test_solve_ols_recovers_known_coefficients():
    # y = 2 + 3*x1 - 1*x2, exactly. The fit should return it.
    rows, targets = [], []
    for x1 in range(6):
        for x2 in range(6):
            rows.append([x1, x2])
            targets.append(2.0 + 3.0 * x1 - 1.0 * x2)

    coefficients = opportunity.solve_ols(rows, targets)
    assert coefficients is not None
    intercept, b1, b2 = coefficients
    assert abs(intercept - 2.0) < 1e-4
    assert abs(b1 - 3.0) < 1e-4
    assert abs(b2 - -1.0) < 1e-4


def test_solve_ols_handles_noise_without_blowing_up():
    rows = [[x, x * 0.5] for x in range(50)]
    targets = [1.0 + 2.0 * x + (0.3 if x % 2 else -0.3) for x in range(50)]
    coefficients = opportunity.solve_ols(rows, targets)
    assert coefficients is not None
    # x1 and x2 are collinear here, so the split between them is arbitrary, but
    # their combined effect must still reproduce the slope.
    _, b1, b2 = coefficients
    assert abs((b1 + 0.5 * b2) - 2.0) < 0.05


def test_solve_ols_rejects_malformed_input():
    assert opportunity.solve_ols([], []) is None
    assert opportunity.solve_ols([[1.0]], [1.0, 2.0]) is None
    assert opportunity.solve_ols([[1.0], [1.0, 2.0]], [1.0, 2.0]) is None


def _synthetic_season(weeks=8):
    """
    Two WRs with identical opportunity. One converts at the league rate, one is
    handed a touchdown every week he should not have had.
    """
    players = {
        "unlucky": {"position": "WR", "team": "AAA", "full_name": "Unlucky Guy"},
        "lucky": {"position": "WR", "team": "BBB", "full_name": "Lucky Guy"},
    }
    # Filler so the position clears MIN_ROWS_PER_POSITION.
    for i in range(10):
        players[f"filler{i}"] = {"position": "WR", "team": "CCC", "full_name": f"F {i}"}

    blobs = {}
    for wk in range(1, weeks + 1):
        week = {
            "unlucky": {"rec_tgt": 10, "rec_rz_tgt": 2, "rec_air_yd": 90, "rec": 6, "rec_yd": 70},
            "lucky": {"rec_tgt": 10, "rec_rz_tgt": 2, "rec_air_yd": 90, "rec": 6, "rec_yd": 70, "rec_td": 2},
        }
        for i in range(10):
            week[f"filler{i}"] = {
                "rec_tgt": 4 + i, "rec_rz_tgt": i % 3, "rec_air_yd": 30 + 5 * i,
                "rec": 3 + i // 2, "rec_yd": 35 + 6 * i,
            }
        blobs[wk] = week
    return blobs, players


def _score(stat, position):
    return (
        stat.get("rec", 0) * 1.0
        + stat.get("rec_yd", 0) * 0.1
        + stat.get("rec_td", 0) * 6.0
    )


def test_build_report_separates_lucky_from_unlucky():
    blobs, players = _synthetic_season()
    rows, models = opportunity.build_report(
        blobs, players, _score, positions=("WR",), min_opportunity_per_game=1.0
    )

    assert "WR" in models
    by_pid = {r["pid"]: r for r in rows}
    assert "unlucky" in by_pid and "lucky" in by_pid

    # Identical opportunity means identical expectation.
    assert abs(by_pid["unlucky"]["expected_per_game"] - by_pid["lucky"]["expected_per_game"]) < 1e-6
    # The one scoring phantom touchdowns is above his expectation, the other below.
    assert by_pid["lucky"]["residual_per_game"] > 0
    assert by_pid["unlucky"]["residual_per_game"] < 0
    # Rows come back most-underperforming first, which is the buy list.
    assert rows[0]["residual_per_game"] <= rows[-1]["residual_per_game"]


def test_build_report_declines_on_thin_data():
    blobs = {1: {"a": {"rec_tgt": 5}}}
    players = {"a": {"position": "WR"}}
    rows, models = opportunity.build_report(blobs, players, _score, positions=("WR",))
    assert rows == [] and models == {}


def test_build_report_filters_low_volume_players():
    blobs, players = _synthetic_season()
    # A one-target-per-week player should not earn a residual verdict.
    for wk in blobs:
        blobs[wk]["scrub"] = {"rec_tgt": 1, "rec": 1, "rec_yd": 5}
    players["scrub"] = {"position": "WR", "team": "DDD", "full_name": "Scrub"}

    rows, _ = opportunity.build_report(
        blobs, players, _score, positions=("WR",), min_opportunity_per_game=3.0
    )
    assert "scrub" not in {r["pid"] for r in rows}
