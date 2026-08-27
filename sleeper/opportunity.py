"""
Expected fantasy points (xFP) from opportunity, and the regression gap.

The idea: a player's fantasy line is opportunity times efficiency, and only
opportunity is stable week to week. Touchdowns in particular are close to noise
at the sample sizes a fantasy season provides. So model the points a player
*should* have scored given his carries, targets, air yards, and red zone looks,
then subtract what he actually scored.

    residual = actual - expected

A large negative residual is the buy signal this whole project is about: the
player is being fed, the box score has not paid him yet, and the field prices
players off box scores. A large positive residual is the sell signal — he has
been finishing everything, and finishing rates do not hold.

The coefficients are not hardcoded. They are fit by ordinary least squares on
the current season's own player-weeks, using the league's own scoring function.
That matters: this league pays 0.2 per carry and stacks milestone bonuses, so
any table of league-average points-per-touch borrowed from elsewhere would be
wrong here. Fitting in place makes the model correct for whatever scoring
settings the league actually runs.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Opportunity features per position. Each is a volume or leverage count that the
# offense controls, never an efficiency outcome — no yards, no touchdowns, no
# receptions. Putting an outcome in here would let the model explain away the
# very luck we are trying to isolate.
FEATURES: Dict[str, List[str]] = {
    "QB": ["pass_att", "pass_rz_att", "rush_att", "rush_rz_att"],
    "RB": ["rush_att", "rush_rz_att", "rec_tgt", "rec_rz_tgt"],
    "WR": ["rec_tgt", "rec_rz_tgt", "rec_air_yd"],
    "TE": ["rec_tgt", "rec_rz_tgt", "rec_air_yd"],
}

# Below this many player-weeks a position's fit is noise, so we decline to
# report rather than publish a confident looking number built on nothing.
MIN_ROWS_PER_POSITION = 40

# Ridge term on the normal equations. Small enough not to bias the fit, large
# enough to keep a nearly collinear season (e.g. rz_att almost proportional to
# att in a short sample) from producing a singular matrix.
RIDGE = 1e-6


def solve_ols(
    rows: Sequence[Sequence[float]],
    targets: Sequence[float],
) -> Optional[List[float]]:
    """
    Least squares fit with an intercept, via the normal equations.

    Returns [intercept, b1, b2, ...], or None if the system is singular.

    Implemented directly rather than with numpy because the design matrix here
    is at most 5 wide; pulling a numeric stack into an MCP server's dependency
    list to inverse a 5x5 is not a trade worth making.
    """
    if not rows or len(rows) != len(targets):
        return None

    width = len(rows[0]) + 1  # +1 for the intercept column
    if any(len(r) + 1 != width for r in rows):
        return None

    # Build the augmented normal equations [X'X | X'y] with the intercept as
    # a leading column of ones.
    xtx = [[0.0] * width for _ in range(width)]
    xty = [0.0] * width
    for row, y in zip(rows, targets):
        vec = [1.0] + list(row)
        for i in range(width):
            xty[i] += vec[i] * y
            for j in range(width):
                xtx[i][j] += vec[i] * vec[j]

    for i in range(width):
        xtx[i][i] += RIDGE

    # Gaussian elimination with partial pivoting.
    aug = [xtx[i] + [xty[i]] for i in range(width)]
    for col in range(width):
        pivot = max(range(col, width), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pval = aug[col][col]
        for r in range(width):
            if r == col:
                continue
            factor = aug[r][col] / pval
            if factor == 0.0:
                continue
            for c in range(col, width + 1):
                aug[r][c] -= factor * aug[col][c]

    return [aug[i][width] / aug[i][i] for i in range(width)]


def _feature_vector(stat: Dict[str, Any], features: Sequence[str]) -> List[float]:
    return [float(stat.get(f) or 0.0) for f in features]


def fit_models(
    weekly_blobs: Dict[int, Dict[str, Any]],
    players: Dict[str, Any],
    score_fn: Callable[[Dict[str, Any], str], float],
    positions: Iterable[str] = ("QB", "RB", "WR", "TE"),
) -> Dict[str, Dict[str, Any]]:
    """
    Fit one opportunity -> points model per position on the given weeks.

    Returns {position: {"coefficients": [...], "features": [...], "rows": n}},
    omitting any position that did not clear MIN_ROWS_PER_POSITION.
    """
    samples: Dict[str, Tuple[List[List[float]], List[float]]] = {
        pos: ([], []) for pos in positions if pos in FEATURES
    }

    for stats_by_pid in weekly_blobs.values():
        for pid, stat in (stats_by_pid or {}).items():
            pos = (players.get(pid) or {}).get("position")
            if pos not in samples:
                continue
            features = FEATURES[pos]
            vec = _feature_vector(stat, features)
            if sum(abs(v) for v in vec) == 0.0:
                continue  # no opportunity that week; tells the fit nothing
            samples[pos][0].append(vec)
            samples[pos][1].append(score_fn(stat, pos))

    models: Dict[str, Dict[str, Any]] = {}
    for pos, (rows, targets) in samples.items():
        if len(rows) < MIN_ROWS_PER_POSITION:
            continue
        coefficients = solve_ols(rows, targets)
        if coefficients is None:
            continue
        models[pos] = {
            "coefficients": coefficients,
            "features": FEATURES[pos],
            "rows": len(rows),
        }
    return models


def expected_points(stat: Dict[str, Any], model: Dict[str, Any]) -> float:
    """Apply a fitted position model to one player-week's opportunity."""
    coefficients = model["coefficients"]
    value = coefficients[0]
    for coef, feature in zip(coefficients[1:], model["features"]):
        value += coef * float(stat.get(feature) or 0.0)
    return value


