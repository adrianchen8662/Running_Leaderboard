import math
import statistics
from datetime import timedelta
from typing import Optional, Dict, Any, List

import gpxpy

MILE_METERS = 1609.344
FIVEK_METERS = 5000.0
TENK_METERS = 10000.0

# Ladder of distances we extract a fastest window for on every run. The
# per-runner minimum across these (the "envelope", or mean-maximal pace curve)
# is a far better basis for modelling than three isolated PRs.
BEST_EFFORT_LADDER = [
    ("400m", 400.0), ("800m", 800.0), ("1K", 1000.0), ("1600m", 1600.0),
    ("Mile", MILE_METERS), ("3K", 3000.0), ("5K", FIVEK_METERS),
    ("10K", TENK_METERS), ("15K", 15000.0), ("Half", 21097.5),
]
STOP_SPEED_MS = 0.3  # m/s — below this we treat the runner as stopped


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine(p1, p2) -> float:
    """Distance in metres between two GPS points."""
    R = 6_371_000
    lat1, lon1 = math.radians(p1.latitude), math.radians(p1.longitude)
    lat2, lon2 = math.radians(p2.latitude), math.radians(p2.longitude)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Fastest-segment (Strava-style sliding window)
# ---------------------------------------------------------------------------

def find_fastest_window(pts: list, cum: list, target_m: float):
    """
    Two-pointer sliding window over pre-built cumulative distances.
    Linear interpolation at the trailing edge for sub-second accuracy.

    Returns ``(seconds, start_idx, end_idx)`` for the fastest window, or None
    if the run is shorter than target_m. The indices let callers measure what
    was happening during the effort — heart rate above all — which is what
    separates a genuine hard effort from the least-slow stretch of a jog.
    """
    n = len(pts)
    if n < 2 or cum[-1] < target_m:
        return None

    best = float("inf")
    best_i = best_j = 0
    j = 0
    for i in range(n):
        if j <= i:
            j = i + 1
        while j < n and cum[j] - cum[i] < target_m:
            j += 1
        if j >= n:
            break

        covered = cum[j] - cum[i]
        seg_dist = cum[j] - cum[j - 1]
        if seg_dist > 0:
            overshoot = covered - target_m
            seg_secs = (pts[j].time - pts[j - 1].time).total_seconds()
            time_adj = (overshoot / seg_dist) * seg_secs
        else:
            time_adj = 0.0

        elapsed = (pts[j].time - pts[i].time).total_seconds() - time_adj
        if elapsed > 0 and elapsed < best:
            best, best_i, best_j = elapsed, i, j

    return (best, best_i, best_j) if best != float("inf") else None


def find_fastest_segment(pts: list, cum: list, target_m: float) -> Optional[float]:
    """Seconds for the fastest ``target_m`` window, or None."""
    found = find_fastest_window(pts, cum, target_m)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# GPX extension helpers (Garmin / Strava HR, cadence, temp)
# ---------------------------------------------------------------------------

def _ext_value(point, *suffixes) -> Optional[float]:
    """
    Search a track-point's XML extensions for a tag whose local name ends
    with any of `suffixes` (case-insensitive) and return its float value.
    Handles both <gpxtpx:hr> and bare <hr> style extensions.
    """
    for ext in point.extensions:
        for elem in ext.iter():
            tag = elem.tag.lower()
            # strip namespace, e.g. {http://...}hr → hr
            local = tag.rsplit("}", 1)[-1]
            for s in suffixes:
                if local == s or local.endswith(s):
                    try:
                        return float(elem.text)
                    except (TypeError, ValueError):
                        pass
    return None


def _heart_rate(point) -> Optional[float]:
    return _ext_value(point, "hr", "heartrate", "heart_rate")


def _cadence(point) -> Optional[float]:
    val = _ext_value(point, "cad", "cadence", "runcadence", "run_cadence")
    if val is None:
        return None
    # Garmin stores run cadence as single-foot steps; double it for SPM
    return val * 2 if val < 150 else val


