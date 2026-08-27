"""
Unit tests for the FAAB market model.

Built around the distinction the tool depends on: a completed waiver claim is a
price paid, a failed one is a price that was not enough.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper import market  # noqa: E402


def waiver(bid, status="complete", adds=None, drops=None, rosters=(1,), created=0, leg=1):
    return {
        "type": "waiver",
        "status": status,
        "settings": {"waiver_bid": bid},
        "adds": adds or {},
        "drops": drops,
        "roster_ids": list(rosters),
        "created": created,
        "leg": leg,
    }


TX = [
    waiver(14, adds={"a": 1}, rosters=(1,), created=100),
    waiver(9, status="failed", adds={"a": 2}, rosters=(2,), created=100),
    waiver(31, status="failed", adds={"a": 3}, rosters=(3,), created=100),
    waiver(4, adds={"b": 1}, rosters=(1,), created=200),
    waiver(0, adds={"c": 4}, drops={"z": 4}, rosters=(4,), created=300, leg=2),
    {"type": "free_agent", "status": "complete", "adds": {"d": 5},
     "drops": {"y": 5}, "roster_ids": [5], "created": 400, "leg": 2},
]


def test_bid_distribution_separates_won_from_lost():
    dist = market.bid_distribution(TX)
    assert sorted(dist["won"]) == [0, 4, 14]
    assert sorted(dist["lost"]) == [9, 31]
    assert dist["claims"] == 5
    assert dist["max_won"] == 14
    assert dist["max_lost"] == 31       # the number that says what contested costs
    assert dist["free_claims"] == 1


def test_bid_distribution_ignores_non_waiver_moves():
    dist = market.bid_distribution([TX[-1]])
    assert dist["claims"] == 0 and dist["won"] == []


def test_percentiles_are_nearest_rank_not_interpolated():
    assert market.percentiles([1, 2, 3, 4], (0.5,))["p50"] == 2
    assert market.percentiles([], (0.5,)) == {}


def test_player_bid_history_counts_contenders():
    history = market.player_bid_history(TX)
    assert history["a"]["winning_bid"] == 14
    assert history["a"]["losing_bids"] == [31, 9]   # sorted high to low
    assert history["a"]["contenders"] == 3
    assert history["b"]["contenders"] == 1


def test_budget_state_computes_remaining():
    rosters = [
        {"roster_id": 1, "owner_id": "u1", "settings": {"waiver_budget_used": 89}},
        {"roster_id": 2, "owner_id": "u2", "settings": {}},
        {"roster_id": None, "settings": {"waiver_budget_used": 5}},
    ]
    state = market.budget_state(rosters, 100)
    assert state[1]["remaining"] == 11
    assert state[2]["remaining"] == 100
    assert None not in state


def test_budget_state_never_reports_negative():
    state = market.budget_state([{"roster_id": 1, "settings": {"waiver_budget_used": 150}}], 100)
    assert state[1]["remaining"] == 0


def test_recent_drops_are_newest_first_and_complete_only():
    drops = market.recent_drops(TX)
    assert [d["player_id"] for d in drops] == ["y", "z"]
    # A failed claim never dropped anyone.
    assert not market.recent_drops([waiver(5, status="failed", drops={"q": 1})])


def test_recent_drops_respects_the_time_window():
    assert [d["player_id"] for d in market.recent_drops(TX, since_ms=350)] == ["y"]


def test_still_dropped_excludes_rostered_and_duplicates():
    drops = [
        {"player_id": "y", "created": 3}, {"player_id": "z", "created": 2},
        {"player_id": "y", "created": 1},
    ]
    out = market.still_dropped(drops, rostered=["z"])
    assert [d["player_id"] for d in out] == ["y"]


def test_suggest_bid_anchors_to_league_history():
    dist = market.bid_distribution(TX)
    suggestion = market.suggest_bid(dist, total_budget=100, remaining=100, aggression=0.75)
    assert suggestion["suggested"] >= 1
    assert suggestion["max_losing_bid"] == 31


def test_suggest_bid_is_capped_by_what_you_have_left():
    dist = market.bid_distribution(TX)
    suggestion = market.suggest_bid(dist, total_budget=100, remaining=2)
    assert suggestion["suggested"] <= 2


def test_suggest_bid_declines_without_history():
    suggestion = market.suggest_bid({"won": []}, total_budget=100, remaining=50)
    assert suggestion["suggested"] is None
    assert "no completed waiver claims" in suggestion["reason"]
