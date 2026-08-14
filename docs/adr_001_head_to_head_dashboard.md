# ADR 001: Head to head league dashboard

**Status:** Accepted, implemented
**Date:** 2026 08 11 (implemented 2026 08 11)
**Deciders:** William (owner, sole maintainer)
**Supersedes:** nothing
**Related:** `sleeper_fantasy_mcp.py` (current MCP server, 13 tools)

---

## Context

The repo today is one file: `sleeper_fantasy_mcp.py`, roughly 2,300 lines, exposing 13 FastMCP tools against the public Sleeper API plus ESPN (defensive stats, schedule) and Open Meteo (weather). It works well as a conversational surface. It is a poor foundation for a dashboard, for four specific reasons found in the audit:

1. **Every tool returns a markdown string.** `sleeper_get_league_standings` and friends end in `"\n".join(lines)`. A browser cannot consume that. All the useful computation (`calculate_fantasy_points`, `_compute_composite_scores`, `_build_def_rank_map`) is entangled with presentation in the same function bodies.
2. **Caching is module level globals with no TTL and no eviction.** `_players_cache`, `_weekly_stats_cache`, `_schedule_cache` and four others live for the process lifetime. That is correct for a short lived stdio MCP process. It is wrong for a long running web server polling every 30 seconds, where a never expiring `_nfl_state_cache` means the dashboard silently believes it is still week 1 in December.
3. **`_get` opens a new `httpx.AsyncClient` per request** (line 311). No connection pooling. Fine at conversational volume, wasteful at poll volume.
4. **Identity is hardcoded.** `SLEEPER_USERNAME = "GronkQuixote"`, `CURRENT_SEASON = "2026"`, `STATS_SEASON = "2025"` are module constants, and `_get_league` string matches on `"chrysoloras"`. A dashboard showing everyone vs everyone should be league centric, not "my team" centric.

### What we are actually building

An **all play grid**: for each week, score every team against every other team's score that week, not just their scheduled opponent. In a 12 team league each team plays 11 phantom opponents per week, producing an all play record (for example 8 and 3) alongside the real record. This is the standard way to separate real strength from schedule luck, and it is exactly what "everyone vs everyone" asks for.

Critically, this needs **no new data source**. `GET /league/{id}/matchups/{week}` returns `points` per roster per week. That single endpoint, replayed across weeks 1 through the current week, is the entire input to the grid.

### Constraints

- **Deadline: before the season starts.** NFL kickoff is roughly the second week of September, so about four weeks from today. The all play grid must be live for week 1; anything else is optional.
- **Near real time during Sunday games.** Refresh on page load is not sufficient.
- **Sleeper is read only, unauthenticated, and non commercial only.** Documented guidance: stay under 1,000 calls per minute or risk an IP block. The full player map is ~5 MB and the docs explicitly say fetch it at most once per day and store it yourself.
- **Single user hosting.** Runs on William's machine. No cloud, no auth, no multi tenancy required.
- **The MCP server must keep working.** It is in daily use and configured in Claude Desktop and Claude Code.

### The live scoring reality check

Sleeper's public v1 API has no push channel and no dedicated live scoring endpoint. Sleeper's own app uses an undocumented GraphQL/websocket path that is off limits here. What is available:

- `/league/{id}/matchups/{week}` → `points` per roster, which does update during games with modest lag.
- `/stats/nfl/regular/{season}/{week}` → per player stat lines, updated more slowly.

So "near real time" here means **polling on a 30 to 60 second interval during game windows**, not sub second push. That is genuinely good enough for a fantasy scoreboard and should be stated plainly rather than promised away.

Call volume at a 30 second poll of one endpoint is 2 calls per minute against a 1,000 per minute ceiling. The budget is not the constraint; correctness of cache invalidation is.

---

## Decision

**Extract a shared core module, then build the web server and the MCP server as two thin adapters over it.**

Concretely, split `sleeper_fantasy_mcp.py` into:

```
sleeper/
  client.py      one shared httpx.AsyncClient, retry, rate limit guard
  cache.py       TTL aware cache; different policies per data class
  scoring.py     calculate_fantasy_points and friends, pure functions
  league.py      rosters, users, matchups, all play computation -> typed dicts
  render.py      dict -> markdown, used only by the MCP adapter
mcp_server.py    13 existing tools, now thin: call league.py, pass through render.py
web/
  app.py         Starlette/FastAPI: JSON endpoints + SSE stream
  static/        single page dashboard
```

