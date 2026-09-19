"""
Unit tests for the draft board model.

The snake maths is the part worth pinning down: an off-by-one in slot_for_pick
puts every "on the clock" call on the wrong manager, and it looks plausible.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper import draft  # noqa: E402


def test_slot_for_pick_snake_serpentine():
    teams = 12
    # Round 1 runs 1..12, round 2 runs 12..1, round 3 runs 1..12.
    assert draft.slot_for_pick(1, teams, snake=True) == (1, 1)
    assert draft.slot_for_pick(12, teams, snake=True) == (12, 1)
    assert draft.slot_for_pick(13, teams, snake=True) == (12, 2)
    assert draft.slot_for_pick(24, teams, snake=True) == (1, 2)
    assert draft.slot_for_pick(25, teams, snake=True) == (1, 3)


def test_slot_for_pick_linear_never_reverses():
    teams = 10
    assert draft.slot_for_pick(1, teams, snake=False) == (1, 1)
    assert draft.slot_for_pick(11, teams, snake=False) == (1, 2)
    assert draft.slot_for_pick(20, teams, snake=False) == (10, 2)


def test_pick_no_round_trips_with_slot_for_pick():
    teams = 12
    for pick_no in range(1, teams * 16 + 1):
        slot, rnd = draft.slot_for_pick(pick_no, teams, snake=True)
        assert draft.pick_no_for(rnd, slot, teams, snake=True) == pick_no


def test_reversal_round_flips_parity_from_that_round():
    teams = 12
    # Without reversal, round 3 runs forward (1..12).
    assert draft.round_is_forward(3, snake=True, reversal_round=0) is True
    # 3rd round reversal makes round 3 run backward, matching round 2.
    assert draft.round_is_forward(3, snake=True, reversal_round=3) is False
    assert draft.round_is_forward(2, snake=True, reversal_round=3) is False


def _draft(status="drafting", teams=3, rounds=2, dtype="snake"):
    return {
        "draft_id": "d1",
        "type": dtype,
        "status": status,
        "settings": {"teams": teams, "rounds": rounds, "pick_timer": 90},
        "draft_order": {"u1": 1, "u2": 2, "u3": 3},
        "slot_to_roster_id": {"1": 10, "2": 20, "3": 30},
    }


def _slots():
    return [
        {"slot": 1, "roster_id": 10, "team_name": "Alpha", "manager": "a"},
        {"slot": 2, "roster_id": 20, "team_name": "Bravo", "manager": "b"},
        {"slot": 3, "roster_id": 30, "team_name": "Charlie", "manager": "c"},
    ]


def test_build_board_places_picks_and_finds_the_clock():
    picks = [
        {"pick_no": 1, "round": 1, "draft_slot": 1, "roster_id": 10,
         "player_id": "p1", "metadata": {"first_name": "Bijan", "last_name": "Robinson",
                                         "position": "RB", "team": "ATL"}},
        {"pick_no": 2, "round": 1, "draft_slot": 2, "roster_id": 20,
         "player_id": "p2", "metadata": {"first_name": "Ja'Marr", "last_name": "Chase",
                                         "position": "WR", "team": "CIN"}},
    ]
    out = draft.build_board(_draft(), picks, players={}, slots=_slots())

    assert out["picks_made"] == 2
    assert out["total_picks"] == 6

    r1 = out["board"][0]["picks"]
    assert r1[0]["player"] == "Bijan Robinson" and r1[0]["pos"] == "RB"
    assert r1[1]["player"] == "Ja'Marr Chase"
    assert r1[2]["player"] is None

    # Pick 3 is the last of round 1: slot 3, and it is on the clock.
    oc = out["on_clock"]
    assert oc["pick_no"] == 3 and oc["round"] == 1 and oc["slot"] == 3
    assert oc["team_name"] == "Charlie"
    assert r1[2]["on_clock"] is True
    # Round 2 reverses, so its first cell (slot 1) is the last pick overall.
    assert out["board"][1]["forward"] is False


def test_build_board_completed_draft_has_no_clock():
    out = draft.build_board(_draft(status="complete"), picks=[], players={}, slots=_slots())
    assert out["on_clock"] is None


def test_best_available_ranks_by_adp_and_tags_position():
    players = {
        "a": {"first_name": "Top", "last_name": "Back", "position": "RB", "team": "AAA", "active": True},
        "b": {"first_name": "Top", "last_name": "Wideout", "position": "WR", "team": "BBB", "active": True},
        "c": {"first_name": "Second", "last_name": "Back", "position": "RB", "team": "CCC", "active": True},
        "d": {"first_name": "Already", "last_name": "Gone", "position": "RB", "team": "DDD", "active": True},
        "e": {"first_name": "No", "last_name": "Team", "position": "WR", "team": "", "active": True},
    }
    adp = {
        "a": {"adp_dd_ppr": 3.0, "pts_ppr": 18.0},
        "b": {"adp_dd_ppr": 5.0},
        "c": {"adp_dd_ppr": 14.0},
        "d": {"adp_dd_ppr": 1.0},
    }
    rows = draft._best_available(players, adp, drafted_ids={"d"}, limit=10)

    assert [r["player_id"] for r in rows] == ["a", "b", "c"]
    assert rows[0]["pos_rank"] == "RB1"
    assert rows[1]["pos_rank"] == "WR1"
    assert rows[2]["pos_rank"] == "RB2"
    assert rows[0]["proj"] == 18.0
