"""Race-time prediction and runner profiling.

Three models, all driven off whatever PRs a runner actually has:

  * **Riegel** — ``T2 = T1 * (D2/D1)^k``.  The textbook exponent is 1.06, but
    recreational runners fade harder than that.  When a runner has two or more
    PRs we fit *their own* k, which predicts far better than the fixed value
    (Vickers & Vertosick 2016, BMC Sports Sci Med Rehabil 8:26).
  * **VDOT** (Daniels/Gilbert) — a distance-neutral fitness score, so efforts
    at different distances can be compared directly.
  * **Critical speed** — ``D = CS*t + D'``.  CS approximates threshold pace,
    D' is the finite "extra" distance available above it.  The model is built
    for ~2-15 min efforts, so anything past a 5K stretches it.

The runner-type labels follow Greg McMillan's Speedster / Combo / Endurance
Monster split, keyed off the personal Riegel exponent: a high k means the
runner bleeds speed as the distance grows (a Speedster), a low k means they
hold on (an Endurance Monster).

Run ``python3 race_analysis.py`` to verify the math against known values.
"""
import math
from typing import Dict, List, Optional, Sequence, Tuple

MILE_M = 1609.344
FIVE_K_M = 5000.0
TEN_K_M = 10000.0

RIEGEL_K = 1.06  # textbook exponent, used when a runner has only one PR

# Distances we predict for, in the order they should be displayed.
RACE_DISTANCES: List[Tuple[str, float]] = [
    ("1K", 1000.0),
    ("Mile", MILE_M),
    ("5K", FIVE_K_M),
    ("10K", TEN_K_M),
    ("Half", 21097.5),
    ("Marathon", 42195.0),
]

# PR keys stored per run -> (display label, metres).  Order matters: shortest
# first, so callers can iterate deterministically.
PR_EVENTS: List[Tuple[str, str, float]] = [
    ("mile_time", "Mile", MILE_M),
    ("fivek_time", "5K", FIVE_K_M),
    ("tenk_time", "10K", TEN_K_M),
]


# ---------------------------------------------------------------------------
# Riegel
# ---------------------------------------------------------------------------

def riegel(t1: float, d1: float, d2: float, k: float = RIEGEL_K) -> float:
    """Predict the time for ``d2`` from a known time ``t1`` over ``d1``."""
    return t1 * (d2 / d1) ** k


def personal_exponent(t1: float, d1: float, t2: float, d2: float) -> Optional[float]:
    """Solve Riegel for k given two efforts."""
    if d1 <= 0 or d2 <= 0 or t1 <= 0 or t2 <= 0 or d1 == d2:
        return None
    return math.log(t2 / t1) / math.log(d2 / d1)


