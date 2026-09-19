"""Time and pace formatting shared across the bot, the GPX pipeline and the
prediction models."""
from typing import Optional

MILE_M = 1609.344


def fmt_time(seconds: Optional[float]) -> str:
    """Format a duration (seconds) as M:SS or H:MM:SS."""
    if seconds is None:
        return "—"
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def fmt_pace_mi(s_per_mile: Optional[float]) -> str:
    if s_per_mile is None:
        return "—"
    m, sec = divmod(round(s_per_mile), 60)
    return f"{m}:{sec:02d}/mi"


def fmt_pace_km(s_per_km: Optional[float]) -> str:
    if s_per_km is None:
        return "—"
    m, sec = divmod(round(s_per_km), 60)
    return f"{m}:{sec:02d}/km"


def pace_per_mile(seconds: float, meters: float) -> float:
    """Seconds per mile for an effort of ``seconds`` over ``meters``."""
    return seconds / meters * MILE_M


def parse_time(value: str) -> float:
    """Parse ``M:SS``, ``MM:SS`` or ``H:MM:SS`` into seconds.

    Raises ``ValueError`` with a user-facing message on bad input.
    """
    parts = value.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"`{value}` isn't a valid time — use M:SS or H:MM:SS.")
    if len(parts) == 2:
        m, s = parts
        if not (0 <= s < 60):
            raise ValueError(f"Seconds must be 0–59, got `{s}`.")
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = parts
        if not (0 <= s < 60) or not (0 <= m < 60):
            raise ValueError("Use H:MM:SS format, e.g. `1:02:30`.")
        return h * 3600 + m * 60 + s
    raise ValueError(f"`{value}` isn't a valid time — use M:SS or H:MM:SS.")