The rule: **`league.py` and `scoring.py` never produce a string for human consumption.** They return typed structures. `render.py` turns those into the markdown the MCP tools already emit, so tool output stays byte identical and nothing about the Claude Desktop experience changes.

**Live updates: server polls, browser subscribes over SSE.** One background task in the web server polls matchups on an interval, recomputes the grid, and pushes to all connected browsers via server sent events. Browsers never call Sleeper directly.

**Cache policy is per data class, not one global:**

| Data | TTL | Reasoning |
|---|---|---|
| `/players/nfl` (~5 MB) | 24 hours, persisted to disk | Docs mandate once daily; must survive restarts |
| `/state/nfl` | 5 minutes | Drives current week; the existing infinite cache is a latent bug |
| league, users, rosters | 10 minutes | Changes on waivers and trades |
| matchups, **completed** weeks | forever | Immutable once the week is final |
| matchups, **current** week | 30 seconds | This is the live path |

That last split is the single highest leverage design detail. Past weeks are immutable, so an entire season of all play history costs at most 17 API calls, computed once and never refetched. Only the current week is ever hot.

---

## Options considered

### Option A: Standalone dashboard that calls Sleeper directly, duplicating the logic

| Dimension | Assessment |
|---|---|
| Complexity | Low to start, high later |
| Time to week 1 | Fastest, roughly 1 week |
| Correctness risk | High |
| Maintenance | Poor |

**Pros:** Zero risk to the working MCP server. Ship fastest. Clean greenfield code.

**Cons:** `calculate_fantasy_points` and the 32 stadium map and the ESPN abbreviation overrides get copy pasted. The two copies drift the first time a scoring rule is tweaked, and then Claude and the dashboard disagree about a player's points, which is exactly the failure that destroys trust in both. Rejected on that basis, despite being the fastest path.

### Option B: Shared core, two thin adapters (recommended)

| Dimension | Assessment |
|---|---|
| Complexity | Medium up front, low ongoing |
| Time to week 1 | Roughly 2 to 3 weeks |
| Correctness risk | Low, one source of truth |
| Maintenance | Good |

**Pros:** One scoring implementation. The refactor also fixes real latent bugs already present in the MCP server (the never expiring NFL state cache, the per request client). Typed returns from `league.py` make the whole thing testable for the first time; today nothing is unit testable because every function ends in markdown. Future tools get the dashboard for free and vice versa.

**Cons:** Touches working code under deadline pressure. Mitigated by the render layer: MCP tool output is unchanged by construction, and a golden file test comparing before and after output makes that verifiable rather than hopeful.

### Option C: Dashboard acts as an MCP client, calling the server over stdio

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Time to week 1 | Roughly 2 weeks |
| Correctness risk | High |
| Maintenance | Poor |

**Pros:** No refactor of the existing file. Genuinely one implementation.

**Cons:** The dashboard would be parsing markdown tables out of tool responses to get numbers back, which is fragile and absurd. Adds a subprocess and an MCP handshake to every page load. MCP is a model facing protocol; using it as an internal data API is a category error. Rejected.

### Option D: Cowork artifact instead of a local web app

| Dimension | Assessment |
|---|---|
| Complexity | Lowest |
| Time to week 1 | Days |
| Correctness risk | Low |
| Live capability | Insufficient |

**Pros:** Nothing to host or run. Calls MCP tools directly. Persists across sessions.

**Cons:** Refreshes when opened, not on a 30 second timer. No background polling, so the Sunday afternoon requirement is not met. **Worth keeping as a fallback** if the season arrives before the web app is ready: an artifact rendering the all play grid on open is a legitimate week 1 stopgap.

---

## Trade off analysis

The real tension is **duplication risk versus deadline risk**. Option A ships sooner; Option B is the one still standing in December.

Option B wins because the extraction is smaller than it looks. The scoring functions (lines 201 to 306) are already pure and move without modification. The cache and client work is a genuine improvement being made anyway. The bulk of the effort is mechanically splitting each tool body at the point where it starts building `lines`, and that seam is already visible in every tool.