def fit_exponent(efforts: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Least-squares Riegel exponent over ``(metres, seconds)`` efforts.

    ``ln t = ln a + k * ln d``, so k is the slope of a log-log regression.
    With exactly two efforts this reduces to :func:`personal_exponent`.
    """
    pts = [(d, t) for d, t in efforts if d > 0 and t > 0]
    if len(pts) < 2:
        return None

    xs = [math.log(d) for d, _ in pts]
    ys = [math.log(t) for _, t in pts]
    x_bar = sum(xs) / len(xs)
    y_bar = sum(ys) / len(ys)
    denom = sum((x - x_bar) ** 2 for x in xs)
    if denom == 0:  # every effort at the same distance
        return None
    return sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / denom


# ---------------------------------------------------------------------------
# VDOT (Daniels / Gilbert)
# ---------------------------------------------------------------------------

def vdot(dist_m: float, t_sec: float) -> Optional[float]:
    """Daniels/Gilbert VDOT from a race distance and time."""
    if dist_m <= 0 or t_sec <= 0:
        return None
    t_min = t_sec / 60
    v = dist_m / t_min  # m/min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = (0.8 + 0.1894393 * math.exp(-0.012778 * t_min)
           + 0.2989558 * math.exp(-0.1932605 * t_min))
    if pct <= 0:
        return None
    return vo2 / pct


def vdot_equivalent_time(dist_m: float, target_vdot: float) -> float:
    """Time at ``dist_m`` that yields ``target_vdot`` (bisection)."""
    lo, hi = 60.0, 30000.0
    mid = hi
    for _ in range(100):
        mid = (lo + hi) / 2
        if vdot(dist_m, mid) > target_vdot:
            lo = mid
        else:
            hi = mid
    return mid


# ---------------------------------------------------------------------------
# Critical speed
# ---------------------------------------------------------------------------

def critical_speed(
    efforts: Sequence[Tuple[float, float]]
) -> Tuple[Optional[float], Optional[float]]:
    """Fit ``D = CS*t + D'``.  Returns ``(CS in m/s, D' in metres)``.

    Least squares over all efforts; with two efforts it is the exact line
    through both.  Returns ``(None, None)`` if the fit is degenerate or
    physically meaningless (non-positive CS, or a negative distance reserve).
    """
    pts = [(d, t) for d, t in efforts if d > 0 and t > 0]
    if len(pts) < 2:
        return None, None

    ts = [t for _, t in pts]
    ds = [d for d, _ in pts]
    t_bar = sum(ts) / len(ts)
    d_bar = sum(ds) / len(ds)
    denom = sum((t - t_bar) ** 2 for t in ts)
    if denom == 0:
        return None, None

    cs = sum((t - t_bar) * (d - d_bar) for d, t in pts) / denom
    d_prime = d_bar - cs * t_bar
    if cs <= 0 or d_prime < 0:
        return None, None
    return cs, d_prime


# ---------------------------------------------------------------------------
# McMillan runner type
# ---------------------------------------------------------------------------

# (exclusive upper bound on k, label, emoji, blurb)
_RUNNER_TYPES: List[Tuple[float, str, str, str]] = [
    (1.12, "Endurance Monster", "🐂",
     "You hold pace as the distance climbs. Longer races are where you shine — "
     "your ceiling is raw top-end speed, so short intervals and strides pay off most."),
    (1.18, "Combo Runner", "⚖️",
     "You're balanced across the range — no glaring strength or weakness. "
     "You can race anything from the mile to the 10K competitively; mix speed "
     "and tempo work rather than specialising."),
    (float("inf"), "Speedster", "⚡",
     "You're quick over short distances and fade as they get longer. Your speed "
     "is banked — the gains are in aerobic base: easy volume and tempo runs to "
     "push your threshold up toward your speed."),
]


def classify_runner(k: Optional[float]) -> Optional[Dict[str, str]]:
    """Map a personal Riegel exponent onto a McMillan runner type."""
    if k is None:
        return None
    for upper, label, emoji, blurb in _RUNNER_TYPES:
        if k < upper:
            return {"label": label, "emoji": emoji, "blurb": blurb}
    return None


# ---------------------------------------------------------------------------
# Profile assembly
# ---------------------------------------------------------------------------

def build_profile(prs: Dict[str, Optional[float]]) -> Dict:
    """Turn a runner's PRs into predictions, fitness scores and a type.

    ``prs`` maps the keys in :data:`PR_EVENTS` (``mile_time``, ``fivek_time``,
    ``tenk_time``) to seconds; missing or ``None`` entries are ignored.
    """
    efforts = [
        {"key": key, "label": label, "meters": meters, "time": prs[key]}
        for key, label, meters in PR_EVENTS
        if prs.get(key)
    ]

    profile: Dict = {
        "efforts": efforts,
        "k": None,
        "k_is_personal": False,
        "predictions": [],
        "vdot": None,
        "cs_pace_s_mi": None,
        "d_prime_m": None,
        "runner_type": None,
        "riegel_gap_pct": None,
        "riegel_gap_span": None,
    }
    if not efforts:
        return profile

    pairs = [(e["meters"], e["time"]) for e in efforts]
    k = fit_exponent(pairs)
    profile["k_is_personal"] = k is not None
    profile["k"] = k if k is not None else RIEGEL_K

    # Fitness score: the runner's single best effort by VDOT.
    vdots = [v for v in (vdot(m, t) for m, t in pairs) if v is not None]
    profile["vdot"] = max(vdots) if vdots else None

    if k is not None:
        profile["runner_type"] = classify_runner(k)
        # The CS model is built for ~2-15 min efforts, and a far-out-of-domain
        # effort (a 10K, say) drags the fitted line flat.  Use the two shortest
        # PRs, which sit closest to where the model actually holds.
        cs, d_prime = critical_speed(pairs[:2])
        if cs:
            profile["cs_pace_s_mi"] = MILE_M / cs
            profile["d_prime_m"] = d_prime

        # How far off the textbook exponent is this runner, measured over
        # their own shortest -> longest span?
        (d_short, t_short), (d_long, t_long) = pairs[0], pairs[-1]
        if d_long > d_short:
            textbook = riegel(t_short, d_short, d_long, RIEGEL_K)
            profile["riegel_gap_pct"] = (t_long / textbook - 1) * 100
            # The gap compounds with the span it's measured over, so callers
            # must show which span it refers to.
            profile["riegel_gap_span"] = (efforts[0]["label"], efforts[-1]["label"])

    profile["predictions"] = predict_all(efforts, profile["k"])
    return profile


def predict_all(efforts: Sequence[Dict], k: float) -> List[Dict]:
    """Predict every race distance, anchored on the nearest PR.

    Riegel degrades the further you extrapolate, so each target is predicted
    from whichever PR is closest to it in log-distance.
    """
    predictions = []
    by_meters = {e["meters"]: e for e in efforts}

    for label, meters in RACE_DISTANCES:
        anchor = min(efforts, key=lambda e: abs(math.log(meters / e["meters"])))
        actual = by_meters.get(meters)
        ratio = meters / anchor["meters"]
        predictions.append({
            "label": label,
            "meters": meters,
            "time": riegel(anchor["time"], anchor["meters"], meters, k),
            "anchor": anchor["label"],
            "actual": actual["time"] if actual else None,
            # Riegel is only trustworthy within roughly a 4x extrapolation of
            # the effort it's anchored on.
            "reliable": 0.25 <= ratio <= 4.0,
        })
    return predictions


# ---------------------------------------------------------------------------
# Verification harness — checks the models against the numbers worked out in
# the original analysis (see context-transfer/CLAUDE.md).
# ---------------------------------------------------------------------------

def _main() -> None:
    from formatting import fmt_time, parse_time

    leaderboard = {
        "#1 scooby doo hater": ("6:31", "24:01", None),
        "Mynorca": ("6:39", "26:52", "1:01:58"),
        "Kuwurisu": ("7:17", "27:09", None),
        "/c c": ("8:41", "38:09", None),
    }

    header = (f"{'Runner':<20}{'Mile':>6}{'5K':>7}{'Riegel5K':>10}{'Off':>6}"
              f"{'k':>6}{'VDOT':>7}{'CS/mi':>8}{'D-prime':>9}  Type")
    print(header)
    print("-" * len(header))

    for name, (mile, fivek, tenk) in leaderboard.items():
        prs = {
            "mile_time": parse_time(mile),
            "fivek_time": parse_time(fivek),
            "tenk_time": parse_time(tenk) if tenk else None,
        }
        p = build_profile(prs)
        m, f = prs["mile_time"], prs["fivek_time"]
        pred = riegel(m, MILE_M, FIVE_K_M)
        cs_pace = fmt_time(p["cs_pace_s_mi"]) if p["cs_pace_s_mi"] else "-"
        d_prime = f"{p['d_prime_m']:.0f}m" if p["d_prime_m"] else "-"
        print(f"{name:<20}{mile:>6}{fivek:>7}{fmt_time(pred):>10}"
              f"{(f / pred - 1) * 100:>5.0f}%{p['k']:>6.2f}{p['vdot']:>7.1f}"
              f"{cs_pace:>8}{d_prime:>9}  {p['runner_type']['label']}")

    # Mynorca's 10K: personal exponent vs the textbook one.
    five_k, ten_k = parse_time("26:52"), parse_time("1:01:58")
    k_mile_5k = personal_exponent(parse_time("6:39"), MILE_M, five_k, FIVE_K_M)
    print("\nMynorca 10K check (actual 1:01:58):")
    print(f"  Riegel k=1.06   -> {fmt_time(riegel(five_k, FIVE_K_M, TEN_K_M))}")
    print(f"  personal k={k_mile_5k:.2f} -> "
          f"{fmt_time(riegel(five_k, FIVE_K_M, TEN_K_M, k_mile_5k))}")
    print("  actual 5K->10K exponent = "
          f"{personal_exponent(five_k, FIVE_K_M, ten_k, TEN_K_M):.2f}")


if __name__ == "__main__":
    _main()