def _temperature(point) -> Optional[float]:
    return _ext_value(point, "atemp", "temp", "temperature")


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def _build_cum(pts: list) -> list:
    cum = [0.0] * len(pts)
    for i in range(1, len(pts)):
        cum[i] = cum[i - 1] + haversine(pts[i - 1], pts[i])
    return cum


def _moving_time(pts: list, cum: list) -> float:
    moving = 0.0
    for i in range(1, len(pts)):
        dt = (pts[i].time - pts[i - 1].time).total_seconds()
        dd = cum[i] - cum[i - 1]
        if dt > 0 and dd / dt >= STOP_SPEED_MS:
            moving += dt
    return moving


def _mile_splits(pts: list, cum: list) -> List[float]:
    """
    Return elapsed seconds for each complete mile, using linear interpolation
    at mile boundaries so splits reflect real distance, not GPS sampling rate.
    """
    splits: List[float] = []
    split_start_time = pts[0].time
    split_start_dist = 0.0

    for i in range(1, len(pts)):
        while cum[i] - split_start_dist >= MILE_METERS:
            target_dist = split_start_dist + MILE_METERS
            seg_dist = cum[i] - cum[i - 1]
            if seg_dist > 0:
                frac = (target_dist - cum[i - 1]) / seg_dist
                seg_secs = (pts[i].time - pts[i - 1].time).total_seconds()
                cross_time = pts[i - 1].time + timedelta(seconds=frac * seg_secs)
            else:
                cross_time = pts[i].time

            splits.append((cross_time - split_start_time).total_seconds())
            split_start_time = cross_time
            split_start_dist = target_dist

    return splits


def _elevation_stats(pts: list) -> Dict[str, Optional[float]]:
    elevs = [p.elevation for p in pts if p.elevation is not None]
    if len(elevs) < 2:
        return {"gain_m": None, "loss_m": None, "min_m": None, "max_m": None}
    gain = sum(max(0.0, elevs[i] - elevs[i - 1]) for i in range(1, len(elevs)))
    loss = sum(max(0.0, elevs[i - 1] - elevs[i]) for i in range(1, len(elevs)))
    return {"gain_m": gain, "loss_m": loss, "min_m": min(elevs), "max_m": max(elevs)}


