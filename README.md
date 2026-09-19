# Running Leaderboard

A Discord bot that turns GPX files into a running leaderboard for a friend group — fastest mile, 5K and 10K, plus race predictions tuned to how each runner actually fades over distance.

No Strava account required for anyone. Export a GPX from your watch, drop it in Discord, and the bot finds your fastest contiguous mile/5K/10K inside that run the same way Strava does.

---

## Commands

| Command | What it does |
|---|---|
| `/upload gpx_file: [runner:] [insights:]` | Record a run from a GPX file. Tag `runner` to upload on someone's behalf; set `insights: true` for a Gemini coaching write-up. |
| `/logtime [mile:] [fivek:] [tenk:] [runner:] [date:]` | Log times by hand — no GPX needed. |
| `/leaderboard [event:]` | Ranked board for Mile, 5K or 10K. Omit `event` to see all three side by side. |
| `/profile [runner:]` | Full runner profile — PRs, runner type, race predictions and run history, in one tabbed view. |
| `/predict [runner:]` | Jump straight to the predictions tab. |
| `/pb [runner:]` | Jump straight to the PR overview. |
| `/runs [runner:]` | Jump straight to the paged run history. |
| `/insights [tag:] [gpx_file:] [runner:]` | Gemini analysis of a stored run (by tag) or an attached GPX. |
| `/remove tag:` | Delete a run by its tag — removes its stored GPX too. |
| `/efforts [runner:]` | Fastest window at every distance from your GPX runs, with how hard each looked. |
| `/attach tag: gpx_file:` | Attach a GPX to a run you already logged — backfills its source file without duplicating the run. |
| `/reprocess` | Re-derive every stored run's times and best efforts from its saved GPX. Requires **Manage Server**. |
| `/weekly_summary` | Preview the Sunday wrap-up in the current channel. |

Every run gets a short **tag** (e.g. `AB3KQ`) shown on upload and in `/runs` — that's what `/insights` and `/remove` take.

A weekly wrap-up posts automatically every **Sunday at 09:00 UTC**: who ran, how often, and the fastest mile/5K/10K of the week. If the bot was offline when it was due, it sends on next startup rather than skipping the week.

---

## The `/profile` view

Three tabs behind buttons on one message:

- **Overview** — PRs with per-mile pace, your runner type, total runs, distance tracked, VDOT and threshold pace.
- **Predictions** — predicted times from 1K to marathon. `★` marks a real PR, `~` marks a rough extrapolation.
- **History** — every run you've ever logged, 8 per page. Nothing is pruned.

### Runner types

Classified with Greg McMillan's three types, keyed off your personal Riegel exponent **k** — how much you fade as races get longer:

| k | Type | Meaning |
|---|---|---|
| < 1.12 | 🐂 Endurance Monster | Holds pace as distance climbs. Gains are in top-end speed. |
| 1.12 – 1.18 | ⚖️ Combo Runner | Balanced; no glaring strength or weakness. |
| > 1.18 | ⚡ Speedster | Quick when short, fades when long. Gains are in aerobic base. |

---

## How the predictions work

**Riegel:** `T2 = T1 × (D2/D1)^k`. The textbook exponent is 1.06, but that describes trained racers — recreational runners fade considerably harder, so a flat 1.06 predicts times nobody actually runs.

