import asyncio
import datetime
import logging
import os

from google.genai import errors as genai_errors

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import race_analysis
import database
from database import Database, EVENT_COLUMNS
from formatting import fmt_time, fmt_pace_mi, pace_per_mile, parse_time
from gpx_processor import get_run_stats
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
# Display helpers
# ---------------------------------------------------------------------------

MEDALS = ["🥇", "🥈", "🥉"]

# Event key -> (display label, emoji, metres).  Drives the leaderboard, the
# upload/logtime embeds and the weekly summary, so a new distance only needs
# adding here and in database.EVENT_COLUMNS.
EVENTS = {
    "mile": ("Mile", "🏃", race_analysis.MILE_M),
    "5k":   ("5K",   "🏅", race_analysis.FIVE_K_M),
    "10k":  ("10K",  "🎽", race_analysis.TEN_K_M),
}

RUNS_PER_PAGE = 8


def rank_str(i: int) -> str:
    return MEDALS[i] if i < 3 else f"{i + 1}."


def _pr_fields(embed: discord.Embed, bests: dict) -> bool:
    """Add a field for every tracked distance. Distances the runner hasn't
    logged still get a slot — otherwise a new event looks like it's missing
    from the bot rather than from their history. Returns True if any PR
    was actually set."""
    added = False
    for key, (label, emoji, meters) in EVENTS.items():
        secs = bests.get(EVENT_COLUMNS[key])
        if secs:
            value = f"`{fmt_time(secs)}`\n{fmt_pace_mi(pace_per_mile(secs, meters))}"
            added = True
        else:
            value = "`—`\n*not logged yet*"
        embed.add_field(name=f"{emoji} {label}", value=value, inline=True)
    return added


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

