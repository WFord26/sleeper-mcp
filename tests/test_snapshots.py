"""
Unit tests for the daily snapshot store.

These run against a real SQLite file in a temp directory, not a mock, because
the thing most likely to break here is the SQL itself.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper import config, snapshots  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    return snapshots


PLAYERS_DAY1 = {
    "1": {"position": "RB", "team": "SF", "active": True, "depth_chart_order": 3,
          "depth_chart_position": "RB", "status": "Active", "injury_status": None,
          "practice_participation": None},
    "2": {"position": "WR", "team": "CIN", "active": True, "depth_chart_order": 1,
          "depth_chart_position": "LWR", "status": "Active", "injury_status": None,
          "practice_participation": "Full"},
    "3": {"position": "OL", "team": "GB", "active": True, "depth_chart_order": 1},
    "4": {"position": "TE", "team": "KC", "active": False, "depth_chart_order": 2},
}

PLAYERS_DAY2 = {
    "1": {**PLAYERS_DAY1["1"], "depth_chart_order": 2},          # promoted
    "2": {**PLAYERS_DAY1["2"], "injury_status": "Questionable",
          "practice_participation": "Did Not Participate"},       # two changes
    "3": PLAYERS_DAY1["3"],
    "4": PLAYERS_DAY1["4"],
}


def test_capture_tracks_only_relevant_players(store):
    written = store.capture(PLAYERS_DAY1, snapshot_date="2026-09-01")
    # OL is not a tracked position, and the inactive TE is skipped.
    assert written["players"] == 2


def test_capture_is_idempotent(store):
    store.capture(PLAYERS_DAY1, snapshot_date="2026-09-01")
    store.capture(PLAYERS_DAY1, snapshot_date="2026-09-01")
    assert store.snapshot_dates() == ["2026-09-01"]


def test_diff_players_reports_depth_and_injury_moves(store):
    store.capture(PLAYERS_DAY1, snapshot_date="2026-09-01")
    store.capture(PLAYERS_DAY2, snapshot_date="2026-09-02")

    changes = {c["player_id"]: c for c in store.diff_players("2026-09-01", "2026-09-02")}
    assert set(changes) == {"1", "2"}

    rb_fields = {c["field"]: (c["old"], c["new"]) for c in changes["1"]["changes"]}
    assert rb_fields["depth_chart_order"] == (3, 2)

    wr_fields = {c["field"]: (c["old"], c["new"]) for c in changes["2"]["changes"]}
    assert wr_fields["injury_status"] == (None, "Questionable")
    assert wr_fields["practice_participation"] == ("Full", "Did Not Participate")


def test_diff_players_ignores_players_missing_from_either_side(store):
    store.capture(PLAYERS_DAY1, snapshot_date="2026-09-01")
    day2 = dict(PLAYERS_DAY2)
    day2["99"] = {"position": "WR", "team": "NYJ", "active": True, "depth_chart_order": 4}
    store.capture(day2, snapshot_date="2026-09-02")

    changed = {c["player_id"] for c in store.diff_players("2026-09-01", "2026-09-02")}
    assert "99" not in changed


def test_diff_players_respects_position_filter(store):
    store.capture(PLAYERS_DAY1, snapshot_date="2026-09-01")
    store.capture(PLAYERS_DAY2, snapshot_date="2026-09-02")
    changed = {c["player_id"] for c in store.diff_players("2026-09-01", "2026-09-02", positions=["RB"])}
    assert changed == {"1"}


def test_previous_date_and_dates(store):
    store.capture(PLAYERS_DAY1, snapshot_date="2026-09-01")
    store.capture(PLAYERS_DAY2, snapshot_date="2026-09-03")
    assert store.snapshot_dates() == ["2026-09-01", "2026-09-03"]
    assert store.previous_date("2026-09-03") == "2026-09-01"
    assert store.previous_date("2026-09-01") is None


def test_trending_series_is_chronological(store):
    for day, count in [("2026-09-01", 10), ("2026-09-02", 400), ("2026-09-03", 90)]:
        store.capture(PLAYERS_DAY1, trending={"add": {"1": count}}, snapshot_date=day)
    series = store.trending_series("1", "add", days=5)
    assert [s["count"] for s in series] == [10, 400, 90]


def test_roster_changes_reports_adds_and_drops(store):
    rosters_day1 = [{"roster_id": 1, "owner_id": "u1", "players": ["1", "2"]}]
    rosters_day2 = [{"roster_id": 1, "owner_id": "u1", "players": ["2", "3"]}]
    store.capture(PLAYERS_DAY1, rosters=rosters_day1, league_id="L", snapshot_date="2026-09-01")
    store.capture(PLAYERS_DAY2, rosters=rosters_day2, league_id="L", snapshot_date="2026-09-02")

    moves = store.roster_changes("L", "2026-09-01", "2026-09-02")
    assert moves["added"] == ["3"]
    assert moves["dropped"] == ["1"]


def test_roster_changes_empty_when_a_side_is_missing(store):
    store.capture(PLAYERS_DAY1, snapshot_date="2026-09-01")
    assert store.roster_changes("L", "2026-09-01") == {"added": [], "dropped": []}
