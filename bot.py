import os
import traceback

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import Database
from gpx_processor import process_gpx, get_run_stats
from gemini_insights import get_insights

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in the environment / .env file.")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set in the environment / .env file.")

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


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    await db.init()
    synced = await bot.tree.sync()
    print(f"Logged in as {bot.user}  |  {len(synced)} slash commands synced.")


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
        print(traceback.format_exc())
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

    run_id = await db.add_run(
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

    footer = f"Run #{run_id}"
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
    run_id="ID of a previously uploaded run (shown in /runs and /upload).",
    gpx_file="GPX file to analyse instead of a stored run.",
    runner="Who ran this? Defaults to you (only needed with gpx_file).",
)
async def insights_cmd(
    interaction: discord.Interaction,
    run_id: int = None,
    gpx_file: discord.Attachment = None,
    runner: discord.Member = None,
):
    await interaction.response.defer()

    if run_id is not None:
        row = await db.get_run_by_id(run_id)
        if not row:
            await interaction.followup.send(f"No run found with ID #{run_id}.")
            return
        if not row["stats"]:
            await interaction.followup.send(
                f"Run #{run_id} was uploaded before AI insights were supported. "
                "Re-upload the GPX file to get an analysis."
            )
            return
        # Resolve display name from the guild if possible
        member = interaction.guild.get_member(int(row["user_id"])) if interaction.guild else None
        target_name = member.display_name if member else row["username"]
        target = member or interaction.user
        target.display_name  # just used for avatar below
        await _send_insights(interaction, row["stats"], target, override_name=target_name)
        return

    if gpx_file is None:
        await interaction.followup.send(
            "Provide either a `run_id` (from `/runs`) or a `gpx_file`."
        )
        return

    if not gpx_file.filename.lower().endswith(".gpx"):
        await interaction.followup.send("Please upload a `.gpx` file.")
        return

    try:
        raw = await gpx_file.read()
        stats = get_run_stats(raw)
    except Exception:
        print(traceback.format_exc())
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
    except Exception:
        print(traceback.format_exc())
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

    for run_id, date, mile, fivek, fname in recent:
        parts = []
        if mile:
            parts.append(f"Mile: `{fmt_time(mile)}`")
        if fivek:
            parts.append(f"5K: `{fmt_time(fivek)}`")
        label = f"#{run_id}  {date or fname or 'Unknown date'}"
        embed.add_field(name=label, value="  ·  ".join(parts) or "No timed segments", inline=False)

    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot.run(TOKEN)
