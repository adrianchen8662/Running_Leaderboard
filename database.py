import asyncio
import datetime
import gzip
import json
import os
import random
from typing import Optional, List, Tuple, Dict, Any

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "leaderboard.db")

# Raw GPX files are kept so new metrics can be derived from old runs later.
# They contain precise coordinates and timestamps, so retention is capped by
# GPX_RETENTION_DAYS; 0 (the default) keeps them indefinitely.
GPX_RETENTION_DAYS = int(os.getenv("GPX_RETENTION_DAYS", "0"))
# Skip storing anything above this compressed size -- the run is still
# recorded, it just can't be reprocessed later.
MAX_STORED_GPX_BYTES = 8 * 1024 * 1024

# Unambiguous alphanumeric chars — no O/0, I/1, L
_TAG_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_TAG_LEN = 5

# Leaderboard event -> column.  Single source of truth for the event names
# accepted by /leaderboard and stored per run.
EVENT_COLUMNS: Dict[str, str] = {
    "mile": "mile_time",
    "5k": "fivek_time",
    "10k": "tenk_time",
}

# Columns the schema must have, with their types.  Anything missing from an
# existing database is added on startup.
_EXPECTED_COLUMNS: Dict[str, str] = {
    "tag": "TEXT",
    "stats_json": "TEXT",
    "mile_time": "REAL",
    "fivek_time": "REAL",
    "tenk_time": "REAL",
}


def _random_tag() -> str:
    return "".join(random.choices(_TAG_CHARS, k=_TAG_LEN))


