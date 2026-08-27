"""
Unit tests for implied totals and the odds parser.

The sign convention is the thing worth guarding: getting home and away backwards
would invert every recommendation while still producing plausible numbers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper import vegas  # noqa: E402


def test_implied_totals_split_the_game_total():
    home, away = vegas.implied_totals(-3.5, 44.5, home_favorite=True)
    assert abs(home - 24.0) < 1e-9
    assert abs(away - 20.5) < 1e-9
    assert abs((home + away) - 44.5) < 1e-9


def test_favorite_always_gets_the_larger_share():
    home, away = vegas.implied_totals(2.5, 39.5, home_favorite=False)
    assert away > home
    assert abs((home + away) - 39.5) < 1e-9


def test_falls_back_to_spread_sign_without_a_favorite_flag():
    home, away = vegas.implied_totals(-6.0, 48.0)
    assert home > away  # negative spread means the home team is laying points


def test_missing_inputs_return_nothing():
    assert vegas.implied_totals(None, 44.5) == (None, None)
    assert vegas.implied_totals(-3.0, None) == (None, None)


def test_total_bonus_is_symmetric_and_capped():
    assert vegas.total_bonus(vegas.BASELINE_TOTAL, 2.5) == 0.0
    high = vegas.total_bonus(vegas.BASELINE_TOTAL + vegas.TOTAL_SPAN, 2.5)
    low = vegas.total_bonus(vegas.BASELINE_TOTAL - vegas.TOTAL_SPAN, 2.5)
    assert abs(high - 2.5) < 1e-9
    assert abs(low + 2.5) < 1e-9
    # Beyond the span the bonus stops growing.
    assert vegas.total_bonus(99.0, 2.5) == high
    assert vegas.total_bonus(0.0, 2.5) == low
    assert vegas.total_bonus(None, 2.5) == 0.0


ODDS_DOC = {
    "items": [{
        "provider": {"name": "DraftKings"},
        "details": "SEA -3.5",
        "spread": -3.5,
        "overUnder": 44.5,
        "homeTeamOdds": {
            "favorite": True,
            "open": {"pointSpread": {"american": "-1.5"}},
            "current": {"pointSpread": {"american": "-3.5"}},
        },
        "awayTeamOdds": {"favorite": False},
    }]
}


def test_parse_odds_extracts_line_and_movement():
    parsed = vegas.parse_odds(ODDS_DOC)
    assert parsed["provider"] == "DraftKings"
    assert parsed["over_under"] == 44.5
    assert parsed["home_favorite"] is True
    assert parsed["spread_move"] == -2.0  # home moved from -1.5 to -3.5


def test_parse_odds_infers_home_favorite_from_the_away_flag():
    doc = {"items": [{
        "spread": 2.5, "overUnder": 39.5,
        "homeTeamOdds": {}, "awayTeamOdds": {"favorite": True},
    }]}
    assert vegas.parse_odds(doc)["home_favorite"] is False


def test_parse_odds_rejects_incomplete_documents():
    assert vegas.parse_odds({}) is None
    assert vegas.parse_odds({"items": []}) is None
    assert vegas.parse_odds({"items": [{"spread": -3.0}]}) is None


def test_describe_environment_reads_game_script():
    odds = {"spread": -9.5, "spread_move": 0.0}
    assert "run-heavy" in vegas.describe_environment(28.0, odds, is_home=True)
    assert "pass-heavy" in vegas.describe_environment(18.5, odds, is_home=False)
    assert vegas.describe_environment(None, odds, is_home=True) == "no line posted"


def test_describe_environment_flags_line_movement():
    odds = {"spread": -2.0, "spread_move": -3.0}
    assert "line moved 3.0 toward" in vegas.describe_environment(23.0, odds, is_home=True)
    assert "line moved 3.0 away from" in vegas.describe_environment(23.0, odds, is_home=False)
