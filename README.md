# Sleeper Fantasy Football MCP Server

A FastMCP Python server that connects Claude to the **Sleeper public API** for managing your fantasy football team. Defaults to **GronkQuixote** in **The Chrysoloras Gang**, but username and league are changeable at runtime without restarting the server.

No API key needed — the Sleeper API is fully public.

---

## Prerequisites

- **Python 3.10+** — the `mcp` package requires it. macOS ships with Python 3.9. See below.

---

## Installation

### Step 1 — Get Python 3.10+ (pick one)

**Option A: `uv` (recommended — fast, self-contained)**

```bash
# Install uv if you don't have it
curl -Ls https://astral.sh/uv/install.sh | sh

# uv will auto-download Python 3.11 the first time it runs the server
# No other steps needed for dependencies — see Step 3 (uv path)
```

**Option B: Homebrew Python**

```bash
brew install python@3.11
# Confirm:
python3.11 --version
```

---

### Step 2 — Place the server file somewhere stable

```bash
mkdir -p ~/mcp-servers
cp sleeper_fantasy_mcp.py ~/mcp-servers/
```

---

### Step 3 — Install dependencies

`fastmcp` is used instead of `mcp[cli]` — some environments' default `mcp` package doesn't expose `mcp.server.fastmcp` the way this server expects, while the standalone `fastmcp` package always does. The script tries `mcp.server.fastmcp` first and falls back to `fastmcp` automatically.

**If you chose uv (Option A):** nothing to install ahead of time — pass deps inline at run time (Step 4), or drop the included `pyproject.toml` in the same folder as the script and just run `uv run sleeper_fantasy_mcp.py`.

**If you chose Homebrew Python (Option B):**
```bash
python3.11 -m pip install fastmcp httpx pydantic
```

---

### Step 4 — Verify it works

**uv (inline deps, no pyproject.toml needed):**
```bash
uv run --python 3.11 --with fastmcp --with httpx --with pydantic ~/mcp-servers/sleeper_fantasy_mcp.py
# Should start quietly — Ctrl-C to exit
```

**uv (with the included pyproject.toml in the same folder):**
```bash
uv run ~/mcp-servers/sleeper_fantasy_mcp.py
```

**Homebrew Python:**
```bash
python3.11 ~/mcp-servers/sleeper_fantasy_mcp.py
# Should start quietly — Ctrl-C to exit
```

---

## Configuration

### Claude Code (recommended)

**uv path:**
```bash
claude mcp add sleeper_fantasy -- uv run --python 3.11 --with fastmcp --with httpx --with pydantic /Users/YOUR_USERNAME/mcp-servers/sleeper_fantasy_mcp.py
```

**Homebrew Python path:**
```bash
claude mcp add sleeper_fantasy -- python3.11 /Users/YOUR_USERNAME/mcp-servers/sleeper_fantasy_mcp.py
```

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` — merge this into the existing `mcpServers` object (don't overwrite other keys in the file):

**uv path:**
```json
{
  "mcpServers": {
    "sleeper_fantasy": {
      "command": "uv",
      "args": [
        "run", "--python", "3.11",
        "--with", "fastmcp", "--with", "httpx", "--with", "pydantic",
        "/Users/YOUR_USERNAME/mcp-servers/sleeper_fantasy_mcp.py"
      ]
    }
  }
}
```

**Homebrew Python path:**
```json
{
  "mcpServers": {
    "sleeper_fantasy": {
      "command": "python3.11",
      "args": ["/Users/YOUR_USERNAME/mcp-servers/sleeper_fantasy_mcp.py"]
    }
  }
}
```

Fully quit (Cmd-Q) and relaunch Claude Desktop after saving — it only reads this file at startup.

---

## Available Tools

| Tool | What it does |
|------|-------------|
| `sleeper_get_my_team` | Your current roster, record, and points |
| `sleeper_get_available_players` | Top unrostered players for a position, ranked by projected pts |
| `sleeper_get_waiver_recommendations` | Best waiver wire / FA pickups this week |
| `sleeper_get_league_standings` | Full standings with playoff picture |
| `sleeper_get_player_stats` | Season stats + projections for any named player |
| `sleeper_get_trade_targets` | Undervalued free agents (projected pts ÷ ownership%) |
| `sleeper_get_optimal_lineup` | Computes your best possible lineup this week and flags swaps vs. your current starters |
| `sleeper_get_injury_report` | Scans your roster for Questionable/Doubtful/Out/IR designations |
| `sleeper_get_bye_week_report` | Maps every roster player's bye week (from the real NFL schedule) and flags collisions |
| `sleeper_get_strength_of_schedule` | Ranks a player's upcoming opponents easiest→hardest, with a playoff-weeks (15-17) callout |
| `sleeper_get_weather_report` | Forecast for a player's (or your whole roster's) next game, with a fantasy-impact note |
| `sleeper_get_snap_report` | Snap share and rush/target usage trend over recent weeks — an early breakout signal |
| `sleeper_get_draft_best_available` | Live draft-day board: best remaining players by ADP, on-the-clock tracking, positional scarcity |
| `sleeper_get_all_play_standings` | All play standings: every team scored against every other team every week, with a luck column |
| `sleeper_get_head_to_head_grid` | Everyone vs everyone matrix — each team's record against each other team |
| `sleeper_get_config` | Show the active username, league, and season settings |
| `sleeper_set_username` | Switch to a different Sleeper username (no restart needed) |
| `sleeper_set_league` | Switch to a different league by name or league ID (no restart needed) |

---

## Dashboard

A browser dashboard for the all play grid, with live updates during games. See
[docs/adr_001_head_to_head_dashboard.md](docs/adr_001_head_to_head_dashboard.md)
for the design rationale.

```bash
# install the web extras once
uv pip install -e ".[web]"

