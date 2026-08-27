"""
Daily snapshots, so deltas are possible at all.

Sleeper's players, trending, and depth chart endpoints are all "now" only. There
is no history in the API, and the signals worth having are almost all changes:
a back moving from third on the depth chart to second, a starter's practice
participation dropping to DNP, add volume accelerating, a productive player
getting dropped across the league. None of that is reconstructable after the
fact. If today's state is not written down today, it is gone.

So this module keeps a small SQLite file of one row per player per day, plus the
trending counts and league rosters. It is deliberately narrow — only the fields
that both change and matter — which keeps a full season under a few megabytes
and every query fast.

Writes are best effort. A snapshot failure must never break a read only tool,
so callers wrap capture in a try and move on.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import config

# Skill positions plus kickers and defenses; everyone else is roster filler that
# would triple the row count and never be queried.
TRACKED_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

SCHEMA = """
CREATE TABLE IF NOT EXISTS player_snapshot (
    snapshot_date        TEXT    NOT NULL,
    player_id            TEXT    NOT NULL,
    team                 TEXT,
    position             TEXT,
    depth_chart_position TEXT,
    depth_chart_order    INTEGER,
    status               TEXT,
    injury_status        TEXT,
    injury_body_part     TEXT,
    practice_participation TEXT,
    news_updated         INTEGER,
    PRIMARY KEY (snapshot_date, player_id)
);

CREATE TABLE IF NOT EXISTS trending_snapshot (
    snapshot_date TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    player_id     TEXT    NOT NULL,
    count         INTEGER NOT NULL,
    captured_at   INTEGER NOT NULL,
    PRIMARY KEY (snapshot_date, kind, player_id)
);

CREATE TABLE IF NOT EXISTS roster_snapshot (
    snapshot_date TEXT NOT NULL,
    league_id     TEXT NOT NULL,
    roster_id     INTEGER NOT NULL,
    owner_id      TEXT,
    player_id     TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, league_id, roster_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_player_snapshot_pid ON player_snapshot (player_id);
CREATE INDEX IF NOT EXISTS idx_trending_pid ON trending_snapshot (player_id, kind);
CREATE INDEX IF NOT EXISTS idx_roster_pid ON roster_snapshot (league_id, player_id);
"""


def db_path() -> Path:
    return Path(config.CACHE_DIR) / "snapshots.sqlite3"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def today() -> str:
    return date.today().isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────


def capture(
    players: Dict[str, Any],
    trending: Optional[Dict[str, Dict[str, int]]] = None,
    rosters: Optional[List[Dict[str, Any]]] = None,
    league_id: Optional[str] = None,
    *,
    snapshot_date: Optional[str] = None,
) -> Dict[str, int]:
    """
    Write today's snapshot. Idempotent — re-running replaces the day's rows
    rather than duplicating them, so calling this on every tool invocation is
    safe and the last write of the day wins.

    trending is {"add": {pid: count}, "drop": {pid: count}}.

    Returns row counts written, for the caller to report.
    """
    day = snapshot_date or today()
    now = int(time.time())
    written = {"players": 0, "trending": 0, "rosters": 0}

    conn = connect()
    try:
        with conn:
            player_rows = [
                (
                    day,
                    pid,
                    p.get("team"),
                    p.get("position"),
                    p.get("depth_chart_position"),
                    p.get("depth_chart_order"),
                    p.get("status"),
                    p.get("injury_status"),
                    p.get("injury_body_part"),
                    p.get("practice_participation"),
                    p.get("news_updated"),
                )
                for pid, p in players.items()
                if p.get("position") in TRACKED_POSITIONS and p.get("active")
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO player_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                player_rows,
            )
            written["players"] = len(player_rows)

            if trending:
                trend_rows = [
                    (day, kind, pid, count, now)
                    for kind, counts in trending.items()
                    for pid, count in (counts or {}).items()
                ]
                conn.executemany(
                    "INSERT OR REPLACE INTO trending_snapshot VALUES (?,?,?,?,?)",
                    trend_rows,
                )
                written["trending"] = len(trend_rows)

            if rosters and league_id:
                roster_rows = [
                    (day, league_id, r.get("roster_id"), r.get("owner_id"), pid)
                    for r in rosters
                    for pid in (r.get("players") or [])
                ]
                conn.executemany(
                    "INSERT OR REPLACE INTO roster_snapshot VALUES (?,?,?,?,?)",
                    roster_rows,
                )
                written["rosters"] = len(roster_rows)
    finally:
        conn.close()

    return written


def captured_today() -> bool:
    """True if a player snapshot already exists for today."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM player_snapshot WHERE snapshot_date = ? LIMIT 1", (today(),)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────


def snapshot_dates() -> List[str]:
    """Every date with a player snapshot, oldest first."""
    conn = connect()
    try:
        return [
            r["snapshot_date"]
            for r in conn.execute(
                "SELECT DISTINCT snapshot_date FROM player_snapshot ORDER BY snapshot_date"
            )
        ]
    finally:
        conn.close()


def previous_date(before: Optional[str] = None) -> Optional[str]:
    """The most recent snapshot date strictly before `before` (default today)."""
    cutoff = before or today()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT MAX(snapshot_date) AS d FROM player_snapshot WHERE snapshot_date < ?",
            (cutoff,),
        ).fetchone()
        return row["d"] if row and row["d"] else None
    finally:
        conn.close()