def _best_efforts(pts: list, cum: list, hrs: List[Optional[float]],
                  moving_s: float) -> List[Dict[str, Any]]:
    """Fastest window at every rung of the ladder, with effort signals.

    A fastest window is only a PR if the runner was actually trying. Three
    signals are recorded per rung so that judgement can be made later (and
    recalibrated) without re-parsing anything:

    ``avg_hr``      mean heart rate during the window
    ``pace_ratio``  window speed / run average speed — a hard effort inside an
                    easy run spikes; the least-slow kilometre of a jog doesn't
    ``coverage``    window distance / total run distance — near 1.0 means the
                    run *was* the effort, which is why a dedicated time trial
                    shows a flat pace_ratio and must not be read as easy
    """
    total_m = cum[-1]
    run_speed = (total_m / moving_s) if moving_s > 0 else None
    efforts = []

    for label, target_m in BEST_EFFORT_LADDER:
        found = find_fastest_window(pts, cum, target_m)
        if not found:
            continue
        secs, i, j = found
        if secs <= 0:
            continue

        seg_hrs = [h for h in hrs[i:j + 1] if h is not None]
        seg_speed = target_m / secs
        efforts.append({
            "label": label,
            "meters": target_m,
            "time_s": secs,
            "avg_hr": (sum(seg_hrs) / len(seg_hrs)) if seg_hrs else None,
            "max_hr": max(seg_hrs) if seg_hrs else None,
            "pace_ratio": (seg_speed / run_speed) if run_speed else None,
            "coverage": (target_m / total_m) if total_m else None,
        })
    return efforts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_run_stats(gpx_bytes: bytes) -> Dict[str, Any]:
    """
    Parse a GPX file and return a comprehensive stats dict suitable for both
    leaderboard storage and AI analysis.
    """
    gpx = gpxpy.parse(gpx_bytes)

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend(segment.points)
    if not points:
        for route in gpx.routes:
            points.extend(route.points)

    # Filter to only timestamped points
    pts = [p for p in points if p.time is not None]
    if len(pts) < 2:
        return {}

    cum = _build_cum(pts)
    total_dist_m = cum[-1]
    total_time_s = (pts[-1].time - pts[0].time).total_seconds()
    moving_s = _moving_time(pts, cum)

    avg_pace_s_km = (moving_s / total_dist_m * 1000) if total_dist_m > 0 else None
    avg_pace_s_mi = (moving_s / total_dist_m * MILE_METERS) if total_dist_m > 0 else None

    mile_splits = _mile_splits(pts, cum)
    elev = _elevation_stats(pts)

    hr_per_point = [_heart_rate(p) for p in pts]
    hrs  = [h for h in hr_per_point if h is not None]
    cads = [c for c in (_cadence(p)      for p in pts) if c is not None]
    temps = [t for t in (_temperature(p) for p in pts) if t is not None]

    # Per-mile pace variance — how consistent was the runner?
    pace_stdev_s = statistics.stdev(mile_splits) if len(mile_splits) >= 2 else None

    # Split trend: negative = got faster, positive = slowed down
    split_delta_s = (mile_splits[-1] - mile_splits[0]) if len(mile_splits) >= 2 else None

    return {
        # Identity
        "date": pts[0].time.strftime("%Y-%m-%d"),
        # Distance / time
        "total_dist_km":   total_dist_m / 1000,
        "total_dist_miles": total_dist_m / MILE_METERS,
        "total_time_s":    total_time_s,
        "moving_time_s":   moving_s,
        "stopped_time_s":  max(0.0, total_time_s - moving_s),
        # Pace
        "avg_pace_s_km":   avg_pace_s_km,
        "avg_pace_s_mi":   avg_pace_s_mi,
        # Full best-effort ladder (feeds the per-runner envelope)
        "best_efforts": _best_efforts(pts, cum, hr_per_point, moving_s),
        # Best segments
        "mile_time":  find_fastest_segment(pts, cum, MILE_METERS),
        "fivek_time": find_fastest_segment(pts, cum, FIVEK_METERS),
        "tenk_time":  find_fastest_segment(pts, cum, TENK_METERS),
        # Splits
        "mile_splits_s":   mile_splits,
        "pace_stdev_s":    pace_stdev_s,
        "split_delta_s":   split_delta_s,
        # Elevation
        "elev_gain_m":  elev["gain_m"],
        "elev_loss_m":  elev["loss_m"],
        "elev_min_m":   elev["min_m"],
        "elev_max_m":   elev["max_m"],
        # Heart rate
        "avg_hr":  (sum(hrs)  / len(hrs))  if hrs  else None,
        "max_hr":  max(hrs)                if hrs  else None,
        "min_hr":  min(hrs)                if hrs  else None,
        # Cadence
        "avg_cadence_spm": (sum(cads) / len(cads)) if cads else None,
        # Temperature
        "avg_temp_c": (sum(temps) / len(temps)) if temps else None,
    }


def process_gpx(gpx_bytes: bytes) -> Dict[str, Any]:
    """
    Thin wrapper kept for backward compatibility with the upload/leaderboard flow.
    Returns only the fields the database needs.
    """
    stats = get_run_stats(gpx_bytes)
    return {
        "mile_time":  stats.get("mile_time"),
        "fivek_time": stats.get("fivek_time"),
        "tenk_time":  stats.get("tenk_time"),
        "date":       stats.get("date"),
    }