class Database:
    """SQLite store for runs.

    Holds one long-lived connection rather than reopening the file for every
    query; ``aiosqlite`` runs it on a dedicated thread, and a lock keeps
    concurrent commands from interleaving writes.
    """

    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def init(self) -> None:
        """Open the connection and bring the schema up to date (idempotent —
        ``on_ready`` fires again on every reconnect)."""
        async with self._lock:
            if self._conn is not None:
                return

            conn = await aiosqlite.connect(self.path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag              TEXT    UNIQUE,
                    discord_user_id  TEXT    NOT NULL,
                    discord_username TEXT    NOT NULL,
                    run_date         TEXT,
                    mile_time        REAL,
                    fivek_time       REAL,
                    tenk_time        REAL,
                    filename         TEXT,
                    stats_json       TEXT,
                    uploaded_at      TEXT    DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cur = await conn.execute("PRAGMA table_info(runs)")
            existing = {row["name"] for row in await cur.fetchall()}
            for col, col_type in _EXPECTED_COLUMNS.items():
                if col not in existing:
                    # UNIQUE can't be declared in ALTER TABLE ADD COLUMN; the
                    # index below covers `tag`.
                    await conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {col_type}")

            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_tag ON runs (tag)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_user ON runs (discord_user_id, id DESC)"
            )
            # Blobs live in their own table so scans of `runs` (every
            # leaderboard query) never page through GPX payloads.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_files (
                    run_id     INTEGER PRIMARY KEY
                                 REFERENCES runs(id) ON DELETE CASCADE,
                    gpx_gz     BLOB    NOT NULL,
                    orig_bytes INTEGER,
                    stored_at  TEXT    DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # One row per run per ladder rung. The per-runner MIN over this
            # table is the envelope (mean-maximal pace curve) the models
            # should eventually be fitted to.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS best_efforts (
                    run_id     INTEGER NOT NULL
                                 REFERENCES runs(id) ON DELETE CASCADE,
                    meters     REAL    NOT NULL,
                    label      TEXT,
                    time_s     REAL    NOT NULL,
                    avg_hr     REAL,
                    max_hr     REAL,
                    pace_ratio REAL,
                    coverage   REAL,
                    PRIMARY KEY (run_id, meters)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_best_efforts_meters "
                "ON best_efforts (meters, time_s)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            await conn.commit()
            self._conn = conn

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() must be awaited before use.")
        return self._conn

    async def _fetchall(self, query: str, params: tuple = ()) -> List[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(query, params)
            return await cur.fetchall()

    async def _fetchone(self, query: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(query, params)
            return await cur.fetchone()

    # -- bot state ---------------------------------------------------------

    async def get_state(self, key: str) -> Optional[str]:
        row = await self._fetchone("SELECT value FROM bot_state WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_state(self, key: str, value: str) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self.conn.commit()

    # -- runs --------------------------------------------------------------

    async def _unique_tag(self) -> str:
        """Generate a tag that doesn't already exist in the DB."""
        for _ in range(20):
            tag = _random_tag()
            cur = await self.conn.execute("SELECT 1 FROM runs WHERE tag = ?", (tag,))
            if not await cur.fetchone():
                return tag
        raise RuntimeError("Could not generate a unique run tag after 20 attempts.")

    async def add_run(
        self,
        discord_user_id: str,
        discord_username: str,
        run_date: Optional[str],
        mile_time: Optional[float],
        fivek_time: Optional[float],
        filename: str,
        tenk_time: Optional[float] = None,
        stats: Optional[Dict[str, Any]] = None,
        gpx_bytes: Optional[bytes] = None,
    ) -> str:
        """Insert a run and return its unique tag. Every run is kept — nothing
        is pruned or overwritten.

        ``gpx_bytes`` is stored gzipped so future metrics can be re-derived
        from the original track rather than lost to whatever the parser
        happened to compute at upload time.
        """
        async with self._lock:
            tag = await self._unique_tag()
            cur = await self.conn.execute(
                """
                INSERT INTO runs
                    (tag, discord_user_id, discord_username, run_date,
                     mile_time, fivek_time, tenk_time, filename, stats_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tag, discord_user_id, discord_username, run_date,
                    mile_time, fivek_time, tenk_time, filename,
                    json.dumps(stats) if stats else None,
                ),
            )
            run_id = cur.lastrowid
            if stats and stats.get("best_efforts"):
                await self._write_best_efforts(run_id, stats["best_efforts"])
            if gpx_bytes:
                blob = gzip.compress(gpx_bytes)
                if len(blob) <= MAX_STORED_GPX_BYTES:
                    await self.conn.execute(
                        "INSERT OR REPLACE INTO run_files (run_id, gpx_gz, orig_bytes) "
                        "VALUES (?, ?, ?)",
                        (run_id, blob, len(gpx_bytes)),
                    )
            await self.conn.commit()
            return tag

    async def get_run_by_tag(self, tag: str) -> Optional[Dict[str, Any]]:
        row = await self._fetchone(
            """
            SELECT discord_user_id, discord_username, stats_json, filename
            FROM runs WHERE tag = ?
            """,
            (tag.upper(),),
        )
        if not row:
            return None
        return {
            "user_id": row["discord_user_id"],
            "username": row["discord_username"],
            "stats": json.loads(row["stats_json"]) if row["stats_json"] else None,
            "filename": row["filename"],
        }

    async def delete_run_by_tag(self, tag: str) -> str:
        """Delete a run by tag. Returns 'deleted' or 'not_found'."""
        async with self._lock:
            cur = await self.conn.execute(
                "DELETE FROM runs WHERE tag = ?", (tag.upper(),)
            )
            await self.conn.commit()
            return "deleted" if cur.rowcount else "not_found"

    async def get_leaderboard(self, event: str, limit: int = 20) -> List[Tuple[str, float]]:
        col = EVENT_COLUMNS.get(event)
        if col is None:
            raise ValueError(f"Unknown leaderboard event: {event!r}")
        rows = await self._fetchall(
            f"""
            SELECT
                (
                    SELECT discord_username FROM runs
                    WHERE discord_user_id = r.discord_user_id
                    ORDER BY id DESC LIMIT 1
                ) AS username,
                MIN(r.{col}) AS best_time
            FROM runs r
            WHERE r.{col} IS NOT NULL
            GROUP BY r.discord_user_id
            ORDER BY best_time ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [(row["username"], row["best_time"]) for row in rows]

    async def get_personal_bests(self, discord_user_id: str) -> Optional[dict]:
        """PRs plus the summary counters the profile needs."""
        row = await self._fetchone(
            """
            SELECT MIN(mile_time)  AS mile_time,
                   MIN(fivek_time) AS fivek_time,
                   MIN(tenk_time)  AS tenk_time,
                   COUNT(*)        AS run_count,
                   SUM(stats_json IS NOT NULL) AS gps_count,
                   MIN(run_date)   AS first_date,
                   MAX(run_date)   AS last_date
            FROM runs WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        )
        if not row or not row["run_count"]:
            return None
        return dict(row)

    async def get_distance_stats(self, discord_user_id: str) -> Dict[str, Any]:
        """Total and longest GPS-recorded distance, in one pass over the
        runner's stored stats. Manual entries carry no distance, so this only
        covers uploaded runs."""
        rows = await self._fetchall(
            "SELECT tag, run_date, stats_json FROM runs "
            "WHERE discord_user_id = ? AND stats_json IS NOT NULL",
            (discord_user_id,),
        )
        stats: Dict[str, Any] = {
            "total_km": None, "longest_km": None,
            "longest_miles": None, "longest_date": None, "longest_tag": None,
        }
        total = 0.0
        for row in rows:
            try:
                parsed = json.loads(row["stats_json"])
                dist = parsed.get("total_dist_km")
            except (ValueError, TypeError, AttributeError):
                continue
            if not dist:
                continue
            total += dist
            if stats["longest_km"] is None or dist > stats["longest_km"]:
                stats.update(
                    longest_km=dist,
                    longest_miles=parsed.get("total_dist_miles"),
                    longest_date=row["run_date"],
                    longest_tag=row["tag"],
                )
        stats["total_km"] = total or None
        return stats

    async def count_runs(self, discord_user_id: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM runs WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        return row["n"] if row else 0

    async def get_runs(
        self, discord_user_id: str, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """A page of a runner's history, newest first.

        ``limit=None`` returns every run they've ever logged.
        """
        rows = await self._fetchall(
            """
            SELECT tag, run_date, mile_time, fivek_time, tenk_time, filename,
                   (stats_json IS NOT NULL) AS gps_verified,
                   EXISTS (SELECT 1 FROM run_files f WHERE f.run_id = runs.id) AS has_gpx
            FROM runs
            WHERE discord_user_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (discord_user_id, -1 if limit is None else limit, offset),
        )
        return [dict(row) for row in rows]

    async def _write_best_efforts(self, run_id: int, efforts: List[Dict[str, Any]]) -> None:
        """Replace a run's ladder rows. Caller must hold the lock."""
        await self.conn.execute("DELETE FROM best_efforts WHERE run_id = ?", (run_id,))
        await self.conn.executemany(
            """
            INSERT INTO best_efforts
                (run_id, meters, label, time_s, avg_hr, max_hr, pace_ratio, coverage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (run_id, e["meters"], e.get("label"), e["time_s"], e.get("avg_hr"),
                 e.get("max_hr"), e.get("pace_ratio"), e.get("coverage"))
                for e in efforts
            ],
        )

    async def get_effort_envelope(self, discord_user_id: str) -> List[Dict[str, Any]]:
        """A runner's fastest window at each ladder distance, across all runs.

        Each row carries the effort signals from the run that set it, so a
        later effort gate can judge whether it was a real effort without
        reparsing anything.
        """
        rows = await self._fetchall(
            """
            SELECT b.meters, b.label, b.time_s, b.avg_hr, b.max_hr,
                   b.pace_ratio, b.coverage, r.tag, r.run_date,
                   (SELECT COUNT(*) FROM best_efforts b2
                    JOIN runs r2 ON r2.id = b2.run_id
                    WHERE r2.discord_user_id = r.discord_user_id
                      AND b2.meters = b.meters) AS samples
            FROM best_efforts b
            JOIN runs r ON r.id = b.run_id
            WHERE r.discord_user_id = ?
              AND b.time_s = (
                    SELECT MIN(b3.time_s) FROM best_efforts b3
                    JOIN runs r3 ON r3.id = b3.run_id
                    WHERE r3.discord_user_id = r.discord_user_id
                      AND b3.meters = b.meters
              )
            GROUP BY b.meters
            ORDER BY b.meters
            """,
            (discord_user_id,),
        )
        return [dict(r) for r in rows]

    # -- stored GPX files --------------------------------------------------

    async def get_gpx(self, tag: str) -> Optional[bytes]:
        """The original GPX for a run, or None if it wasn't retained."""
        row = await self._fetchone(
            "SELECT f.gpx_gz FROM run_files f "
            "JOIN runs r ON r.id = f.run_id WHERE r.tag = ?",
            (tag.upper(),),
        )
        return gzip.decompress(row["gpx_gz"]) if row else None

    async def iter_stored_gpx(self) -> List[Dict[str, Any]]:
        """Every retained GPX, for bulk reprocessing.

        Returns decompressed bytes; the caller is expected to parse them off
        the event loop.
        """
        rows = await self._fetchall(
            "SELECT f.run_id, r.tag, f.gpx_gz FROM run_files f "
            "JOIN runs r ON r.id = f.run_id ORDER BY f.run_id"
        )
        out = []
        for row in rows:
            try:
                raw = gzip.decompress(row["gpx_gz"])
            except (OSError, EOFError):
                continue  # corrupt blob — skip rather than abort the batch
            out.append({"run_id": row["run_id"], "tag": row["tag"], "gpx": raw})
        return out

    async def update_run_stats(
        self,
        run_id: int,
        mile_time: Optional[float],
        fivek_time: Optional[float],
        tenk_time: Optional[float],
        stats: Optional[Dict[str, Any]],
    ) -> None:
        """Rewrite a run's derived times from a fresh parse of its GPX."""
        async with self._lock:
            await self.conn.execute(
                """
                UPDATE runs
                SET mile_time = ?, fivek_time = ?, tenk_time = ?, stats_json = ?
                WHERE id = ?
                """,
                (mile_time, fivek_time, tenk_time,
                 json.dumps(stats) if stats else None, run_id),
            )
            if stats and stats.get("best_efforts"):
                await self._write_best_efforts(run_id, stats["best_efforts"])
            await self.conn.commit()

    async def get_storage_stats(self) -> Dict[str, Any]:
        row = await self._fetchone(
            "SELECT COUNT(*) AS files, "
            "       COALESCE(SUM(LENGTH(gpx_gz)), 0) AS stored_bytes, "
            "       COALESCE(SUM(orig_bytes), 0)     AS orig_bytes "
            "FROM run_files"
        )
        total = await self._fetchone("SELECT COUNT(*) AS n FROM runs")
        return {
            "files": row["files"],
            "stored_bytes": row["stored_bytes"],
            "orig_bytes": row["orig_bytes"],
            "total_runs": total["n"] if total else 0,
        }

    async def prune_old_gpx(self, days: int) -> int:
        """Drop stored GPX older than ``days``. The runs themselves stay.

        Retention exists because these files pin down where people live and
        when they're out; derived stats carry no coordinates.
        """
        if days <= 0:
            return 0
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)
        ).strftime("%Y-%m-%d %H:%M:%S")
        async with self._lock:
            cur = await self.conn.execute(
                "DELETE FROM run_files WHERE stored_at < ?", (cutoff,)
            )
            await self.conn.commit()
            return cur.rowcount or 0

    async def get_weekly_runs(self) -> List[Dict[str, Any]]:
        """Returns all runs uploaded in the past 7 days, newest first."""
        rows = await self._fetchall(
            """
            SELECT discord_user_id, discord_username, run_date,
                   mile_time, fivek_time, tenk_time, uploaded_at
            FROM runs
            WHERE uploaded_at >= datetime('now', '-7 days')
            ORDER BY uploaded_at DESC
            """
        )
        return [
            {
                "user_id": r["discord_user_id"],
                "username": r["discord_username"],
                "run_date": r["run_date"],
                "mile_time": r["mile_time"],
                "fivek_time": r["fivek_time"],
                "tenk_time": r["tenk_time"],
                "uploaded_at": r["uploaded_at"],
            }
            for r in rows
        ]
