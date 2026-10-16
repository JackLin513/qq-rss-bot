"""SQLite 存储层。

两张表的分工：
  subscriptions —— 谁（group_openid）订了什么（url）
  seen_entries  —— 每个订阅已经见过哪些条目，用来去重

去重靠 entries 的主键（fallback 到 link / 标题+时间），
这样即使上游 RSS 没有稳定的 guid 也不会重复推送。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    group_openid  TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid   TEXT    NOT NULL,
    url            TEXT    NOT NULL,
    title          TEXT,
    interval_sec   INTEGER NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    initialized    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL,
    last_polled_at TEXT,
    last_error     TEXT,
    UNIQUE (group_openid, url)
);

CREATE TABLE IF NOT EXISTS seen_entries (
    subscription_id INTEGER NOT NULL,
    entry_key       TEXT    NOT NULL,
    seen_at         TEXT    NOT NULL,
    PRIMARY KEY (subscription_id, entry_key)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Subscription:
    id: int
    group_openid: str
    url: str
    title: str | None
    interval_sec: int
    initialized: bool
    last_polled_at: str | None
    last_error: str | None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> "Subscription":
        return Subscription(
            id=row["id"],
            group_openid=row["group_openid"],
            url=row["url"],
            title=row["title"],
            interval_sec=row["interval_sec"],
            initialized=bool(row["initialized"]),
            last_polled_at=row["last_polled_at"],
            last_error=row["last_error"],
        )


class Database:
    """薄薄一层 sqlite 封装。查询都是微秒级，直接在事件循环里同步调用即可。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- 群 ----------

    def touch_group(self, group_openid: str) -> None:
        """记录机器人见过的群。主动推送必须有 group_openid，而它只能从事件里拿。"""
        now = utcnow()
        self._conn.execute(
            """
            INSERT INTO groups (group_openid, first_seen_at, last_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT (group_openid) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (group_openid, now, now),
        )
        self._conn.commit()

    def known_groups(self) -> list[str]:
        rows = self._conn.execute("SELECT group_openid FROM groups").fetchall()
        return [r["group_openid"] for r in rows]

    # ---------- 订阅 ----------

    def add_subscription(
        self, group_openid: str, url: str, title: str | None, interval_sec: int
    ) -> tuple[int, bool]:
        """返回 (subscription_id, 是否新建)。已存在时返回原有的 id。"""
        cur = self._conn.execute(
            """
            INSERT INTO subscriptions (group_openid, url, title, interval_sec, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (group_openid, url) DO NOTHING
            """,
            (group_openid, url, title, interval_sec, utcnow()),
        )
        self._conn.commit()
        created = cur.rowcount > 0
        if created:
            return int(cur.lastrowid), True
        row = self._conn.execute(
            "SELECT id FROM subscriptions WHERE group_openid = ? AND url = ?",
            (group_openid, url),
        ).fetchone()
        return int(row["id"]), False

    def remove_subscription(self, group_openid: str, sub_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM subscriptions WHERE id = ? AND group_openid = ?",
            (sub_id, group_openid),
        )
        self._conn.execute("DELETE FROM seen_entries WHERE subscription_id = ?", (sub_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def list_subscriptions(self, group_openid: str) -> list[Subscription]:
        rows = self._conn.execute(
            "SELECT * FROM subscriptions WHERE group_openid = ? ORDER BY id",
            (group_openid,),
        ).fetchall()
        return [Subscription._from_row(r) for r in rows]

    def due_subscriptions(self, now: str) -> list[Subscription]:
        """所有到点该抓的订阅（含从未抓过的）。"""
        rows = self._conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE enabled = 1
              AND (
                last_polled_at IS NULL
                OR strftime('%s', ?) - strftime('%s', last_polled_at) >= interval_sec
              )
            """,
            (now,),
        ).fetchall()
        return [Subscription._from_row(r) for r in rows]

    def mark_polled(
        self, sub_id: int, *, title: str | None = None, error: str | None = None,
        mark_initialized: bool = False,
    ) -> None:
        self._conn.execute(
            """
            UPDATE subscriptions
               SET last_polled_at = ?,
                   last_error     = ?,
                   title          = COALESCE(?, title),
                   initialized    = CASE WHEN ? THEN 1 ELSE initialized END
             WHERE id = ?
            """,
            (utcnow(), error, title, 1 if mark_initialized else 0, sub_id),
        )
        self._conn.commit()

    # ---------- 条目去重 ----------

    def filter_new_entries(
        self, sub_id: int, entry_keys: Sequence[str]
    ) -> list[str]:
        """返回其中从未见过的 key，并把它们登记为已见。"""
        fresh: list[str] = []
        now = utcnow()
        for key in entry_keys:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen_entries (subscription_id, entry_key, seen_at)"
                " VALUES (?, ?, ?)",
                (sub_id, key, now),
            )
            if cur.rowcount > 0:
                fresh.append(key)
        self._conn.commit()
        return fresh

    def prune_seen(self, sub_id: int, keep: int = 500) -> None:
        """只保留最近 keep 条记录，防止 seen_entries 无限增长。"""
        self._conn.execute(
            """
            DELETE FROM seen_entries
             WHERE subscription_id = ?
               AND entry_key NOT IN (
                   SELECT entry_key FROM seen_entries
                    WHERE subscription_id = ?
                    ORDER BY seen_at DESC
                    LIMIT ?
               )
            """,
            (sub_id, sub_id, keep),
        )
        self._conn.commit()

    def stats(self) -> dict[str, int]:
        return {
            "groups": self._conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"],
            "subscriptions": self._conn.execute(
                "SELECT COUNT(*) c FROM subscriptions"
            ).fetchone()["c"],
            "seen_entries": self._conn.execute(
                "SELECT COUNT(*) c FROM seen_entries"
            ).fetchone()["c"],
        }
