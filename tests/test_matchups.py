"""
Unit tests for matchup finality: no winner until every starter on both sides
has finished playing.

Run: python3 -m pytest tests/test_matchups.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper import gamestatus  # noqa: E402
from sleeper.league import (  # noqa: E402
    _game_state_report,
    _rank_teams,
    build_week_matchups,
    fully_final_weeks,
    real_results_from_matchups,
    summarize_real_records,
)

PLAYERS = {
    "p_buf": {"full_name": "Buf Guy", "position": "QB", "team": "BUF"},
    "p_det": {"full_name": "Det Guy", "position": "WR", "team": "DET"},
    "p_nyg": {"full_name": "Monday Guy", "position": "RB", "team": "NYG"},
    "p_was": {"full_name": "Was Guy", "position": "TE", "team": "WAS"},
    "p_fa": {"full_name": "Cut Guy", "position": "WR", "team": None},
}

# Thursday game final, Sunday game live, Monday game not started, WAS on bye.
STATES = {"BUF": "post", "DET": "post", "SEA": "in", "ARI": "in", "NYG": "pre", "LAR": "pre"}


def entry(rid, mid, starters, points):
    return {
        "roster_id": rid, "matchup_id": mid, "starters": starters,
        "starters_points": [points / max(len(starters), 1)] * len(starters),
        "points": points,
    }


def one(entries, states=STATES, closed=False):
    return build_week_matchups(entries, PLAYERS, states, week_is_closed=closed)


# ── starter_state ────────────────────────────────────────────────────────────


def test_starter_states():
    st = gamestatus.starter_state
    assert st("p_buf", PLAYERS, STATES) == "post"
    assert st("p_nyg", PLAYERS, STATES) == "pre"
    assert st("SEA", PLAYERS, STATES) == "in"      # team defense keyed by abbr
    assert st("p_was", PLAYERS, STATES) == "bye"   # team not on the slate
    assert st("p_fa", PLAYERS, STATES) == "bye"    # free agent
    assert st("0", PLAYERS, STATES) == "bye"       # empty slot
    assert st("p_buf", PLAYERS, None) == "pre"     # no scoreboard: fail closed


def _sleeper_game(away, home, status, **meta):
    return {"status": status, "metadata": {"away_team": away, "home_team": home, **meta}}


def _full_slate(extra=()):
    """Twelve filler games, the fewest a real week ever has, plus any extras."""
    filler = [
        _sleeper_game(f"A{i}", f"H{i}", "pre_game") for i in range(12 - len(extra))
    ]
    return list(extra) + filler


def test_sleeper_scores_read_every_game_state():
    games = _full_slate([
        _sleeper_game("DET", "BUF", "complete", is_over=True),
        _sleeper_game("SEA", "ARI", "in_game", has_started=True, is_in_progress=True),
        _sleeper_game("NYG", "LAR", "pre_game"),
    ])
    states = gamestatus.parse_sleeper_scores(games)
    assert states["DET"] == states["BUF"] == "post"
    assert states["SEA"] == states["ARI"] == "in"
    assert states["NYG"] == states["LAR"] == "pre"


def test_cancelled_game_is_finished_but_postponed_is_not():
    states = gamestatus.parse_sleeper_scores([
        _sleeper_game("CIN", "HOU", "canceled", canceled=True),
        _sleeper_game("GB", "NYJ", "postponed", has_started=False),
    ])
    assert states["CIN"] == "post"   # nobody will score again
    assert states["GB"] == "pre"     # points may still be coming


def test_short_slate_is_rejected_rather_than_read_as_byes():
    # A truncated response would otherwise retire every missing team to a bye
    # and finalize matchups early, which is the failure this guards.
    assert gamestatus._usable(gamestatus.parse_sleeper_scores(_full_slate())) is True
    two_games = [_sleeper_game("DET", "BUF", "complete", is_over=True)] * 2
    assert gamestatus._usable(gamestatus.parse_sleeper_scores(two_games)) is False


def test_espn_abbreviations_normalize_to_sleeper():
    data = {"events": [{
        "status": {"type": {"state": "post", "completed": True}},
        "competitions": [{"competitors": [
            {"team": {"abbreviation": "WSH"}}, {"team": {"abbreviation": "DAL"}},
        ]}],
    }]}
    assert gamestatus.parse_scoreboard(data) == {"WAS": "post", "DAL": "post"}


# ── finality ─────────────────────────────────────────────────────────────────


def test_big_lead_is_not_a_win_while_the_loser_still_has_a_player():
    ms = one([entry(1, 1, ["p_buf", "p_det"], 80.0), entry(2, 1, ["p_buf", "p_nyg"], 10.0)])
    assert ms[0]["status"] == "live"
    assert ms[0]["winner"] is None
    assert real_results_from_matchups({2: ms}) == {}


def test_leader_still_playing_is_not_a_win_either():
    ms = one([entry(1, 1, ["p_buf", "SEA"], 80.0), entry(2, 1, ["p_buf", "p_det"], 10.0)])
    assert ms[0]["status"] == "live" and ms[0]["winner"] is None
    assert ms[0]["sides"][0]["playing"] == 1
    assert ms[0]["sides"][1]["complete"] is True


def test_both_sides_done_declares_winner_before_the_week_closes():
    ms = one([entry(1, 1, ["p_buf", "p_was", "0"], 40.0), entry(2, 1, ["p_det", "p_fa"], 55.5)])
    assert ms[0]["status"] == "final"
    assert ms[0]["winner"] == 2
    records = summarize_real_records(real_results_from_matchups({2: ms}))
    assert records[2]["wins"] == 1 and records[2]["losses"] == 0
    assert records[1]["wins"] == 0 and records[1]["losses"] == 1
    # Points ride along with the result, for the standings tiebreak.
    assert records[2]["points_for"] == 55.5
    assert records[1]["points_against"] == 55.5


def test_tie_when_final_and_level():
    ms = one([entry(1, 1, ["p_buf"], 50.0), entry(2, 1, ["p_det"], 50.0)])
    assert ms[0]["status"] == "final" and ms[0]["tie"] and ms[0]["winner"] is None
    records = summarize_real_records(real_results_from_matchups({2: ms}))
    assert records[1]["ties"] == 1 and records[2]["ties"] == 1


def test_nothing_started_is_upcoming():
    ms = one([entry(1, 1, ["p_nyg"], 0.0), entry(2, 1, ["p_nyg", "p_was"], 0.0)])
    assert ms[0]["status"] == "upcoming"


def test_no_scoreboard_means_no_winner():
    ms = one([entry(1, 1, ["p_buf"], 99.0), entry(2, 1, ["p_det"], 1.0)], states=None)
    assert ms[0]["status"] != "final" and ms[0]["winner"] is None


def test_closed_week_is_final_without_game_state():
    ms = one([entry(1, 1, ["p_nyg"], 99.0), entry(2, 1, ["p_nyg"], 1.0)], states=None, closed=True)
    assert ms[0]["status"] == "final" and ms[0]["winner"] == 1


def test_all_play_waits_for_the_whole_league():
    done = one([entry(1, 1, ["p_buf"], 40.0), entry(2, 1, ["p_det"], 30.0)])
    live = one([entry(3, 2, ["p_buf"], 40.0), entry(4, 2, ["p_nyg"], 0.0)])
    closed = one([entry(1, 1, ["p_buf"], 1.0), entry(2, 1, ["p_buf"], 2.0)], closed=True)
    assert fully_final_weeks({1: closed, 2: done + live}) == [1]
    assert fully_final_weeks({1: closed, 2: done}) == [1, 2]
    # ...while the decided pairing in the open week already has a result.
    assert 2 in real_results_from_matchups({1: closed, 2: done + live})


def test_unpaired_entries_are_ignored():
    assert one([entry(1, None, ["p_buf"], 10.0), entry(2, 7, ["p_det"], 10.0)]) == []


# ── game state reporting ─────────────────────────────────────────────────────


def test_game_state_report_flags_a_failed_read():
    ok = _game_state_report({2: {"states": {"BUF": "post"}, "source": "sleeper", "error": None}}, 2)
    assert ok == {"ok": True, "source": "sleeper", "error": None, "teams": 1}

    down = _game_state_report({2: {"states": None, "source": None, "error": "boom"}}, 2)
    assert down["ok"] is False and down["error"] == "boom"

    # A closed week fetches no game state at all; that is not a failure.
    assert _game_state_report({}, 2)["ok"] is True


# ── ranking ──────────────────────────────────────────────────────────────────


def test_rank_follows_the_real_record_not_all_play():
    directory = {
        1: {"roster_id": 1, "team_name": "Lucky", "manager": "a"},
        2: {"roster_id": 2, "team_name": "Robbed", "manager": "b"},
    }
    # Robbed outscores Lucky every week but keeps drawing the top scorer.
    all_play = {
        1: {"all_play_wins": 2, "all_play_losses": 8, "all_play_ties": 0,
            "all_play_pct": 0.2, "avg_points": 90.0, "best_week": 95.0,
            "worst_week": 85.0, "weeks_played": 2, "points_for": 180.0},
        2: {"all_play_wins": 8, "all_play_losses": 2, "all_play_ties": 0,
            "all_play_pct": 0.8, "avg_points": 130.0, "best_week": 140.0,
            "worst_week": 120.0, "weeks_played": 2, "points_for": 260.0},
    }
    real = {
        1: {"wins": 2, "losses": 0, "ties": 0, "points_for": 180.0, "points_against": 100.0},
        2: {"wins": 0, "losses": 2, "ties": 0, "points_for": 260.0, "points_against": 300.0},
    }
    rows = _rank_teams(all_play, real, {1: 0.8, 2: -0.8}, directory)

    assert [r["team_name"] for r in rows] == ["Lucky", "Robbed"]
    assert [r["rank"] for r in rows] == [1, 2]
    # ...while all play still ranks them the other way round.
    assert rows[0]["all_play_rank"] == 2 and rows[1]["all_play_rank"] == 1


def test_points_break_a_tied_record():
    directory = {i: {"roster_id": i, "team_name": f"T{i}", "manager": "m"} for i in (1, 2)}
    ap = {i: {"all_play_pct": 0.5, "avg_points": 100.0, "all_play_wins": 5,
              "all_play_losses": 5, "all_play_ties": 0, "best_week": 0.0,
              "worst_week": 0.0, "weeks_played": 1} for i in (1, 2)}
    real = {
        1: {"wins": 1, "losses": 1, "ties": 0, "points_for": 210.0, "points_against": 0.0},
        2: {"wins": 1, "losses": 1, "ties": 0, "points_for": 240.0, "points_against": 0.0},
    }
    rows = _rank_teams(ap, real, {}, directory)
    assert [r["team_name"] for r in rows] == ["T2", "T1"]


def test_a_tie_counts_as_half_a_win():
    directory = {i: {"roster_id": i, "team_name": f"T{i}", "manager": "m"} for i in (1, 2)}
    ap = {i: {"all_play_pct": 0.5, "avg_points": 100.0, "all_play_wins": 0,
              "all_play_losses": 0, "all_play_ties": 0, "best_week": 0.0,
              "worst_week": 0.0, "weeks_played": 1} for i in (1, 2)}
    real = {
        1: {"wins": 1, "losses": 1, "ties": 0, "points_for": 500.0, "points_against": 0.0},
        2: {"wins": 1, "losses": 0, "ties": 1, "points_for": 100.0, "points_against": 0.0},
    }
    rows = _rank_teams(ap, real, {}, directory)
    assert [r["team_name"] for r in rows] == ["T2", "T1"]
    assert rows[0]["real_pct"] == 0.75 and rows[1]["real_pct"] == 0.5
