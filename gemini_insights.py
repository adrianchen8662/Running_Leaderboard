"""
Gemini-powered run analysis using the google-genai SDK.
"""
import os
from typing import Dict, Any

from google import genai

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_time(s: float) -> str:
    s = round(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_pace_mi(s_per_mile: float) -> str:
    m, sec = divmod(round(s_per_mile), 60)
    return f"{m}:{sec:02d}/mi"


def _fmt_pace_km(s_per_km: float) -> str:
    m, sec = divmod(round(s_per_km), 60)
    return f"{m}:{sec:02d}/km"


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(stats: Dict[str, Any], runner_name: str) -> str:
    lines: list[str] = [
        f"You are a friendly, knowledgeable running coach. Analyse the following "
        f"GPS run data for **{runner_name}** and give a concise, personalised "
        f"coaching response formatted for Discord (use **bold** for emphasis, "
        f"hyphens for lists — no # headers, no triple-backtick blocks). "
        f"Keep the total response under 2 000 characters.",
        "",
        "---",
        "**RUN DATA**",
    ]

    if stats.get("date"):
        lines.append(f"Date: {stats['date']}")

    dk = stats.get("total_dist_km")
    dm = stats.get("total_dist_miles")
    if dk:
        lines.append(f"Distance: {dk:.2f} km / {dm:.2f} mi")

    tt = stats.get("total_time_s")
    mv = stats.get("moving_time_s")
    st = stats.get("stopped_time_s", 0)
    if tt:
        lines.append(f"Total time: {_fmt_time(tt)}")
    if mv and st and st > 30:
        lines.append(f"Moving time: {_fmt_time(mv)}  (stopped {_fmt_time(st)})")

    pk = stats.get("avg_pace_s_km")
    pm = stats.get("avg_pace_s_mi")
    if pk and pm:
        lines.append(f"Avg pace: {_fmt_pace_km(pk)} / {_fmt_pace_mi(pm)}")

    mile_t = stats.get("mile_time")
    fivek_t = stats.get("fivek_time")
    if mile_t or fivek_t:
        lines += ["", "**Best Segments (fastest contiguous)**"]
        if mile_t:
            lines.append(f"- Fastest mile: {_fmt_time(mile_t)}  ({_fmt_pace_mi(mile_t)} pace)")
        if fivek_t:
            lines.append(f"- Fastest 5 K:  {_fmt_time(fivek_t)}  ({_fmt_pace_km(fivek_t / 5)} pace)")

    splits = stats.get("mile_splits_s", [])
    if splits:
        lines += ["", "**Mile Splits**"]
        for i, s in enumerate(splits, 1):
            lines.append(f"- Mile {i}: {_fmt_time(s)}  ({_fmt_pace_mi(s)})")
        stdev = stats.get("pace_stdev_s")
        delta = stats.get("split_delta_s")
        if stdev is not None:
            lines.append(f"Split consistency: ±{stdev:.0f}s std dev")
        if delta is not None:
            if delta < -10:
                lines.append(f"Pacing trend: **negative split** (last mile {abs(delta):.0f}s faster than first)")
            elif delta > 10:
                lines.append(f"Pacing trend: **positive split** (last mile {delta:.0f}s slower than first)")
            else:
                lines.append("Pacing trend: **even split**")

    gain = stats.get("elev_gain_m")
    loss = stats.get("elev_loss_m")
    if gain is not None:
        lines += ["", "**Elevation**"]
        lines.append(f"- Gain: {gain:.0f} m  |  Loss: {loss:.0f} m")
        mn, mx = stats.get("elev_min_m"), stats.get("elev_max_m")
        if mn is not None:
            lines.append(f"- Range: {mn:.0f} m – {mx:.0f} m")

    avg_hr = stats.get("avg_hr")
    max_hr = stats.get("max_hr")
    if avg_hr:
        lines += ["", "**Heart Rate**"]
        lines.append(f"- Avg: {avg_hr:.0f} bpm  |  Max: {max_hr:.0f} bpm")
        if avg_hr < 130:
            lines.append("- Effort zone: easy / recovery")
        elif avg_hr < 155:
            lines.append("- Effort zone: aerobic / base building")
        elif avg_hr < 170:
            lines.append("- Effort zone: tempo / threshold")
        else:
            lines.append("- Effort zone: hard / VO2max")

    cad = stats.get("avg_cadence_spm")
    if cad:
        lines += ["", "**Cadence**"]
        lines.append(f"- Avg: {cad:.0f} spm")
        if cad < 160:
            lines.append("- Note: cadence is on the lower side (elite range ~170–180 spm)")
        elif cad > 185:
            lines.append("- Note: cadence is high — good for speed work")

    temp = stats.get("avg_temp_c")
    if temp is not None:
        lines.append(f"Ambient temp: {temp:.1f} °C")

    lines += [
        "",
        "---",
        "Please cover:",
        "1. A one-sentence overall verdict.",
        "2. Pacing analysis — what the splits reveal about effort distribution.",
        "3. Elevation impact on pace (if elevation data present).",
        "4. Heart rate / effort level (if HR data present).",
        "5. Cadence feedback (if cadence data present).",
        "6. Two or three specific, actionable training suggestions.",
        "",
        "Skip sections where no data is available. Be encouraging but honest.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public async function
# ---------------------------------------------------------------------------

async def get_insights(
    stats: Dict[str, Any],
    runner_name: str,
    api_key: str,
) -> str:
    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(stats, runner_name)
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return response.text.strip()