So when you have PRs at **two or more distances**, the bot fits *your own* k by log-log least squares over your efforts and predicts from that instead. This tracks real performance far better than the fixed exponent ([Vickers & Vertosick 2016](https://doi.org/10.1186/s13102-016-0052-y), BMC Sports Sci Med Rehabil 8:26). With only one distance on record it falls back to 1.06 and flags every row as rough.

Each target distance is predicted from whichever PR is nearest to it in log-distance, since Riegel degrades the further you extrapolate.

Also computed:

- **VDOT** (Daniels/Gilbert) — a distance-neutral fitness score, so efforts at different distances compare directly.
- **Critical speed** — `D = CS·t + D'`, giving an estimated threshold/tempo pace and `D'`, the finite distance reserve available above it. The model is built for ~2–15 minute efforts, so it's fitted on your two *shortest* PRs; feeding it a 10K drags the fitted line flat.

### Caveats worth knowing

- Predictions assume equivalent training and effort at every distance.
- Your k is only as good as the efforts behind it. A mile lifted from inside an easy long run understates your speed and will skew the classification toward Endurance Monster.
- PRs may come from different dates and different fitness levels.

`python3 race_analysis.py` prints the models over a sample leaderboard — handy as a sanity check after touching the math.

---

## Best efforts and the effort envelope

Every GPX upload is searched for its fastest window at ten distances — 400m, 800m, 1K, 1600m, mile, 3K, 5K, 10K, 15K, half. The per-runner minimum at each distance is the **envelope** (a mean-maximal pace curve): your best-ever effort at every duration, not just three isolated PRs. `/efforts` shows it.

### Why effort quality is recorded

A fastest window is only a PR if you were trying. The bias runs one way — a segment is never faster than you can run, but it's often slower than you *could* — so distances you never push are silently understated, which flattens the prediction curve and pushes the runner-type classification toward Endurance Monster. Feeding a jogged 1K into the model moves a Speedster (k=1.22) to Endurance Monster (k=1.09).

So each effort stores three signals:

| Signal | What it catches |
|---|---|
| `avg_hr` | Strongest signal. A real effort sits near max HR; a jogged kilometre doesn't. |
| `pace_ratio` | Fallback when a file has no HR — how much faster the window was than the rest of the run. |
| `coverage` | Window distance ÷ run distance. Rescues a dedicated time trial, whose pace ratio is flat *by definition* because the effort was the whole run. |

**These do not yet gate predictions.** The thresholds in `race_analysis.effort_quality` are literature-shaped starting points, not calibrated against real runs. `/efforts` marks each distance 🔥 or 〰️ so you can check the marks against runs you know were hard — that's the calibration step, and until it's done predictions continue to come from recorded PRs.

Times entered with `/logtime` have no track to search, so they produce no envelope. They're treated as self-reported efforts and feed the model directly.

---

## Backfilling GPX for past runs

Runs logged before GPX retention existed have no source file, so they have no best-effort ladder and can't be reprocessed. You can backfill them by re-uploading the original files.

**`/upload` will not duplicate a run.** Before inserting, it looks for an existing run by the same person on the same date whose distance *and* duration both match within 2%. On a match it updates that run in place — stores the GPX, records the ladder, refreshes the times — and tells you which run it matched. Pass `force_new: True` if you really did run twice.

Matching is deliberately strict, so it handles the awkward cases:

- Two different runs on the same day stay separate; re-uploading either one finds the right record.
- A Garmin export that differs slightly from the original Strava export still matches.
- **Manual `/logtime` entries are never auto-matched.** A date alone isn't enough to risk overwriting the wrong run, so use `/attach` with the tag for those.

**`/attach tag: gpx_file:`** is the explicit version, for manual entries and anything the matcher misses. On a manual entry it keeps your typed times by default — a hand-entered time is usually a deliberate statement (an official race result that beats what a re-parsed track computes), so it isn't silently replaced. Pass `overwrite_times: True` to use the track's numbers instead.

After backfilling, run `/reprocess` to rebuild derived data across everything at once.

---

## Stored GPX files

Uploaded GPX files are retained, gzipped, in a separate `run_files` table (typically 5–12x compression). They sit in their own table so leaderboard queries never page through blob data.

This exists so **new metrics can be applied to old runs**. When the 10K board was added, existing runs couldn't get a 10K — only derived stats had been kept, and those predated the 10K. With the source file retained, `/reprocess` re-derives every stored run's times, so a future distance, a new metric, or a parser fix reaches history instead of only future uploads.

`/reprocess` is gated behind **Manage Server**, parses off the event loop so the bot stays responsive, and reports how many runs it rewrote.

### Privacy and retention

GPX tracks contain precise coordinates and timestamps — where people live and when they're out. The derived stats contain no coordinates at all, so retaining files is a real change in what the bot holds about your group.

Retention is controlled by `GPX_RETENTION_DAYS`:

- **`0`** (default) — keep indefinitely.
- **`N`** — drop stored files older than N days, on startup. The runs and their times survive; only the source files go, so those runs can no longer be reprocessed.

Files above 8 MB compressed are skipped; the run is still recorded.

---

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```
DISCORD_TOKEN=your_discord_bot_token
GEMINI_API_KEY=your_gemini_api_key
DB_PATH=leaderboard.db        # optional, defaults to ./leaderboard.db
GEMINI_MODEL=gemini-2.5-flash # optional
GPX_RETENTION_DAYS=0          # optional, 0 = keep stored GPX forever
```

The bot needs the **applications.commands** scope, and Message Content intent is *not* required. Then:

```bash
python bot.py
```

Slash commands sync on startup. For Docker and VM deployment, see [DOCKER.md](DOCKER.md).

---

## Layout

| File | Role |
|---|---|
| `bot.py` | Slash commands, embeds and the tabbed profile view. |
| `database.py` | SQLite store — schema, migrations and queries. |
| `gpx_processor.py` | GPX parsing, fastest-segment search and derived run stats. |
| `race_analysis.py` | Riegel / VDOT / critical-speed models and runner classification. |
| `formatting.py` | Shared time and pace formatting. |
| `gemini_insights.py` | Prompt building and the Gemini call. |

**Fastest-segment search:** a two-pointer sliding window over cumulative GPS distances, with linear interpolation at the trailing edge so a 4-second sampling gap doesn't cost you seconds on the clock.

**Storage:** every run is kept forever — the database is append-only apart from `/remove`. Missing columns and tables are added automatically on startup, so an existing database upgrades in place.

> If you add a new module, add it to the `COPY` line in the `Dockerfile` too — it lists files explicitly.
