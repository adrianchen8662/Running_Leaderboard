import json
import os
import random
from typing import Optional, List, Tuple, Dict, Any
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "leaderboard.db")

# Unambiguous alphanumeric chars — no O/0, I/1, L
_TAG_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_TAG_LEN = 5


def _random_tag() -> str:
    return "".join(random.choices(_TAG_CHARS, k=_TAG_LEN))


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag              TEXT    UNIQUE,
                    discord_user_id  TEXT    NOT NULL,
                    discord_username TEXT    NOT NULL,
                    run_date         TEXT,
                    mile_time        REAL,
                    fivek_time       REAL,
                    filename         TEXT,
                    stats_json       TEXT,
                    uploaded_at      TEXT    DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for col in ("stats_json", "tag"):
                try:
                    await db.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
                except Exception:
                    pass  # already exists
            # Unique index must be created separately — SQLite forbids UNIQUE
            # constraints in ALTER TABLE ADD COLUMN
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_tag ON runs (tag)"
            )
            await db.commit()

    async def _unique_tag(self, db) -> str:
        """Generate a tag that doesn't already exist in the DB."""
        for _ in range(20):
            tag = _random_tag()
            cur = await db.execute("SELECT 1 FROM runs WHERE tag = ?", (tag,))
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
        stats: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert a run and return its unique tag."""
        async with aiosqlite.connect(self.path) as db:
            tag = await self._unique_tag(db)
            await db.execute(
                """
                INSERT INTO runs
                    (tag, discord_user_id, discord_username, run_date,
                     mile_time, fivek_time, filename, stats_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tag, discord_user_id, discord_username, run_date,
                    mile_time, fivek_time, filename,
                    json.dumps(stats) if stats else None,
                ),
            )
            await db.commit()
            return tag

    async def get_run_by_tag(self, tag: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT discord_user_id, discord_username, stats_json, filename
                FROM runs WHERE tag = ?
                """,
                (tag.upper(),),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "user_id":  row[0],
                "username": row[1],
                "stats":    json.loads(row[2]) if row[2] else None,
                "filename": row[3],
            }

    async def delete_run_by_tag(self, tag: str) -> str:
        """
        Delete a run by tag.  Returns:
          'deleted'    — success
          'not_found'  — tag doesn't exist
        """
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT discord_user_id FROM runs WHERE tag = ?", (tag.upper(),)
            )
            row = await cur.fetchone()
            if not row:
                return "not_found"
            await db.execute("DELETE FROM runs WHERE tag = ?", (tag.upper(),))
            await db.commit()
            return "deleted"

    async def get_leaderboard(self, event: str) -> List[Tuple[str, float]]:
        col = "mile_time" if event == "mile" else "fivek_time"
        query = f"""
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
            LIMIT 20
        """
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(query)
            return await cur.fetchall()

    async def get_personal_bests(self, discord_user_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT MIN(mile_time), MIN(fivek_time), COUNT(*)
                FROM runs WHERE discord_user_id = ?
                """,
                (discord_user_id,),
            )
            row = await cur.fetchone()
            if not row or row[2] == 0:
                return None
            return {"mile_time": row[0], "fivek_time": row[1], "run_count": row[2]}

    async def get_recent_runs(
        self, discord_user_id: str, limit: int = 5
    ) -> List[tuple]:
        """Returns (tag, run_date, mile_time, fivek_time, filename, gps_verified) tuples."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT tag, run_date, mile_time, fivek_time, filename,
                       (stats_json IS NOT NULL) AS gps_verified
                FROM runs
                WHERE discord_user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (discord_user_id, limit),
            )
            return await cur.fetchall()

    async def get_weekly_runs(self) -> List[Dict[str, Any]]:
        """Returns all runs uploaded in the past 7 days, newest first."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT discord_user_id, discord_username, run_date,
                       mile_time, fivek_time, uploaded_at
                FROM runs
                WHERE uploaded_at >= datetime('now', '-7 days')
                ORDER BY uploaded_at DESC
                """
            )
            rows = await cur.fetchall()
        return [
            {
                "user_id":   r[0],
                "username":  r[1],
                "run_date":  r[2],
                "mile_time": r[3],
                "fivek_time": r[4],
                "uploaded_at": r[5],
            }
            for r in rows
        ]