# run it
uv run web/app.py
# then open http://127.0.0.1:8080
```

Three views: **standings** (sortable, with the luck column), **head to head
grid** (the everyone vs everyone matrix), and **by week** (each week's scores
with how many teams you would have beaten that week).

The server polls Sleeper on one background task and pushes updates to every open
tab over server sent events, so upstream call volume stays flat no matter how
many tabs are open. Polling is every 30 seconds during game windows and every 15
minutes otherwise; it idles entirely when nobody is watching.

### Architecture

Per ADR 001, shared logic lives in `sleeper/` and both surfaces are thin
adapters over it, so scoring can never drift between Claude and the dashboard.

```
sleeper/
  config.py    settings, overridable by env var or runtime MCP tools
  client.py    one pooled httpx client, retry, rate limit guard
  cache.py     TTL cache per data class, disk persistence for the player map
  scoring.py   fantasy point calculation (the only copy)
  league.py    typed data access plus the all play model
  render.py    data -> markdown, used only by the MCP adapter
sleeper_fantasy_mcp.py   MCP adapter (18 tools)
web/app.py               dashboard adapter (JSON + SSE)
```

Key caching decision: completed weeks are immutable, so they are cached forever
and a full season of history costs at most 17 API calls. Only the current week
is ever refetched.

### Configuration

Every value has a working default; override only what you need.

| Variable | Default | Purpose |
|---|---|---|
| `SLEEPER_USERNAME` | `GronkQuixote` | Sleeper account |
| `SLEEPER_LEAGUE_ID` | unset | Pin a specific league, skipping name matching |
| `SLEEPER_LEAGUE_MATCH` | `chrysoloras` | League name fragment to match |
| `SLEEPER_SEASON` | `2026` | Active season |
| `SLEEPER_WEB_PORT` | `8080` | Dashboard port |
| `SLEEPER_POLL_LIVE` | `30` | Poll seconds during games |
| `SLEEPER_POLL_IDLE` | `900` | Poll seconds otherwise |

You can also change username and league **at runtime** without restarting the server, using the MCP tools:

```
# In Claude:
Switch to username JohnDoe
Change my league to Dynasty Kings
Set league ID to 1234567890
What username is the Sleeper MCP using?
```

Runtime changes take effect immediately and flush the relevant cache entries.

Point it at a past season to browse history:

```bash
SLEEPER_LEAGUE_ID=1257056909626724352 SLEEPER_SEASON=2025 uv run web/app.py
```

### Tests

```bash
python3 -m pytest tests/test_allplay.py -q   # all play model, 14 tests

# golden file check: proves a refactor did not change MCP tool output
python3 tests/golden.py capture before
# ...make changes...
python3 tests/golden.py capture after
python3 tests/golden.py diff
```

---

## Example Prompts

```
Show me my team
What top RBs are available on waivers?
Who should I pick up this week at WR and TE?
What are the league standings?
Get stats for Justin Jefferson
Who are my best trade targets right now?
What's my optimal lineup this week?
Is anyone on my team injured?
Do I have any bye week collisions coming up?
What's Bijan Robinson's strength of schedule for the playoffs?
What's the weather looking like for my QB's game?
Check the snap share trend for my RB2
Who's the best player available in my draft?
Best available RBs right now in the draft
Who's on the clock?

