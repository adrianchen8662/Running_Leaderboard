import os
from typing import Optional, List, Tuple
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "leaderboard.db")


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_user_id TEXT    NOT NULL,
                    discord_username TEXT   NOT NULL,
                    run_date        TEXT,
                    mile_time       REAL,
                    fivek_time      REAL,
                    filename        TEXT,
                    uploaded_at     TEXT    DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

    async def add_run(
        self,
        discord_user_id: str,
        discord_username: str,
        run_date: Optional[str],
        mile_time: Optional[float],
        fivek_time: Optional[float],
        filename: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO runs
                    (discord_user_id, discord_username, run_date,
                     mile_time, fivek_time, filename)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (discord_user_id, discord_username, run_date,
                 mile_time, fivek_time, filename),
            )
            await db.commit()

    async def get_leaderboard(self, event: str) -> List[Tuple[str, float]]:
        """Return (username, best_time) pairs ordered fastest first."""
        col = "mile_time" if event == "mile" else "fivek_time"
        # Use the most-recently uploaded display name for each user
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
                FROM runs
                WHERE discord_user_id = ?
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
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT run_date, mile_time, fivek_time, filename
                FROM runs
                WHERE discord_user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (discord_user_id, limit),
            )
            return await cur.fetchall()
