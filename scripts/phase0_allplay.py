#!/usr/bin/env python3
"""
Phase 0 proof (ADR 001, action item 1).

Pulls every regular season week of matchups for the 2025 league and computes the
all play grid, then sanity checks it against the real final standings.

Throwaway by design: no caching, no error handling, direct httpx. If the numbers
here look right, the model in sleeper/league.py is correct.

Run: uv run scripts/phase0_allplay.py
"""

import asyncio
import httpx

LEAGUE_ID = "1257056909626724352"  # The Chrysoloras Gang, 2025 (completed)
API = "https://api.sleeper.app/v1"


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        league = (await client.get(f"{API}/league/{LEAGUE_ID}")).json()
        rosters = (await client.get(f"{API}/league/{LEAGUE_ID}/rosters")).json()
        users = (await client.get(f"{API}/league/{LEAGUE_ID}/users")).json()

        playoff_start = league["settings"]["playoff_week_start"]
        last_week = playoff_start - 1

        weeks = await asyncio.gather(*[
            client.get(f"{API}/league/{LEAGUE_ID}/matchups/{w}")
            for w in range(1, last_week + 1)
        ])

    user_by_id = {u["user_id"]: u for u in users}
    name_by_roster = {}
    for r in rosters:
        u = user_by_id.get(r.get("owner_id")) or {}
        meta = u.get("metadata") or {}
        name_by_roster[r["roster_id"]] = (
            meta.get("team_name") or u.get("display_name") or f"Roster {r['roster_id']}"
        )

    # all play: every week, score each team against every other team's score
    aw = {rid: 0 for rid in name_by_roster}
    al = {rid: 0 for rid in name_by_roster}
    at = {rid: 0 for rid in name_by_roster}
    real_w = {rid: 0 for rid in name_by_roster}
    real_l = {rid: 0 for rid in name_by_roster}
    total_pts = {rid: 0.0 for rid in name_by_roster}

    for week_no, resp in enumerate(weeks, start=1):
        entries = resp.json()
        scores = {
            m["roster_id"]: float(m.get("points") or 0.0)
            for m in entries
            if m.get("roster_id") in name_by_roster
        }
        # a week with every score at zero never happened; skip it
        if not scores or all(v == 0.0 for v in scores.values()):
            print(f"  (week {week_no} has no scores, skipping)")
            continue

        for rid, pts in scores.items():
            total_pts[rid] += pts
            for other, opts in scores.items():
                if other == rid:
                    continue
                if pts > opts:
                    aw[rid] += 1
                elif pts < opts:
                    al[rid] += 1
                else:
                    at[rid] += 1

        # real head to head, derived from matchup_id pairing
        pairs: dict = {}
        for m in entries:
            pairs.setdefault(m.get("matchup_id"), []).append(m)
        for mid, side in pairs.items():
            if mid is None or len(side) != 2:
                continue
            a, b = side
            pa, pb = float(a.get("points") or 0), float(b.get("points") or 0)
            if pa > pb:
                real_w[a["roster_id"]] += 1
                real_l[b["roster_id"]] += 1
            elif pb > pa:
                real_w[b["roster_id"]] += 1
                real_l[a["roster_id"]] += 1

    print(f"\nAll play through week {last_week} ({league['name']}, {league['season']})\n")
    header = f"{'Team':<24} {'Real':>7} {'All play':>10} {'AP%':>7} {'Points':>9} {'Luck':>6}"
    print(header)
    print("=" * len(header))

    rows = sorted(
        name_by_roster,
        key=lambda rid: (aw[rid] / max(aw[rid] + al[rid] + at[rid], 1)),
        reverse=True,
    )
    for rid in rows:
        games = aw[rid] + al[rid] + at[rid]
        ap_pct = aw[rid] / games if games else 0.0
        real_games = real_w[rid] + real_l[rid]
        real_pct = real_w[rid] / real_games if real_games else 0.0
        luck = real_pct - ap_pct  # positive means the schedule was kind
        print(
            f"{name_by_roster[rid][:24]:<24} "
            f"{str(real_w[rid]) + '-' + str(real_l[rid]):>7} "
            f"{str(aw[rid]) + '-' + str(al[rid]):>10} "
            f"{ap_pct:>6.1%} {total_pts[rid]:>9.1f} {luck:>+6.1%}"
        )

    # sanity: total all play wins must equal total all play losses
    print(f"\nCheck: all play W total {sum(aw.values())} == L total {sum(al.values())} "
          f"-> {sum(aw.values()) == sum(al.values())}")
    # sanity: real wins should equal real losses too
    print(f"Check: real W total {sum(real_w.values())} == L total {sum(real_l.values())} "
          f"-> {sum(real_w.values()) == sum(real_l.values())}")
    # sanity: the team with the most points should have a top all play record
    top_pts = max(total_pts, key=total_pts.get)
    print(f"Check: most points ({name_by_roster[top_pts]}) all play rank "
          f"{rows.index(top_pts) + 1} of {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
