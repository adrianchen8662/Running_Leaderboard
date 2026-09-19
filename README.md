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
| `/remove tag:` | Delete a run by its tag. |
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

**Storage:** every run is kept forever — the database is append-only apart from `/remove`. Missing columns are added automatically on startup, so an existing database upgrades in place.

> If you add a new module, add it to the `COPY` line in the `Dockerfile` too — it lists files explicitly.
