"""
Head to head dashboard (ADR 001 phase 2).

Starlette app serving the all play grid, with a single background poller pushing
updates to every connected browser over server sent events.

Why server side polling rather than browser polling: with N tabs open, browser
polling multiplies upstream calls to Sleeper by N, putting a public API's rate
limit at the mercy of how many tabs someone left open. One server side poller
with SSE fanout keeps upstream volume flat regardless of viewer count. SSE is
one way and reconnects natively, which is all a scoreboard needs; websockets
would be over engineering.

Run: uv run web/app.py    (or: python3 web/app.py)
"""

import asyncio
import contextlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from sleeper import client, config, league  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"


class Broadcaster:
    """
    Fans one payload out to every connected browser.

    Each subscriber gets its own bounded queue. A slow or stalled client drops
    frames rather than applying backpressure to the poller, because a scoreboard
    that is one refresh behind is fine and a poller blocked on a dead socket is
    not.
    """

    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self.latest: Optional[Dict[str, Any]] = None
        self.last_updated: Optional[str] = None
        self.last_error: Optional[str] = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, payload: Dict[str, Any]) -> None:
        self.latest = payload
        self.last_updated = datetime.now(timezone.utc).isoformat()
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # drop the frame for this client, keep the poller moving


broadcaster = Broadcaster()


def _is_game_window() -> bool:
    """
    True when NFL games are plausibly in progress, in US Eastern time.

    Sunday midday through late night, Monday and Thursday evenings. Outside
    these windows the poller idles, because hammering a public API at 3am on a
    Tuesday is rude and pointless.
    """
    now = datetime.now(timezone.utc)
    # Eastern is UTC-5 or UTC-4; use -5 as the conservative approximation, which
    # widens the window slightly rather than closing it early.
    eastern_hour = (now.hour - 5) % 24
    weekday = now.weekday()  # Monday is 0
    if weekday == 6:  # Sunday
        return 12 <= eastern_hour or eastern_hour < 1
    if weekday in (0, 3):  # Monday, Thursday night
        return 19 <= eastern_hour or eastern_hour < 1
    if weekday == 5:  # Saturday, late season games
        return 12 <= eastern_hour or eastern_hour < 1
    return False


async def poll_loop() -> None:
    """Refresh the payload forever, fast during games and slow otherwise."""
    while True:
        try:
            payload = await league.build_dashboard_payload()
            broadcaster.last_error = None
            broadcaster.publish(payload)
        except Exception as exc:  # noqa: BLE001
            # Never let a transient upstream failure kill the poller; the
            # dashboard keeps showing the last good payload with a stale badge.
            broadcaster.last_error = client.describe_error(exc)
            print(f"[poller] {broadcaster.last_error}", file=sys.stderr)

        live = _is_game_window()
        # No point polling fast if nobody is watching.
        if not broadcaster.subscriber_count:
            interval = config.POLL_INTERVAL_IDLE
        else:
            interval = (
                config.POLL_INTERVAL_LIVE if live else config.POLL_INTERVAL_IDLE
            )
        await asyncio.sleep(interval)


async def _ensure_payload() -> Dict[str, Any]:
    if broadcaster.latest is None:
        broadcaster.publish(await league.build_dashboard_payload())
    return broadcaster.latest or {}


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def api_allplay(request: Request) -> JSONResponse:
    """Full dashboard payload: standings, grid, weekly scores."""
    try:
        payload = await _ensure_payload()
        return JSONResponse({
            **payload,
            "last_updated": broadcaster.last_updated,
            "error": broadcaster.last_error,
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": client.describe_error(exc)}, status_code=502)


async def api_week(request: Request) -> JSONResponse:
    """Every team's score for one week, plus that week's real matchup results."""
    try:
        week = int(request.path_params["week"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "week must be an integer"}, status_code=400)

    try:
        payload = await _ensure_payload()
        scores = (payload.get("weekly_scores") or {}).get(str(week))
        if not scores:
            return JSONResponse(
                {"error": f"no scores recorded for week {week}"}, status_code=404
            )

        teams = {str(t["roster_id"]): t for t in payload.get("teams", [])}
        rows: List[Dict[str, Any]] = sorted(
            (
                {
                    "roster_id": int(rid),
                    "team_name": teams.get(rid, {}).get("team_name", f"Roster {rid}"),
                    "points": pts,
                }
                for rid, pts in scores.items()
            ),
            key=lambda r: r["points"],
            reverse=True,
        )
        for i, row in enumerate(rows, start=1):
            row["week_rank"] = i
            # All play record for this single week
            row["beat"] = sum(1 for o in rows if o["points"] < row["points"])
            row["lost_to"] = sum(1 for o in rows if o["points"] > row["points"])
        return JSONResponse({"week": week, "scores": rows})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": client.describe_error(exc)}, status_code=502)


async def api_health(request: Request) -> JSONResponse:
    from sleeper import cache

    return JSONResponse({
        "ok": True,
        "last_updated": broadcaster.last_updated,
        "subscribers": broadcaster.subscriber_count,
        "in_game_window": _is_game_window(),
        "poll_interval": (
            config.POLL_INTERVAL_LIVE if _is_game_window() else config.POLL_INTERVAL_IDLE
        ),
        "cache": cache.memory.stats(),
        "error": broadcaster.last_error,
    })


async def api_stream(request: Request) -> StreamingResponse:
    """Server sent events: pushes a new payload whenever the poller refreshes."""

    async def event_source():
        q = broadcaster.subscribe()
        try:
            if broadcaster.latest is not None:
                yield _sse(broadcaster.latest)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield _sse(payload)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # holds the connection through proxies
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(payload: Dict[str, Any]) -> str:
    body = json.dumps({
        **payload,
        "last_updated": broadcaster.last_updated,
        "error": broadcaster.last_error,
    })
    return f"event: update\ndata: {body}\n\n"


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    """
    Start the poller on boot, tear it down cleanly on shutdown.

    Uses the lifespan protocol rather than on_startup/on_shutdown, which recent
    Starlette releases removed.
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    poller = asyncio.create_task(poll_loop())
    print(f"  Dashboard  http://{config.WEB_HOST}:{config.WEB_PORT}")
    try:
        yield
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        await client.close_client()


routes = [
    Route("/", index),
    Route("/api/allplay", api_allplay),
    Route("/api/week/{week}", api_week),
    Route("/api/health", api_health),
    Route("/api/stream", api_stream),
    Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        log_level="warning",
    )