async def _maybe_send_missed_summary() -> None:
    """Send the weekly summary if it was missed while the bot was offline."""
    now = datetime.datetime.now(datetime.timezone.utc)
    # Find the most recent Sunday at 09:00 UTC
    days_since_sunday = (now.weekday() - 6) % 7
    last_sunday = (now - datetime.timedelta(days=days_since_sunday)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    if last_sunday > now:
        last_sunday -= datetime.timedelta(weeks=1)

    last_sent_str = await db.get_state("last_weekly_summary_sent")
    if last_sent_str:
        last_sent = datetime.datetime.fromisoformat(last_sent_str)
        if last_sent >= last_sunday:
            return  # already sent for this week

    # Bot missed the Sunday window — send now
    try:
        channel = bot.get_channel(SUMMARY_CHANNEL_ID) or await bot.fetch_channel(SUMMARY_CHANNEL_ID)
    except (discord.NotFound, discord.Forbidden):
        log.warning("_maybe_send_missed_summary: channel %d not found.", SUMMARY_CHANNEL_ID)
        return

    log.info("Sending missed weekly summary (should have fired %s).", last_sunday.isoformat())
    embed = await _build_weekly_summary_embed()
    if embed:
        await channel.send(embed=embed)
    else:
        await channel.send("No runs logged this week — lace up and get out there! 👟")
    await db.set_state("last_weekly_summary_sent", now.isoformat())


@bot.event
async def on_ready():
    await db.init()
    if database.GPX_RETENTION_DAYS > 0:
        pruned = await db.prune_old_gpx(database.GPX_RETENTION_DAYS)
        if pruned:
            log.info("Pruned %d GPX file(s) older than %d days.",
                     pruned, database.GPX_RETENTION_DAYS)
    synced = await bot.tree.sync()
    log.info("Logged in as %s  |  %d slash commands synced.", bot.user, len(synced))
    if not weekly_summary.is_running():
        weekly_summary.start()
    await _maybe_send_missed_summary()


# ---------------------------------------------------------------------------
# /upload
# ---------------------------------------------------------------------------

@bot.tree.command(name="upload", description="Upload a GPX file to record a run.")
@app_commands.describe(
    gpx_file="GPX file exported from Strava or any GPS app.",
    runner="Who ran this? Tag someone else if you're uploading on their behalf.",
    insights="Ask Gemini for a coaching analysis of this run (requires GEMINI_API_KEY).",
    force_new="Record as a new run even if it matches one already logged.",
)
async def upload(
    interaction: discord.Interaction,
    gpx_file: discord.Attachment,
    runner: discord.Member = None,
    insights: bool = False,
    force_new: bool = False,
):
    await interaction.response.defer()

    target = runner or interaction.user

    if not gpx_file.filename.lower().endswith(".gpx"):
        await interaction.followup.send("Please upload a `.gpx` file.")
        return

    try:
        raw = await gpx_file.read()
        stats = await asyncio.to_thread(get_run_stats, raw)
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

    # Re-uploading an old run to backfill its GPX must not create a second
    # record, so look for the same activity before inserting.
    match = None if force_new else await db.find_matching_run(
        str(target.id), stats.get("date"),
        stats.get("total_dist_km"), stats.get("total_time_s"),
    )

    if match:
        stored = await db.attach_gpx(match["id"], raw, stats, overwrite_times=True)
        tag = match["tag"]
        embed = discord.Embed(
            title=f"Matched an existing run for {target.display_name}",
            description=(
                f"This is the same activity as **`{tag}`** "
                f"({stats.get('date')}), so it was updated in place rather "
                f"than logged twice."
                + ("" if stored else "\n⚠️ The file was too large to store.")
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name="What changed",
            value=(
                ("✅ GPX stored — this run can now be reprocessed\n" if stored else "")
                + "✅ Best-effort ladder recorded\n"
                + "✅ Times refreshed from the track"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Tag: {tag}  ·  Pass force_new: True to log it separately")
        await interaction.followup.send(embed=embed)
        if insights:
            await _send_insights(interaction, stats, target)
        return

    tag = await db.add_run(
        discord_user_id=str(target.id),
        discord_username=target.display_name,
        run_date=stats.get("date"),
        mile_time=stats.get("mile_time"),
        fivek_time=stats.get("fivek_time"),
        tenk_time=stats.get("tenk_time"),
        filename=gpx_file.filename,
        stats=stats,
        gpx_bytes=raw,
    )

    embed = discord.Embed(
        title=f"Run recorded for {target.display_name}!",
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    if stats.get("total_dist_km"):
        embed.description = (
            f"**{stats['total_dist_km']:.2f} km** "
            f"({stats['total_dist_miles']:.2f} mi) in {fmt_time(stats.get('moving_time_s'))}"
        )

    # Fastest contiguous segments found inside this run
    found = False
    for key, (label, emoji, meters) in EVENTS.items():
        secs = stats.get(EVENT_COLUMNS[key])
        if secs:
            embed.add_field(name=f"Fastest {label}", value=f"`{fmt_time(secs)}`", inline=True)
            found = True
    if not found:
        embed.add_field(name="Fastest Mile", value="N/A — run too short", inline=True)

    if stats.get("date"):
        embed.add_field(name="Date", value=stats["date"], inline=True)

    footer = f"Tag: {tag}  ·  /insights tag:{tag} to analyse  ·  /profile to see predictions"
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
        stats = await asyncio.to_thread(get_run_stats, raw)
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
@app_commands.describe(event="Mile, 5K or 10K — omit to show all three.")
@app_commands.choices(
    event=[
        app_commands.Choice(name="Mile", value="mile"),
        app_commands.Choice(name="5K", value="5k"),
        app_commands.Choice(name="10K", value="10k"),
    ]
)
async def leaderboard(interaction: discord.Interaction, event: str = None):
    keys = [event] if event else list(EVENTS)
    results = await asyncio.gather(*(db.get_leaderboard(k) for k in keys))

    if not any(results):
        where = f"{EVENTS[event][0]} times" if event else "times"
        await interaction.response.send_message(
            f"No {where} on the board yet. Record one with `/upload`, or enter "
            f"it by hand with `/logtime`."
        )
        return

    if event:
        label, emoji, _ = EVENTS[event]
        embed = discord.Embed(
            title=f"{emoji} Fastest {label} Times", color=discord.Color.orange()
        )
        embed.description = "\n".join(
            f"{rank_str(i)}  **{u}** — `{fmt_time(t)}`"
            for i, (u, t) in enumerate(results[0])
        )
    else:
        embed = discord.Embed(title="🏃 Leaderboard", color=discord.Color.orange())
        for key, rows in zip(keys, results):
            label, emoji, _ = EVENTS[key]
            embed.add_field(
                name=f"{emoji} {label}",
                value="\n".join(
                    f"{rank_str(i)}  **{u}** — `{fmt_time(t)}`"
                    for i, (u, t) in enumerate(rows)
                ) or "*No times yet*",
                inline=True,
            )
        embed.set_footer(text="/profile for predictions and runner type")

    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Profile embeds — shared by /profile, /pb and /runs
# ---------------------------------------------------------------------------

def _build_overview_embed(target, bests: dict, profile: dict, dist: dict) -> discord.Embed:
    dist = dist or {}
    embed = discord.Embed(
        title=f"Profile — {target.display_name}",
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    if not _pr_fields(embed, bests):
        embed.description = "No timed segments yet — log a mile, 5K or 10K to unlock predictions."

    rtype = profile.get("runner_type")
    if rtype:
        embed.add_field(
            name=f"{rtype['emoji']} Runner Type: {rtype['label']}",
            value=(
                f"{rtype['blurb']}\n*Fade exponent k = {profile['k']:.2f} — read from "
                f"your PRs, so it's sharpest when those came from real hard efforts "
                f"rather than segments inside an easy run.*"
            ),
            inline=False,
        )
    elif profile["efforts"]:
        embed.add_field(
            name="🧭 Runner Type",
            value=(
                "Needs PRs at **two different distances** to work out how you fade "
                "as races get longer. Log another distance with `/logtime`."
            ),
            inline=False,
        )

    summary = [f"**{bests['run_count']}** run{'s' if bests['run_count'] != 1 else ''} logged"]
    if bests["gps_count"]:
        summary.append(f"**{bests['gps_count']}** GPS-verified 📍")
    if dist.get("total_km"):
        summary.append(f"**{dist['total_km']:.1f} km** tracked")
    if profile.get("vdot"):
        summary.append(f"VDOT **{profile['vdot']:.1f}**")
    if profile.get("cs_pace_s_mi"):
        summary.append(f"threshold ~**{fmt_pace_mi(profile['cs_pace_s_mi'])}**")
    embed.add_field(name="📊 Stats", value="  ·  ".join(summary), inline=False)

    if dist.get("longest_km"):
        longest = f"**{dist['longest_km']:.2f} km**"
        if dist.get("longest_miles"):
            longest += f"  ({dist['longest_miles']:.2f} mi)"
        detail = "  ·  ".join(
            p for p in (dist.get("longest_date"), f"`{dist['longest_tag']}`"
                        if dist.get("longest_tag") else None) if p
        )
        embed.add_field(
            name="🏔️ Longest Run",
            value=f"{longest}\n{detail}" if detail else longest,
            inline=False,
        )

    if bests["first_date"] and bests["last_date"]:
        span = bests["first_date"]
        if bests["last_date"] != bests["first_date"]:
            span += f" → {bests['last_date']}"
        embed.set_footer(text=f"Active {span}")
    return embed


def _build_predictions_embed(target, profile: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"Race Predictions — {target.display_name}",
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    if not profile["efforts"]:
        embed.description = (
            "No times recorded yet. Upload a run with `/upload` or log one with "
            "`/logtime` to get predictions."
        )
        return embed

    rows = ["Distance     Time       Pace", "─" * 32]
    for p in profile["predictions"]:
        secs = p["actual"] or p["time"]
        # Without a personal exponent every prediction is a guess off the
        # textbook curve, so flag the lot rather than just the long throws.
        trusted = p["reliable"] and profile["k_is_personal"]
        mark = "★" if p["actual"] else (" " if trusted else "~")
        rows.append(
            f"{p['label']:<9}{fmt_time(secs):>8} {mark}  "
            f"{fmt_pace_mi(pace_per_mile(secs, p['meters'])):>8}"
        )
    embed.description = "```\n" + "\n".join(rows) + "\n```★ your PR   ~ rough extrapolation"

    if profile["k_is_personal"]:
        anchors = ", ".join(e["label"] for e in profile["efforts"])
        model = (
            f"Riegel fitted to **your own** {anchors} PR"
            f"{'s' if len(profile['efforts']) > 1 else ''} — "
            f"exponent **k = {profile['k']:.2f}**.\n"
            f"Textbook Riegel uses {race_analysis.RIEGEL_K}; most recreational "
            f"runners sit near **1.10–1.18**. The higher your k, the more you "
            f"fade as races get longer."
        )
        span = profile.get("riegel_gap_span")
        if span and profile.get("riegel_gap_pct") is not None:
            model += (
                f"\nOver **{span[0]} → {span[1]}** you run "
                f"**{profile['riegel_gap_pct']:+.0f}%** against a flat "
                f"k={race_analysis.RIEGEL_K} curve."
            )
    else:
        model = (
            f"Only one distance on record, so these use the **textbook** Riegel "
            f"exponent k = {race_analysis.RIEGEL_K}. Log a second distance and the "
            f"model retunes to how *you* actually fade."
        )
    embed.add_field(name="📐 Model", value=model, inline=False)

    if profile.get("cs_pace_s_mi"):
        embed.add_field(
            name="🎯 Training paces",
            value=(
                f"Threshold / tempo ≈ **{fmt_pace_mi(profile['cs_pace_s_mi'])}** "
                f"(critical speed, D′ {profile['d_prime_m']:.0f} m)"
            ),
            inline=False,
        )

    embed.set_footer(
        text="Predictions assume equivalent training and effort at every distance."
    )
    return embed


def _build_history_embed(target, runs: list, page: int, total: int) -> discord.Embed:
    pages = max(1, -(-total // RUNS_PER_PAGE))
    embed = discord.Embed(
        title=f"Run History — {target.display_name}",
        color=discord.Color.blurple(),
    )

    lines = []
    for r in runs:
        parts = [
            f"{label}: `{fmt_time(r[EVENT_COLUMNS[key]])}`"
            for key, (label, _, _) in EVENTS.items()
            if r[EVENT_COLUMNS[key]]
        ]
        gps_badge = " 📍" if r["gps_verified"] else ""
        gps_badge += " 💾" if r.get("has_gpx") else ""
        time_str = "  ·  ".join(parts) or "No timed segments"
        date = r["run_date"] or r["filename"] or "Unknown date"
        lines.append(f"**`{r['tag']}`** {date}{gps_badge} — {time_str}")
    embed.description = "\n".join(lines) or "No runs recorded yet."

    embed.set_footer(text=f"Page {page + 1}/{pages}  ·  {total} run{'s' if total != 1 else ''} total")
    return embed


_QUALITY_MARK = {"hard": "🔥", "easy": "〰️", "unknown": "·"}


def _build_efforts_embed(target, envelope: list, max_hr) -> discord.Embed:
    """The runner's best-effort envelope — fastest window at each distance
    across every run, with how hard that window looked."""
    embed = discord.Embed(
        title=f"Best Efforts — {target.display_name}",
        color=discord.Color.teal(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    if not envelope:
        embed.description = (
            "No GPS runs yet. Best efforts are pulled from uploaded GPX files — "
            "times entered with `/logtime` are recorded as PRs but have no track "
            "to search."
        )
        return embed

    rows = ["Dist       Time     HR   Effort", "─" * 34]
    for e in envelope:
        quality = race_analysis.effort_quality(e, max_hr)
        hr = f"{e['avg_hr']:.0f}" if e.get("avg_hr") else "—"
        rows.append(
            f"{e['label']:<9}{fmt_time(e['time_s']):>8}{hr:>6}   "
            f"{_QUALITY_MARK[quality]}"
        )
    embed.description = "```\n" + "\n".join(rows) + "\n```"

    legend = "🔥 looks like a real effort   〰️ likely from an easy run"
    if max_hr:
        legend += f"\nJudged against your observed max HR of **{max_hr:.0f}** bpm."
    else:
        legend += (
            "\nNo heart-rate data, so this falls back to how much faster the "
            "window was than the rest of the run."
        )
    embed.add_field(name="Reading this", value=legend, inline=False)
    embed.add_field(
        name="⚠️ Not used for predictions yet",
        value=(
            "These thresholds haven't been calibrated against your group's runs. "
            "Predictions still come from your recorded PRs. Check whether the "
            "🔥 marks match runs you know were hard — that's what calibrates them."
        ),
        inline=False,
    )
    embed.set_footer(text=f"{len(envelope)} distances  ·  from your uploaded GPX runs")
    return embed


class ProfileView(discord.ui.View):
    """Tabbed profile: overview, predictions and a paged run history."""

    def __init__(self, target, bests: dict, profile: dict, dist: dict, total_runs: int,
                 envelope: list = None, max_hr=None, page: str = "overview"):
        super().__init__(timeout=300)
        self.target = target
        self.bests = bests
        self.profile = profile
        self.dist = dist
        self.envelope = envelope or []
        self.max_hr = max_hr
        self.total_runs = total_runs
        self.history_page = 0
        self.message: discord.Message | None = None
        self.page = page
        self._sync_buttons()

    # -- state -------------------------------------------------------------

    def _sync_buttons(self) -> None:
        """Grey out the current tab and hide paging outside the history tab."""
        on_history = self.page == "history"
        pages = max(1, -(-self.total_runs // RUNS_PER_PAGE))
        for item in self.children:
            cid = getattr(item, "custom_id", None)
            if cid in ("overview", "predictions", "history", "efforts"):
                item.disabled = cid == self.page
                item.style = (
                    discord.ButtonStyle.primary if cid == self.page
                    else discord.ButtonStyle.secondary
                )
            elif cid == "prev":
                item.disabled = not on_history or self.history_page == 0
            elif cid == "next":
                item.disabled = not on_history or self.history_page >= pages - 1

    async def _render(self, interaction: discord.Interaction) -> None:
        if self.page == "overview":
            embed = _build_overview_embed(self.target, self.bests, self.profile, self.dist)
        elif self.page == "predictions":
            embed = _build_predictions_embed(self.target, self.profile)
        elif self.page == "efforts":
            embed = _build_efforts_embed(self.target, self.envelope, self.max_hr)
        else:
            runs = await db.get_runs(
                str(self.target.id),
                limit=RUNS_PER_PAGE,
                offset=self.history_page * RUNS_PER_PAGE,
            )
            embed = _build_history_embed(self.target, runs, self.history_page, self.total_runs)
        self._sync_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    async def initial_embed(self) -> discord.Embed:
        if self.page == "overview":
            return _build_overview_embed(self.target, self.bests, self.profile, self.dist)
        if self.page == "predictions":
            return _build_predictions_embed(self.target, self.profile)
        if self.page == "efforts":
            return _build_efforts_embed(self.target, self.envelope, self.max_hr)
        runs = await db.get_runs(str(self.target.id), limit=RUNS_PER_PAGE)
        return _build_history_embed(self.target, runs, 0, self.total_runs)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # -- buttons -----------------------------------------------------------

    @discord.ui.button(label="Overview", custom_id="overview", row=0)
    async def overview_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = "overview"
        await self._render(interaction)

    @discord.ui.button(label="Predictions", custom_id="predictions", row=0)
    async def predictions_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = "predictions"
        await self._render(interaction)

    @discord.ui.button(label="Efforts", custom_id="efforts", row=0)
    async def efforts_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = "efforts"
        await self._render(interaction)

    @discord.ui.button(label="History", custom_id="history", row=0)
    async def history_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = "history"
        self.history_page = 0
        await self._render(interaction)

    @discord.ui.button(label="◀", custom_id="prev", row=1)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.history_page = max(0, self.history_page - 1)
        await self._render(interaction)

    @discord.ui.button(label="▶", custom_id="next", row=1)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.history_page += 1
        await self._render(interaction)


async def _send_profile(interaction: discord.Interaction, runner, page: str) -> None:
    """Load a runner's data once and hand it to a ProfileView."""
    target = runner or interaction.user
    bests, dist, envelope = await asyncio.gather(
        db.get_personal_bests(str(target.id)),
        db.get_distance_stats(str(target.id)),
        db.get_effort_envelope(str(target.id)),
    )
    # Observed max HR across every stored effort — the yardstick for judging
    # whether a given window was actually hard.
    max_hr = max((e["max_hr"] for e in envelope if e.get("max_hr")), default=None)

    if not bests:
        await interaction.response.send_message(
            f"No runs recorded for **{target.display_name}** yet. "
            "Upload one with `/upload` or log a time with `/logtime`."
        )
        return

    profile = race_analysis.build_profile(bests)
    view = ProfileView(target, bests, profile, dist, bests["run_count"],
                       envelope=envelope, max_hr=max_hr, page=page)
    await interaction.response.send_message(embed=await view.initial_embed(), view=view)
    view.message = await interaction.original_response()


# ---------------------------------------------------------------------------
# /profile, /pb, /runs
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="profile",
    description="Full runner profile: PRs, race predictions, runner type and run history.",
)
@app_commands.describe(runner="Whose profile to show (defaults to you).")
async def profile(interaction: discord.Interaction, runner: discord.Member = None):
    await _send_profile(interaction, runner, "overview")


@bot.tree.command(name="pb", description="Show personal bests for a runner.")
@app_commands.describe(runner="Whose PBs to look up (defaults to you).")
async def pb(interaction: discord.Interaction, runner: discord.Member = None):
    await _send_profile(interaction, runner, "overview")


@bot.tree.command(name="runs", description="Show a runner's full run history.")
@app_commands.describe(runner="Whose runs to show (defaults to you).")
async def runs(interaction: discord.Interaction, runner: discord.Member = None):
    await _send_profile(interaction, runner, "history")


@bot.tree.command(
    name="efforts",
    description="Your fastest window at every distance, and how hard each looked.",
)
@app_commands.describe(runner="Whose best efforts to show (defaults to you).")
async def efforts(interaction: discord.Interaction, runner: discord.Member = None):
    await _send_profile(interaction, runner, "efforts")


@bot.tree.command(
    name="predict",
    description="Race-time predictions from a runner's PRs (Riegel, tuned to them).",
)
@app_commands.describe(runner="Whose predictions to show (defaults to you).")
async def predict(interaction: discord.Interaction, runner: discord.Member = None):
    await _send_profile(interaction, runner, "predictions")


# ---------------------------------------------------------------------------
# /logtime
# ---------------------------------------------------------------------------

# Event key -> (min seconds, max seconds) sanity bounds for manual entry
_TIME_BOUNDS = {
    "mile": (60, 3600),
    "5k": (600, 7200),
    "10k": (1200, 14400),
}


@bot.tree.command(name="logtime", description="Manually log a mile, 5K and/or 10K time without a GPX file.")
@app_commands.describe(
    mile="Fastest mile time, e.g. 7:30",
    fivek="Fastest 5K time, e.g. 25:00",
    tenk="Fastest 10K time, e.g. 55:00",
    runner="Who ran this? Defaults to you.",
    date="Date of the run (YYYY-MM-DD). Defaults to today.",
)
async def logtime(
    interaction: discord.Interaction,
    mile: str = None,
    fivek: str = None,
    tenk: str = None,
    runner: discord.Member = None,
    date: str = None,
):
    raw = {"mile": mile, "5k": fivek, "10k": tenk}
    if not any(raw.values()):
        await interaction.response.send_message(
            "Provide at least one time — `mile`, `fivek` or `tenk`.", ephemeral=True
        )
        return

    times: dict[str, float] = {}
    for key, value in raw.items():
        if not value:
            continue
        label = EVENTS[key][0]
        try:
            secs = parse_time(value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        lo, hi = _TIME_BOUNDS[key]
        if not (lo <= secs <= hi):
            await interaction.response.send_message(
                f"That {label} time doesn't look right — it must be between "
                f"`{fmt_time(lo)}` and `{fmt_time(hi)}`.",
                ephemeral=True,
            )
            return
        times[key] = secs

    target = runner or interaction.user
    run_date = date or discord.utils.utcnow().strftime("%Y-%m-%d")

    tag = await db.add_run(
        discord_user_id=str(target.id),
        discord_username=target.display_name,
        run_date=run_date,
        mile_time=times.get("mile"),
        fivek_time=times.get("5k"),
        tenk_time=times.get("10k"),
        filename="manual entry",
        stats=None,
    )

    embed = discord.Embed(
        title=f"Time logged for {target.display_name}!",
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    for key, secs in times.items():
        label, emoji, _ = EVENTS[key]
        embed.add_field(name=f"{emoji} {label}", value=f"`{fmt_time(secs)}`", inline=True)
    embed.add_field(name="Date", value=run_date, inline=True)

    footer = f"Tag: {tag}  ·  /profile to see your predictions"
    if runner and runner != interaction.user:
        footer += f"  ·  Logged by {interaction.user.display_name}"
    embed.set_footer(text=footer)

    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /attach
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="attach",
    description="Attach a GPX file to a run you already logged, by its tag.",
)
@app_commands.describe(
    tag="The run to attach it to (shown in /runs).",
    gpx_file="The GPX for that run.",
    overwrite_times="Replace the run's recorded times with ones from the track.",
)
async def attach(
    interaction: discord.Interaction,
    tag: str,
    gpx_file: discord.Attachment,
    overwrite_times: bool = False,
):
    """Backfill a GPX onto an existing run.

    The explicit counterpart to /upload's automatic matching — for manual
    entries, which are never auto-matched, and for anything the matcher
    misses.
    """
    await interaction.response.defer()

    row = await db.get_run_row_by_tag(tag)
    if not row:
        await interaction.followup.send(f"No run found with tag `{tag.upper()}`.")
        return
    if not gpx_file.filename.lower().endswith(".gpx"):
        await interaction.followup.send("Please upload a `.gpx` file.")
        return

    try:
        raw = await gpx_file.read()
        stats = await asyncio.to_thread(get_run_stats, raw)
    except Exception:
        log.exception("attach: parse failed")
        await interaction.followup.send("Failed to parse the GPX file.")
        return
    if not stats:
        await interaction.followup.send("No valid timed GPS data in that file.")
        return

    was_manual = row.get("filename") == "manual entry"
    # A hand-entered time is a deliberate statement — often an official race
    # result that beats whatever a re-parsed track computes — so keep it
    # unless asked otherwise.
    keep_times = was_manual and not overwrite_times

    stored = await db.attach_gpx(
        row["id"], raw, stats, overwrite_times=not keep_times
    )
    if not stored:
        await interaction.followup.send(
            "That file is too large to store (8 MB compressed limit)."
        )
        return

    embed = discord.Embed(
        title=f"GPX attached to `{row['tag']}`",
        color=discord.Color.green(),
    )
    lines = ["✅ GPX stored — this run can now be reprocessed",
             "✅ Best-effort ladder recorded"]
    if keep_times:
        lines.append(
            f"↩️ Kept your logged times (the track says "
            f"{fmt_time(stats.get('mile_time')) if stats.get('mile_time') else '—'} "
            f"for the mile). Pass `overwrite_times: True` to use the track instead."
        )
    else:
        lines.append("✅ Times refreshed from the track")
    embed.description = "\n".join(lines)

    if row.get("has_gpx"):
        embed.set_footer(text="This run already had a GPX — it was replaced.")
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /reprocess
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="reprocess",
    description="Re-derive every stored run's times from its saved GPX file.",
)
@app_commands.default_permissions(manage_guild=True)
async def reprocess(interaction: discord.Interaction):
    """Recompute times for runs whose GPX was retained.

    This is what makes storing the files worth it: a new distance or a parser
    fix can be applied to history instead of only to future uploads.
    """
    await interaction.response.defer(ephemeral=True)

    stored = await db.iter_stored_gpx()
    if not stored:
        await interaction.followup.send(
            "No stored GPX files yet. Runs uploaded from now on keep their "
            "source file and can be reprocessed later.",
            ephemeral=True,
        )
        return

    changed = failed = 0
    for entry in stored:
        try:
            # Parsing is CPU-bound; keep it off the event loop so the bot
            # stays responsive through a long batch.
            stats = await asyncio.to_thread(get_run_stats, entry["gpx"])
        except Exception:
            log.exception("reprocess: failed to parse %s", entry["tag"])
            failed += 1
            continue
        if not stats:
            failed += 1
            continue
        await db.update_run_stats(
            entry["run_id"], stats.get("mile_time"), stats.get("fivek_time"),
            stats.get("tenk_time"), stats,
        )
        changed += 1

    storage = await db.get_storage_stats()
    embed = discord.Embed(
        title="Reprocess complete",
        description=(
            f"Re-derived **{changed}** run{'s' if changed != 1 else ''} from "
            f"stored GPX."
            + (f"\n**{failed}** could not be parsed." if failed else "")
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text=_storage_footer(storage))
    await interaction.followup.send(embed=embed, ephemeral=True)


def _storage_footer(s: dict) -> str:
    kept = f"{s['files']}/{s['total_runs']} runs have their GPX retained"
    if s["stored_bytes"]:
        mb = s["stored_bytes"] / 1e6
        ratio = (s["orig_bytes"] / s["stored_bytes"]) if s["stored_bytes"] else 0
        kept += f"  ·  {mb:.1f} MB stored"
        if ratio > 1:
            kept += f" ({ratio:.1f}x compressed)"
    if database.GPX_RETENTION_DAYS > 0:
        kept += f"  ·  kept {database.GPX_RETENTION_DAYS} days"
    return kept


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
        entry = runners.setdefault(
            r["user_id"],
            {"username": r["username"], "run_count": 0, "best": {}},
        )
        entry["run_count"] += 1
        for key in EVENTS:
            col = EVENT_COLUMNS[key]
            t = r[col]
            if t and (entry["best"].get(key) is None or t < entry["best"][key]):
                entry["best"][key] = t

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
        times = [
            f"{EVENTS[key][0]} `{fmt_time(entry['best'][key])}`"
            for key in EVENTS
            if entry["best"].get(key)
        ]
        if times:
            parts.append("(" + ", ".join(times) + ")")
        lines.append(" ".join(parts))

    embed.add_field(name="Who ran this week", value="\n".join(lines), inline=False)

    # Shoutouts
    shoutouts = []
    most_active = max(runners.values(), key=lambda e: e["run_count"])
    if most_active["run_count"] > 1:
        shoutouts.append(
            f"**Most dedicated:** {most_active['username']} with "
            f"{most_active['run_count']} runs — consistency wins! 🔥"
        )
    for key, (label, emoji, _) in EVENTS.items():
        fastest = min(
            (e for e in runners.values() if e["best"].get(key)),
            key=lambda e: e["best"][key],
            default=None,
        )
        if fastest:
            shoutouts.append(
                f"**Fastest {label} this week:** {fastest['username']} — "
                f"`{fmt_time(fastest['best'][key])}` {emoji}"
            )

    if shoutouts:
        embed.add_field(name="Shoutouts", value="\n".join(shoutouts), inline=False)

    embed.set_footer(text="Keep it up everyone — see you next Sunday! 🌅")
    return embed


@tasks.loop(time=datetime.time(hour=9, minute=0, tzinfo=datetime.timezone.utc))
async def weekly_summary():
    if datetime.datetime.now(datetime.timezone.utc).weekday() != 6:  # 6 = Sunday
        return

    try:
        channel = bot.get_channel(SUMMARY_CHANNEL_ID) or await bot.fetch_channel(SUMMARY_CHANNEL_ID)
    except (discord.NotFound, discord.Forbidden):
        log.warning("weekly_summary: channel %d not found.", SUMMARY_CHANNEL_ID)
        return

    embed = await _build_weekly_summary_embed()
    if embed:
        await channel.send(embed=embed)
    else:
        await channel.send("No runs logged this week — lace up and get out there! 👟")
    await db.set_state(
        "last_weekly_summary_sent",
        datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


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