# Change identity at runtime (no restart needed)
Switch to username TDMachine99
Change my league to Dynasty Kings
Set league ID to 1234567890
What username and league am I connected to?
```

---

## League Details

| Setting | Value |
|---------|-------|
| **League** | The Chrysoloras Gang |
| **Teams** | 12 |
| **Scoring** | Full PPR (1 pt/rec) |
| **Roster** | QB, 2 RB, 2 WR, TE, 2 FLEX (W/R/T), K, DEF, 6 BN, 2 IR |
| **Playoffs** | Top 6 teams, starts Week 15 |
| **Trade deadline** | Week 12 |
| **Waivers clear** | Wednesday 1 AM MDT |
| **Waiver processing** | Wed / Thu / Fri at 8 AM MDT |

---

## Scoring Reference (Chrysoloras Gang)

**Passing:** 0.04/yd · TD=6 · INT=-2 · 2pt=2  
Bonuses: 40+ yd completion=+1 · 40+ yd TD=+1 · 50+ yd TD=+1  
Game milestones: 300-399 pass yds=+1 · 400+ pass yds=+2 · 25+ completions=+1

**Rushing:** 0.1/yd · TD=6 · 0.2/attempt · 2pt=2  
Bonuses: 40+ yd rush=+1 · 40+ yd rush TD=+1 · 50+ yd rush TD=+1  
Game milestones: 100-199 rush yds=+1 · 200+ rush yds=+1 (cumulative: +2 for 200+)

**Receiving (Full PPR):** 1/rec · 0.1/yd · TD=6 · 2pt=2  
Bonuses: 40+ yd rec=+1 · 40+ yd rec TD=+1 · 50+ yd rec TD=+1  
Game milestones: 100-199 rec yds=+1 · 200+ rec yds=+1 (cumulative: +2 for 200+)  
Combined: 200+ rush+rec yds=+1

**Kicking:** FG 0-39=3 · FG 40-49=4 · FG 50+=5 · PAT=1 · FG miss=-1 · PAT miss=-1

**Defense:** TD=6 · Sack=1 · INT=2 · Fum rec=2 · Safety=2 · FF=1 · Blk kick=2 · 4th down stop=1  
PA0=7 · PA1-6=6 · PA7-13=5 · PA14-20=2 · PA28-34=-1 · PA35+=-4  
YA<100=5 · YA100-199=3 · YA200-299=2 · YA300-349=1 · YA400-449=-1 · YA450-499=-3 · YA500-549=-5 · YA550+=-6

**Misc:** Fum lost=-2 · Fum rec TD=6

---

## Data Sources

| Source | Used for | Auth |
|--------|----------|------|
| Sleeper API | Roster, league, stats, weekly projections, snap counts | None |
| ESPN public scoreboard API | Real NFL schedule, opponents, home/away, venue (Sleeper has no schedule endpoint) | None |
| Open-Meteo | 16-day weather forecast for game venues | None |

The ESPN and Open-Meteo endpoints are free, public, and don't require registration — but they're also unofficial/undocumented in ESPN's case, so if either changes shape upstream, the schedule/weather/bye-week/SOS tools may need small fixes (they fail gracefully with a message rather than crashing).

---

## Notes

- **Player cache:** The Sleeper `/players/nfl` endpoint is ~10 MB. The server caches it in memory for the duration of the session, so the first call per session is slower.
- **Projections & stats URL shape:** Sleeper requires `season_type` as a *path segment*, not a query parameter — e.g. `/projections/nfl/regular/2026/5`, not `/projections/nfl/2026/5?season_type=regular`. Passing it as a query param is accepted by the API but silently returns near-empty data. This is fixed in the current version; if you're diffing against an earlier copy, this was the cause of `get_available_players` etc. always showing ~0 projected points.
- **Projections availability:** Sleeper publishes weekly projections once the season is active. Before the season starts, tools fall back to Week 1 projections where possible, but during the true off-season projections may still be empty.
- **Strength of schedule caveat:** Opponent difficulty uses Sleeper's own `fan_pts_allow_<position>` defensive stat (e.g. `fan_pts_allow_rb`), which is scored under Sleeper's default rules, not recalculated under Chrysoloras Gang scoring. Treat the 1–32 ranks as directional, not exact point projections. It also falls back to the most recently completed season's data if the current season has no games played yet.
- **Weather window:** Open-Meteo only forecasts ~16 days out. Games further away show "not yet available" until closer to kickoff — there's no way around this with a free forecast API.
- **Snap counts:** Sourced from Sleeper's own weekly box-score stats (`off_snp` / `tm_off_snp`), not a third-party service — no extra dependency needed.
- **Bye weeks:** Derived by finding the week each team is absent from the ESPN schedule, not stored anywhere directly.
- **Season year:** The server uses `CURRENT_SEASON = "2026"` and `STATS_SEASON = "2025"`. Update these constants at the top of `sleeper_fantasy_mcp.py` if you're using this in a different year.
- **League matching:** The server matches your league by searching for "chrysoloras" in the league name. If the name changes, update the match logic in `_get_league()`.
- **Pre-draft state:** Before your league's draft happens, your roster is empty — `get_optimal_lineup`, `get_injury_report`, and `get_bye_week_report` will say so rather than showing misleading empty tables. Use `get_draft_best_available` instead during the actual draft.
- **Composite ranking:** `get_available_players` and `get_waiver_recommendations` rank by `proj_pts + SOS bonus (±2 pts) + snap trend bonus (±3 pts)`, not raw projection alone — a player with a rising snap share or a soft upcoming schedule can outrank a higher-projected player who's losing his role. Weights are tunable constants at the top of the file (`SOS_MAX_BONUS`, `SNAP_TREND_MAX_BONUS`, etc.).
- **Draft ADP:** `get_draft_best_available` uses Sleeper's `adp_dd_ppr` / `pos_adp_dd_ppr` fields from the projections endpoint (falls back to `search_rank` if ADP isn't populated for a player yet). It reads the *live draft's* pick list, not season rosters, so it stays accurate mid-draft before rosters update, and shows who's on the clock via snake-draft math from the draft order.