# Fields whose change is worth surfacing, and how to describe a change in each.
WATCHED_FIELDS = {
    "depth_chart_order": "depth chart",
    "injury_status": "injury",
    "practice_participation": "practice",
    "team": "team",
    "status": "status",
}


def diff_players(
    from_date: str,
    to_date: Optional[str] = None,
    positions: Iterable[str] = TRACKED_POSITIONS,
) -> List[Dict[str, Any]]:
    """
    Every watched-field change between two snapshots.

    Returns one row per changed player, carrying a list of {field, old, new}.
    Players absent from either snapshot are skipped: a player who first appears
    has no "before" to compare against, and reporting that as a change would
    bury the real movers under roster churn every time the player map updates.
    """
    end = to_date or today()
    pos_list = list(positions)
    placeholders = ",".join("?" * len(pos_list))

    conn = connect()
    try:
        query = f"""
            SELECT a.player_id AS player_id,
                   a.position  AS position,
                   b.team      AS team,
                   {", ".join(f"a.{f} AS old_{f}, b.{f} AS new_{f}" for f in WATCHED_FIELDS)}
            FROM player_snapshot a
            JOIN player_snapshot b
              ON a.player_id = b.player_id
             AND b.snapshot_date = ?
            WHERE a.snapshot_date = ?
              AND a.position IN ({placeholders})
        """
        rows = conn.execute(query, [end, from_date, *pos_list]).fetchall()
    finally:
        conn.close()

    changes: List[Dict[str, Any]] = []
    for row in rows:
        deltas = [
            {"field": field, "old": row[f"old_{field}"], "new": row[f"new_{field}"]}
            for field in WATCHED_FIELDS
            if row[f"old_{field}"] != row[f"new_{field}"]
        ]
        if deltas:
            changes.append({
                "player_id": row["player_id"],
                "position": row["position"],
                "team": row["team"],
                "changes": deltas,
            })
    return changes


def trending_series(player_id: str, kind: str = "add", days: int = 14) -> List[Dict[str, Any]]:
    """One player's daily add or drop counts, oldest first."""
    conn = connect()
    try:
        return [
            {"date": r["snapshot_date"], "count": r["count"]}
            for r in conn.execute(
                """
                SELECT snapshot_date, count FROM trending_snapshot
                WHERE player_id = ? AND kind = ?
                ORDER BY snapshot_date DESC LIMIT ?
                """,
                (player_id, kind, days),
            )
        ][::-1]
    finally:
        conn.close()


def roster_changes(league_id: str, from_date: str, to_date: Optional[str] = None) -> Dict[str, List[str]]:
    """
    Who joined and who left rosters in this league between two snapshots.

    Dropped players are the interesting half: someone whose role just improved
    but who was cut across the league is free talent nobody is watching.
    """
    end = to_date or today()
    conn = connect()
    try:
        def ids(day: str) -> set:
            return {
                r["player_id"]
                for r in conn.execute(
                    "SELECT DISTINCT player_id FROM roster_snapshot "
                    "WHERE league_id = ? AND snapshot_date = ?",
                    (league_id, day),
                )
            }

        before, after = ids(from_date), ids(end)
    finally:
        conn.close()

    if not before or not after:
        return {"added": [], "dropped": []}
    return {
        "added": sorted(after - before),
        "dropped": sorted(before - after),
    }