def build_report(
    weekly_blobs: Dict[int, Dict[str, Any]],
    players: Dict[str, Any],
    score_fn: Callable[[Dict[str, Any], str], float],
    *,
    positions: Iterable[str] = ("QB", "RB", "WR", "TE"),
    min_games: int = 2,
    min_opportunity_per_game: float = 3.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Score every player against his position's opportunity model.

    Returns (rows, models). Each row carries per-game actual, expected, and the
    residual between them, plus the raw opportunity that drove the expectation
    so a caller can show its work rather than asking to be trusted.

    min_opportunity_per_game filters out players whose expected line rests on
    almost no touches — a residual computed from one target a week is noise
    dressed as a signal.
    """
    models = fit_models(weekly_blobs, players, score_fn, positions)
    if not models:
        return [], {}

    tallies: Dict[str, Dict[str, Any]] = {}
    for stats_by_pid in weekly_blobs.values():
        for pid, stat in (stats_by_pid or {}).items():
            player = players.get(pid) or {}
            pos = player.get("position")
            model = models.get(pos)
            if model is None:
                continue
            vec = _feature_vector(stat, model["features"])
            if sum(abs(v) for v in vec) == 0.0:
                continue

            entry = tallies.setdefault(
                pid,
                {
                    "pid": pid,
                    "player": player,
                    "position": pos,
                    "team": player.get("team"),
                    "games": 0,
                    "actual": 0.0,
                    "expected": 0.0,
                    "opportunity": {f: 0.0 for f in model["features"]},
                },
            )
            entry["games"] += 1
            entry["actual"] += score_fn(stat, pos)
            entry["expected"] += expected_points(stat, model)
            for feature, value in zip(model["features"], vec):
                entry["opportunity"][feature] += value

    rows: List[Dict[str, Any]] = []
    for entry in tallies.values():
        games = entry["games"]
        if games < min_games:
            continue
        # "Opportunity" for the volume gate is the primary touch count: the
        # first feature of each position group is always attempts or targets.
        primary = entry["opportunity"][models[entry["position"]]["features"][0]]
        if entry["position"] in ("RB",):
            primary += entry["opportunity"].get("rec_tgt", 0.0)
        if primary / games < min_opportunity_per_game:
            continue

        actual_pg = entry["actual"] / games
        expected_pg = entry["expected"] / games
        rows.append({
            **entry,
            "actual_per_game": actual_pg,
            "expected_per_game": expected_pg,
            "residual_per_game": actual_pg - expected_pg,
            "opportunity_per_game": {
                f: v / games for f, v in entry["opportunity"].items()
            },
        })

    rows.sort(key=lambda r: r["residual_per_game"])
    return rows, models
