import datetime
import logging
import os

from google.genai import errors as genai_errors

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database
from gpx_processor import process_gpx, get_run_stats
from gemini_insights import get_insights

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("leaderboard")

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in the environment / .env file.")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set in the environment / .env file.")

SUMMARY_CHANNEL_ID = 1488671121418092594

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
db = Database()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_time(seconds: float) -> str:
    """Format a duration (seconds) as M:SS or H:MM:SS."""
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


MEDALS = ["🥇", "🥈", "🥉"]


def rank_str(i: int) -> str:
    return MEDALS[i] if i < 3 else f"{i + 1}."


def parse_time(value: str) -> float:
    """
    Parse a time string into seconds.
    Accepts M:SS, MM:SS, H:MM:SS.
    Raises ValueError with a user-friendly message on bad input.
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
            raise ValueError(f"Use H:MM:SS format, e.g. `1:02:30`.")
        return h * 3600 + m * 60 + s
    raise ValueError(f"`{value}` isn't a valid time — use M:SS or H:MM:SS.")


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    await db.init()
    synced = await bot.tree.sync()
    log.info("Logged in as %s  |  %d slash commands synced.", bot.user, len(synced))
    if not weekly_summary.is_running():
        weekly_summary.start()


# ---------------------------------------------------------------------------
# /upload
# ---------------------------------------------------------------------------

@bot.tree.command(name="upload", description="Upload a GPX file to record a run.")
@app_commands.describe(
    gpx_file="GPX file exported from Strava or any GPS app.",
    runner="Who ran this? Tag someone else if you're uploading on their behalf.",
    insights="Ask Gemini for a coaching analysis of this run (requires GEMINI_API_KEY).",
)
async def upload(
    interaction: discord.Interaction,
    gpx_file: discord.Attachment,
    runner: discord.Member = None,
    insights: bool = False,
):
    await interaction.response.defer()

    target = runner or interaction.user

    if not gpx_file.filename.lower().endswith(".gpx"):
        await interaction.followup.send("Please upload a `.gpx` file.")
        return

    try:
        raw = await gpx_file.read()
        stats = get_run_stats(raw)
    except Exception:
        log.exception("Unhandled error:")
        await interaction.followup.send(
            "Failed to parse the GPX file. Make sure it contains GPS track data with timestamps."
        )
        return

    if not stats:
        await interaction.followup.send(
            "No valid timed GPS segments found. "
            "The run may be too short, or the GPX file is missing timestamps."
        )
        return

    tag = await db.add_run(
        discord_user_id=str(target.id),
        discord_username=target.display_name,
        run_date=stats.get("date"),
        mile_time=stats.get("mile_time"),
        fivek_time=stats.get("fivek_time"),
        filename=gpx_file.filename,
        stats=stats,
    )

    embed = discord.Embed(
        title=f"Run recorded for {target.display_name}!",
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    mile_t = stats.get("mile_time")
    fivek_t = stats.get("fivek_time")
    embed.add_field(
        name="Fastest Mile",
        value=fmt_time(mile_t) if mile_t else "N/A — run too short",
        inline=True,
    )
    embed.add_field(
        name="Fastest 5K",
        value=fmt_time(fivek_t) if fivek_t else "N/A — run too short",
        inline=True,
    )
    if stats.get("date"):
        embed.add_field(name="Date", value=stats["date"], inline=True)

    footer = f"Tag: {tag}  ·  Use /insights tag:{tag} to analyse"
    if runner and runner != interaction.user:
        footer += f"  ·  Uploaded by {interaction.user.display_name}"
    embed.set_footer(text=footer)

    await interaction.followup.send(embed=embed)

    # Optionally fire off a Gemini analysis in the same channel
    if insights:
        await _send_insights(interaction, stats, target)


# ---------------------------------------------------------------------------
# /insights
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="insights",
    description="Get a Gemini AI coaching analysis of a run.",
)
@app_commands.describe(
    tag="Run tag shown in /runs or after /upload (e.g. AB3KQ).",
    gpx_file="Upload a GPX file directly instead of using a stored run.",
    runner="Who ran this? Only needed when uploading a GPX file.",
)
async def insights_cmd(
    interaction: discord.Interaction,
    tag: str = None,
    gpx_file: discord.Attachment = None,
    runner: discord.Member = None,
):
    await interaction.response.defer()

    if tag is not None:
        row = await db.get_run_by_tag(tag)
        if not row:
            await interaction.followup.send(f"No run found with tag `{tag.upper()}`.")
            return
        if not row["stats"]:
            if row.get("filename") == "manual entry":
                await interaction.followup.send(
                    f"Run `{tag.upper()}` is a manual time entry — there's no GPS data to analyse."
                )
            else:
                await interaction.followup.send(
                    f"Run `{tag.upper()}` was uploaded before AI insights were supported. "
                    "Re-upload the GPX file to get an analysis."
                )
            return
        member = interaction.guild.get_member(int(row["user_id"])) if interaction.guild else None
        target_name = member.display_name if member else row["username"]
        target = member or interaction.user
        await _send_insights(interaction, row["stats"], target, override_name=target_name)
        return

    if gpx_file is None:
        await interaction.followup.send(
            "Provide a run `tag` (from `/runs`) or attach a `gpx_file`."
        )
        return

    if not gpx_file.filename.lower().endswith(".gpx"):
        await interaction.followup.send("Please upload a `.gpx` file.")
        return

    try:
        raw = await gpx_file.read()
        stats = get_run_stats(raw)
    except Exception:
        log.exception("Unhandled error:")
        await interaction.followup.send("Failed to parse the GPX file.")
        return

    if not stats:
        await interaction.followup.send("No valid GPS data found in this file.")
        return

    target = runner or interaction.user
    await _send_insights(interaction, stats, target)


# ---------------------------------------------------------------------------
# Shared insights helper
# ---------------------------------------------------------------------------

async def _send_insights(
    interaction: discord.Interaction,
    stats: dict,
    target: discord.Member,
    override_name: str = None,
) -> None:
    """Call Gemini and post the coaching embed. Works from both commands."""
    display_name = override_name or target.display_name
    thinking = await interaction.followup.send("Asking Gemini for insights… 🤔")

    try:
        analysis = await get_insights(stats, display_name, GEMINI_API_KEY)
    except genai_errors.ServerError as e:
        if e.code == 503:
            await thinking.edit(content="Gemini is overloaded right now — try again in a moment.")
        else:
            log.exception("Gemini server error:")
            await thinking.edit(content="Gemini returned a server error. Check the logs.")
        return
    except Exception:
        log.exception("Unhandled error:")
        await thinking.edit(content="Gemini analysis failed. Check the logs.")
        return

    embed = discord.Embed(
        title=f"AI Run Analysis — {display_name}",
        description=analysis[:4096],
        color=discord.Color.purple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    # Attach a compact stats footer so the numbers are visible alongside the prose
    footer_parts = []
    if stats.get("total_dist_km"):
        footer_parts.append(f"{stats['total_dist_km']:.2f} km")
    if stats.get("moving_time_s"):
        footer_parts.append(fmt_time(stats["moving_time_s"]))
    if stats.get("avg_pace_s_km"):
        m, s = divmod(round(stats["avg_pace_s_km"]), 60)
        footer_parts.append(f"avg {m}:{s:02d}/km")
    if footer_parts:
        embed.set_footer(text="  ·  ".join(footer_parts))

    await thinking.edit(content=None, embed=embed)


# ---------------------------------------------------------------------------
# /leaderboard
# ---------------------------------------------------------------------------

@bot.tree.command(name="leaderboard", description="Show the fastest times leaderboard.")
@app_commands.describe(event="Mile or 5K leaderboard.")
@app_commands.choices(
    event=[
        app_commands.Choice(name="Mile", value="mile"),
        app_commands.Choice(name="5K", value="5k"),
    ]
)
async def leaderboard(interaction: discord.Interaction, event: str = "mile"):
    rows = await db.get_leaderboard(event)

    if not rows:
        await interaction.response.send_message(
            "No times on the board yet. Upload a run with `/upload`!"
        )
        return

    label = "Mile" if event == "mile" else "5K"
    embed = discord.Embed(
        title=f"🏃 Fastest {label} Times",
        color=discord.Color.orange(),
    )

    lines = [
        f"{rank_str(i)}  **{username}** — `{fmt_time(t)}`"
        for i, (username, t) in enumerate(rows)
    ]
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /pb  (personal bests)
# ---------------------------------------------------------------------------

@bot.tree.command(name="pb", description="Show personal bests for a runner.")
@app_commands.describe(runner="Whose PBs to look up (defaults to you).")
async def pb(interaction: discord.Interaction, runner: discord.Member = None):
    target = runner or interaction.user
    bests = await db.get_personal_bests(str(target.id))

    if not bests:
        await interaction.response.send_message(
            f"No runs recorded for **{target.display_name}** yet."
        )
        return

    embed = discord.Embed(
        title=f"Personal Bests — {target.display_name}",
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    if bests["mile_time"]:
        embed.add_field(name="🏃 Fastest Mile", value=f"`{fmt_time(bests['mile_time'])}`", inline=True)
    if bests["fivek_time"]:
        embed.add_field(name="🏅 Fastest 5K", value=f"`{fmt_time(bests['fivek_time'])}`", inline=True)

    embed.add_field(name="Runs logged", value=str(bests["run_count"]), inline=True)
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /runs  (recent run history)
# ---------------------------------------------------------------------------

@bot.tree.command(name="runs", description="Show recent runs for a runner.")
@app_commands.describe(runner="Whose runs to show (defaults to you).")
async def runs(interaction: discord.Interaction, runner: discord.Member = None):
    target = runner or interaction.user
    recent = await db.get_recent_runs(str(target.id))

    if not recent:
        await interaction.response.send_message(
            f"No runs recorded for **{target.display_name}** yet."
        )
        return

    embed = discord.Embed(
        title=f"Recent Runs — {target.display_name}",
        color=discord.Color.blurple(),
    )

    lines = []
    for tag, date, mile, fivek, fname, gps_verified in recent:
        parts = []
        if mile:
            parts.append(f"Mile: `{fmt_time(mile)}`")
        if fivek:
            parts.append(f"5K: `{fmt_time(fivek)}`")
        gps_badge = " 📍" if gps_verified else ""
        time_str = "  ·  ".join(parts) or "No timed segments"
        lines.append(f"**`{tag}`** {date or fname or 'Unknown date'}{gps_badge} — {time_str}")
    embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /logtime
# ---------------------------------------------------------------------------

@bot.tree.command(name="logtime", description="Manually log a mile and/or 5K time without a GPX file.")
@app_commands.describe(
    mile="Fastest mile time, e.g. 7:30",
    fivek="Fastest 5K time, e.g. 25:00",
    runner="Who ran this? Defaults to you.",
    date="Date of the run (YYYY-MM-DD). Defaults to today.",
)
async def logtime(
    interaction: discord.Interaction,
    mile: str = None,
    fivek: str = None,
    runner: discord.Member = None,
    date: str = None,
):
    if mile is None and fivek is None:
        await interaction.response.send_message(
            "Provide at least one time — `mile`, `fivek`, or both.", ephemeral=True
        )
        return

    mile_s = fivek_s = None
    try:
        if mile:
            mile_s = parse_time(mile)
        if fivek:
            fivek_s = parse_time(fivek)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    target = runner or interaction.user

    # Basic sanity checks
    if mile_s is not None and not (60 <= mile_s <= 3600):
        await interaction.response.send_message(
            "That mile time doesn't look right (must be between 1:00 and 60:00).", ephemeral=True
        )
        return
    if fivek_s is not None and not (600 <= fivek_s <= 7200):
        await interaction.response.send_message(
            "That 5K time doesn't look right (must be between 10:00 and 2:00:00).", ephemeral=True
        )
        return

    run_date = date or discord.utils.utcnow().strftime("%Y-%m-%d")

    tag = await db.add_run(
        discord_user_id=str(target.id),
        discord_username=target.display_name,
        run_date=run_date,
        mile_time=mile_s,
        fivek_time=fivek_s,
        filename="manual entry",
        stats=None,
    )

    embed = discord.Embed(
        title=f"Time logged for {target.display_name}!",
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    if mile_s:
        embed.add_field(name="Mile", value=f"`{fmt_time(mile_s)}`", inline=True)
    if fivek_s:
        embed.add_field(name="5K", value=f"`{fmt_time(fivek_s)}`", inline=True)
    embed.add_field(name="Date", value=run_date, inline=True)

    footer = f"Tag: {tag}"
    if runner and runner != interaction.user:
        footer += f"  ·  Logged by {interaction.user.display_name}"
    embed.set_footer(text=footer)

    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /remove
# ---------------------------------------------------------------------------

@bot.tree.command(name="remove", description="Remove one of your runs by its tag.")
@app_commands.describe(tag="The run tag to delete (shown in /runs).")
async def remove(interaction: discord.Interaction, tag: str):
    result = await db.delete_run_by_tag(tag)
    if result == "deleted":
        await interaction.response.send_message(
            f"Run `{tag.upper()}` has been deleted.", ephemeral=True
        )
    else:  # not_found
        await interaction.response.send_message(
            f"No run found with tag `{tag.upper()}`.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Weekly summary (posts every Sunday at 09:00 UTC)
# ---------------------------------------------------------------------------

async def _build_weekly_summary_embed() -> discord.Embed | None:
    """Build the weekly summary embed. Returns None if there are no runs."""
    rows = await db.get_weekly_runs()
    if not rows:
        return None

    # Group by user
    runners: dict[str, dict] = {}
    for r in rows:
        uid = r["user_id"]
        if uid not in runners:
            runners[uid] = {
                "username": r["username"],
                "run_count": 0,
                "best_mile": None,
                "best_fivek": None,
            }
        entry = runners[uid]
        entry["run_count"] += 1
        if r["mile_time"] and (entry["best_mile"] is None or r["mile_time"] < entry["best_mile"]):
            entry["best_mile"] = r["mile_time"]
        if r["fivek_time"] and (entry["best_fivek"] is None or r["fivek_time"] < entry["best_fivek"]):
            entry["best_fivek"] = r["fivek_time"]

    total_runs = len(rows)
    total_runners = len(runners)

    embed = discord.Embed(
        title="Weekly Running Wrap-up 🏃",
        description=(
            f"Here's what our crew got up to this week — "
            f"**{total_runners} runner{'s' if total_runners != 1 else ''}**, "
            f"**{total_runs} run{'s' if total_runs != 1 else ''}** logged. "
            "Every mile counts! 💪"
        ),
        color=discord.Color.green(),
    )

    lines = []
    for entry in sorted(runners.values(), key=lambda e: e["run_count"], reverse=True):
        run_word = "run" if entry["run_count"] == 1 else "runs"
        parts = [f"**{entry['username']}** — {entry['run_count']} {run_word}"]
        times = []
        if entry["best_mile"]:
            times.append(f"mile `{fmt_time(entry['best_mile'])}`")
        if entry["best_fivek"]:
            times.append(f"5K `{fmt_time(entry['best_fivek'])}`")
        if times:
            parts.append("(" + ", ".join(times) + ")")
        lines.append(" ".join(parts))

    embed.add_field(name="Who ran this week", value="\n".join(lines), inline=False)

    # Shoutouts
    shoutouts = []
    most_active = max(runners.values(), key=lambda e: e["run_count"])
    if most_active["run_count"] > 1:
        shoutouts.append(
            f"**Most dedicated:** {most_active['username']} with {most_active['run_count']} runs — consistency wins! 🔥"
        )
    fastest_mile = min(
        (e for e in runners.values() if e["best_mile"]),
        key=lambda e: e["best_mile"],
        default=None,
    )
    if fastest_mile:
        shoutouts.append(
            f"**Fastest mile this week:** {fastest_mile['username']} — `{fmt_time(fastest_mile['best_mile'])}` 🥇"
        )
    fastest_fivek = min(
        (e for e in runners.values() if e["best_fivek"]),
        key=lambda e: e["best_fivek"],
        default=None,
    )
    if fastest_fivek:
        shoutouts.append(
            f"**Fastest 5K this week:** {fastest_fivek['username']} — `{fmt_time(fastest_fivek['best_fivek'])}` 🏅"
        )

    if shoutouts:
        embed.add_field(name="Shoutouts", value="\n".join(shoutouts), inline=False)

    embed.set_footer(text="Keep it up everyone — see you next Sunday! 🌅")
    return embed


@tasks.loop(time=datetime.time(hour=9, minute=0, tzinfo=datetime.timezone.utc))
async def weekly_summary():
    if datetime.datetime.now(datetime.timezone.utc).weekday() != 6:  # 6 = Sunday
        return

    channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    if channel is None:
        log.warning("weekly_summary: channel %d not found.", SUMMARY_CHANNEL_ID)
        return

    embed = await _build_weekly_summary_embed()
    if embed:
        await channel.send(embed=embed)
    else:
        await channel.send("No runs logged this week — lace up and get out there! 👟")


# ---------------------------------------------------------------------------
# /weekly_summary  (test command)
# ---------------------------------------------------------------------------

@bot.tree.command(name="weekly_summary", description="Preview the weekly run summary for this channel.")
async def weekly_summary_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = await _build_weekly_summary_embed()
    if embed:
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("No runs logged in the past 7 days.")



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot.run(TOKEN)