The second trade off is **push versus poll on the browser side**. Browser side polling is simpler, but with N tabs open it multiplies calls to Sleeper by N and puts a public API's rate limit at the mercy of how many tabs are open. Server side polling with SSE fanout keeps upstream call volume flat regardless of viewers, and SSE is one way and reconnects natively, which is all a scoreboard needs. Websockets would be over engineering here.

The third: **all play is a derived metric with no upstream source of truth**, so it must be recomputed and cannot be reconciled against Sleeper. This argues for making the raw weekly scores visible in the UI so any number on the grid can be traced back to inputs.

---

## Consequences

**Easier**

- Scoring changes land in one place and both surfaces update together.
- The core becomes unit testable; today it effectively is not.
- New tools and new dashboard views draw from the same well.
- Multi league and multi user support becomes a parameter change rather than a rewrite, since hardcoded identity gets pushed to config during the extraction.

**Harder**

- Two processes to run instead of one. The web server has to be started, and it will be forgotten at some point on a Sunday.
- Cache invalidation is now a real concern rather than something ignorable.
- Disk persistence for the player map introduces a stale file failure mode.

**To revisit**

- If a second person ever uses the dashboard, the no auth assumption needs review before it leaves localhost.
- Sleeper's non commercial terms cap this at personal use. Do not put it on a public URL.
- If polling proves too laggy on Sundays, the fallback is layering `/stats/nfl/regular/{season}/{week}` for per player detail, accepting that it trails matchup totals.

---

## Action items

**Phase 0, this week: prove the concept before refactoring anything**

1. [ ] Write a throwaway script that pulls `/league/{id}/matchups/{week}` for weeks 1 to 17 of the 2025 season and computes the all play grid. Confirm the numbers look right against the known final standings.
2. [ ] Decide what the grid cell shows: raw points differential, win or loss, or margin. Sketch it before writing UI code.

**Phase 1, week 2: extract the core**

3. [ ] Create `sleeper/scoring.py` by moving lines 201 to 306 unchanged. No behavior change.
4. [ ] Create `sleeper/client.py` with one shared `AsyncClient`, and `sleeper/cache.py` with the per class TTL table above.
5. [ ] Create `sleeper/league.py` returning typed dicts. Start with the three functions the dashboard needs: rosters, users, matchups by week.
6. [ ] Add golden file tests capturing current MCP tool output, then repoint the tools at the core and confirm output is identical.

**Phase 2, week 3: the web app**

7. [ ] Starlette app with `GET /api/allplay`, `GET /api/week/{n}`, and `GET /api/stream` (SSE).
8. [ ] Background poller: 30 second interval during game windows, idle otherwise. Do not poll at 3am on a Tuesday.
9. [ ] Single page front end: the grid, sortable, with a week selector and real record shown next to all play record.

**Phase 3, week 4: buffer and polish**

10. [ ] Deliberately left empty. Something in phases 1 and 2 will overrun.
11. [ ] If phase 2 slips, ship the Cowork artifact fallback from Option D for week 1 and finish the web app during the season.

**Open question to settle before phase 1**

12. [x] Does the dashboard stay single league, or does the extraction parameterize league ID now? **Resolved: parameterized.** `sleeper/config.py` reads every identity value from the environment with the previous hardcoded values as defaults, so nothing changed behaviorally and pointing at another league or season is now a variable.

---

## Implementation notes

Recorded after the fact, because two things differed from the plan.

**Phase 0 paid for itself immediately.** The throwaway script confirmed the math (all play wins equal all play losses league wide, the top scorer ranks first by all play) and surfaced the finding that justifies the whole project: in 2025, Boulangers finished 6 and 8 while posting the third best all play record, meaning they were 19.5 percentage points unluckier than their scoring deserved. That is invisible in Sleeper's own standings.

**One real bug was caught by running against a completed season.** `get_completed_week_range` originally derived the week range from live NFL state alone, so viewing a finished 2025 league during the 2026 preseason reported zero weeks played for a season that had a champion. The league's own `status` and `season` now take priority over the global calendar. Worth noting because it would have been invisible until someone tried to browse history mid season.

**The golden file approach worked exactly as hoped.** All 13 pre existing tools produce byte for byte identical output after the refactor, verified by diff rather than by hope. Scoring extraction was separately verified against the original implementation across 18,000 randomized stat lines with zero mismatches.

**Phase 3 buffer was not needed**, so the Option D artifact fallback was not built.
