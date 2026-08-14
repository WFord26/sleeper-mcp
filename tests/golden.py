#!/usr/bin/env python3
"""
Golden file harness for the MCP tools (ADR 001 phase 1, step 6).

Calls every tool against the live API and records its exact output. Run once
before refactoring, once after, then diff. If the diff is empty the refactor
provably did not change what Claude sees.

Usage:
    python3 tests/golden.py capture before
    python3 tests/golden.py capture after
    python3 tests/golden.py diff
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sleeper_fantasy_mcp as srv  # noqa: E402

OUT_DIR = Path(__file__).parent / "golden"

# (tool name, kwargs). Tools taking a params model get a dict, built below.
CASES = [
    ("sleeper_get_my_team", None),
    ("sleeper_get_league_standings", None),
    ("sleeper_get_optimal_lineup", None),
    ("sleeper_get_injury_report", None),
    ("sleeper_get_bye_week_report", None),
    ("sleeper_get_available_players", {"position": "RB", "limit": 10}),
    ("sleeper_get_waiver_recommendations", {"limit": 5}),
    ("sleeper_get_player_stats", {"player_name": "Bijan Robinson"}),
    ("sleeper_get_trade_targets", {"limit": 5}),
    ("sleeper_get_strength_of_schedule", {"player_name": "Bijan Robinson"}),
    ("sleeper_get_weather_report", {"player_name": "Bijan Robinson"}),
    ("sleeper_get_snap_report", {"player_name": "Bijan Robinson", "weeks_back": 4}),
    ("sleeper_get_draft_best_available", {"limit": 10}),
]


def _unwrap(tool):
    """FastMCP wraps the function; reach the underlying coroutine."""
    for attr in ("fn", "func", "_fn", "__wrapped__"):
        inner = getattr(tool, attr, None)
        if inner is not None:
            return inner
    return tool


async def capture(label: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = {}

    for name, kwargs in CASES:
        tool = getattr(srv, name, None)
        if tool is None:
            results[name] = "<<tool not found>>"
            continue
        fn = _unwrap(tool)
        try:
            if kwargs is None:
                out = await fn()
            else:
                # Rebuild the pydantic input model from its annotation
                import inspect

                sig = inspect.signature(fn)
                param = list(sig.parameters.values())[0]
                model = param.annotation
                out = await fn(model(**kwargs))
            results[name] = out
        except Exception as exc:  # noqa: BLE001
            results[name] = f"<<raised {type(exc).__name__}: {exc}>>"
        print(f"  captured {name}")

    path = OUT_DIR / f"{label}.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {path}")


def diff() -> int:
    before = json.loads((OUT_DIR / "before.json").read_text(encoding="utf-8"))
    after = json.loads((OUT_DIR / "after.json").read_text(encoding="utf-8"))

    changed = []
    for name in before:
        if before[name] != after.get(name):
            changed.append(name)

    if not changed:
        print(f"IDENTICAL: all {len(before)} tools produce byte for byte the same output.")
        return 0

    print(f"DIFFERENCES in {len(changed)} of {len(before)} tools:\n")
    for name in changed:
        print(f"--- {name} ---")
        b = str(before[name]).splitlines()
        a = str(after.get(name, "")).splitlines()
        import difflib

        for line in list(difflib.unified_diff(b, a, "before", "after", lineterm=""))[:40]:
            print(line)
        print()
    return 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "capture":
        label = sys.argv[2] if len(sys.argv) > 2 else "before"
        asyncio.run(capture(label))
    elif len(sys.argv) >= 2 and sys.argv[1] == "diff":
        sys.exit(diff())
    else:
        print(__doc__)
