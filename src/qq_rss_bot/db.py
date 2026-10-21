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
from datetime import datetime, timedelta, timezone
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

-- 每群每天的主动消息计数。落库而不是放内存：
-- 进程重启不能把已用配额清零，否则一天的真实用量会被低估。
CREATE TABLE IF NOT EXISTS send_counters (
    group_openid TEXT    NOT NULL,
    day          TEXT    NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_openid, day)
);

-- 出错时要私聊谁。openid 从群消息的 author.member_openid 里拿，
-- 只有发过指令的人才会被记下来（从没说过话的成员拿不到 openid）。
CREATE TABLE IF NOT EXISTS notify_targets (
    group_openid  TEXT NOT NULL,
    member_openid TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (group_openid, member_openid)
);

-- AI 摘要的缓存。键是 `Entry.key`（guid > link > 标题+时间），
-- **跨订阅共用**：同一篇文章被两个群订到只付一次钱。
-- 落库而不是放内存：`/new` 手动拉取会把同一批文章再走一遍，
-- 进程一重启缓存就没了的话那笔钱等于白花。
CREATE TABLE IF NOT EXISTS ai_cache (
    entry_key  TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    translated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- AI 调用的**全局**每日计数（不是每群 —— 花的是我们自己的钱）。
-- 缓存命中不计数。
CREATE TABLE IF NOT EXISTS ai_counters (
    day   TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);

-- ASOBI TICKET 抽票提醒：哪条受付的哪种提醒**已经发过了**。
-- 主键带 group_openid —— 提醒是逐群记的，将来加群不会因为别的群发过就漏推。
-- 落库而不是放内存：进程一重启「已提醒过」就清零的话，同一条締切会重复推。
--
-- 这张表还有一个副作用：**「表里有没有行」= 「有没有建过 baseline」**，
-- 见 tickets.TicketPoller.poll_once。所以别去删里面的行。
CREATE TABLE IF NOT EXISTS ticket_alerts (
    reception_id TEXT NOT NULL,
    group_openid TEXT NOT NULL,
    kind         TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (reception_id, group_openid, kind)
);

-- 「月曜が近いよ」每周播报发过哪些。**主键是 (计划时刻, 群)** ——
-- 同一个 slot 一个群只发一次，所以窗口内 tick 好几次、或者进程重启，
-- 都不会重发。落库而不是放内存的理由同上：重启不能把「这周发过了」清零。
CREATE TABLE IF NOT EXISTS monday_posts (
    slot         TEXT NOT NULL,
    group_openid TEXT NOT NULL,
    video        TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (slot, group_openid)
);

-- 每日早安语音。主键同样是 (计划时刻, 群) —— 一个 slot 一个群只发一次，
-- 窗口内 tick 好几次、或者进程重启都不会重发。落库而不是放内存，
-- 理由和 monday_posts 一样：重启不能把「今天发过了」清零。
CREATE TABLE IF NOT EXISTS morning_posts (
    slot         TEXT NOT NULL,
    group_openid TEXT NOT NULL,
    idol         TEXT NOT NULL,
    line         TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (slot, group_openid)
);

-- 偶像生日推送。主键是 **(被祝福的那天, 群)**，**不是推送时刻** ——
-- 23:05 推的是第二天的生日（见 birthday.py），按推送时刻记的话
-- 「10-18 晚上推的 10-19 生日」和「10-19 晚上推的 10-19 生日」会分成两行，
-- 而它们本来就是同一天，重试 / 重启时会重复发。
CREATE TABLE IF NOT EXISTS birthday_posts (
    day          TEXT NOT NULL,   -- 被祝福的那天，YYYY-MM-DD
    group_openid TEXT NOT NULL,
    tweet        TEXT NOT NULL,   -- 推文 id；**空串 = 没抓到图，发的是纯文字**
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (day, group_openid)
);

CREATE TABLE IF NOT EXISTS birthday_art_posts (
    day          TEXT NOT NULL,   -- 被庆祝的那天（JST），YYYY-MM-DD
    group_openid TEXT NOT NULL,
    sent         INTEGER NOT NULL,-- 实际发出去几张（一条一张）
    tweets       TEXT NOT NULL,   -- 逗号分隔的推文 id，诊断用
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (day, group_openid)
);

-- 按群的「功能开关」—— 每个群单独决定收不收某个功能的推送。
-- **这是群列表的唯一来源**（`.env` 里没有群参数，`Config` 里也没有 `*_groups`）。
-- 见 features.py 的模块说明：为什么落库、为什么不留半个在 .env 里。
--
-- 没有这一行 = 这个功能对这个群是关的。`set_group_feature()` 用 upsert 写
-- `enabled=0`（保留历史），但**直接删行也是对的** —— 效果一样。
CREATE TABLE IF NOT EXISTS group_features (
    group_openid TEXT NOT NULL,
    feature      TEXT NOT NULL,   -- features.FEATURES 里的名字
    enabled      INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (group_openid, feature)
);
"""

# 老库缺的列。本项目没有成套迁移框架，但「加一列」这类向前兼容的改动
# 用 PRAGMA 检查一下就够了 —— 不写的话，已有的 data/bot.db 会直接报
# no such column。改类型 / 删列仍然要手工处理。
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("subscriptions", "fail_count", "INTEGER NOT NULL DEFAULT 0"),
    # AI 摘要按源开关。**默认 0** —— 这个功能会花钱，必须是显式打开的。
    ("subscriptions", "ai_enabled", "INTEGER NOT NULL DEFAULT 0"),
]


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
    # 连续抓取失败次数。到阈值就自动暂停，免得一个死掉的源
    # 永远每 15 分钟失败一次、把日志和 RSSHub 都拖住。
    fail_count: int = 0
    # 暂停的订阅不参与轮询（due_subscriptions 里已经有 WHERE enabled = 1，
    # 但在此之前项目里没有任何代码能把它设成 0）
    enabled: bool = True
    # 这个源要不要走「抓正文 + AI 翻译/摘要」。**默认关**：会花钱，
    # 而且对 36氪快讯这种「162 字就是全文」的源纯属浪费。
    ai_enabled: bool = False

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
            fail_count=row["fail_count"] if "fail_count" in row.keys() else 0,
            enabled=bool(row["enabled"]),
            ai_enabled=(
                bool(row["ai_enabled"]) if "ai_enabled" in row.keys() else False
            ),
        )


class Database:
    """薄薄一层 sqlite 封装。查询都是微秒级，直接在事件循环里同步调用即可。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """把老库补齐到当前 SCHEMA（只做加列，见 _MIGRATIONS 的说明）。"""
        for table, column, decl in _MIGRATIONS:
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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

    def forget_group(self, group_openid: str) -> int:
        """机器人退群时把该群彻底忘掉，返回删掉的订阅数。

        **必须连 `groups` 那一行一起删。** `known_groups()` 是启动补订阅的依据：
        只删订阅、留着 groups 的话，退群后那张表还记着这个群，
        下次启动会把它当成「零订阅的新群」重新补上订阅，然后轮询器对着一个
        机器人已经不在的群推送 —— 每个周期失败一次，永远不停
        （而且 `fail_count` 只统计**抓取**失败，推送失败不会让它自动暂停）。

        `send_counters` 不动：它按天清理，而且同一天被重新拉进群时
        沿用已用配额反而更安全。
        """
        removed = self._conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE group_openid = ?", (group_openid,)
        ).fetchone()["c"]
        self._conn.execute(
            "DELETE FROM seen_entries WHERE subscription_id IN"
            " (SELECT id FROM subscriptions WHERE group_openid = ?)",
            (group_openid,),
        )
        self._conn.execute("DELETE FROM subscriptions WHERE group_openid = ?", (group_openid,))
        self._conn.execute("DELETE FROM notify_targets WHERE group_openid = ?", (group_openid,))
        self._conn.execute("DELETE FROM groups WHERE group_openid = ?", (group_openid,))
        self._conn.commit()
        return int(removed)

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
    ) -> int:
        """记录一次抓取结果，返回**连续失败次数**（这次成功就归零）。

        调用方靠这个返回值决定要不要自动暂停。
        """
        self._conn.execute(
            """
            UPDATE subscriptions
               SET last_polled_at = ?,
                   last_error     = ?,
                   fail_count     = CASE WHEN ? IS NULL THEN 0 ELSE fail_count + 1 END,
                   title          = COALESCE(?, title),
                   initialized    = CASE WHEN ? THEN 1 ELSE initialized END
             WHERE id = ?
            """,
            (utcnow(), error, error, title, 1 if mark_initialized else 0, sub_id),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT fail_count FROM subscriptions WHERE id = ?", (sub_id,)
        ).fetchone()
        return int(row["fail_count"]) if row else 0

    def set_enabled(self, group_openid: str, sub_id: int, enabled: bool) -> bool:
        """暂停 / 恢复一个订阅。表里本来就有 enabled 列，只是从前没有任何代码能改它。"""
        cur = self._conn.execute(
            "UPDATE subscriptions SET enabled = ? WHERE id = ? AND group_openid = ?",
            (1 if enabled else 0, sub_id, group_openid),
        )
        if enabled:
            # 恢复时把失败计数清零，否则一恢复就立刻又被自动暂停
            self._conn.execute(
                "UPDATE subscriptions SET fail_count = 0 WHERE id = ? AND group_openid = ?",
                (sub_id, group_openid),
            )
        self._conn.commit()
        return cur.rowcount > 0

    def set_interval(self, group_openid: str, sub_id: int, interval_sec: int) -> bool:
        """改间隔，并把 last_polled_at 清空让新间隔**立刻生效**。

        不清的话，从 60min 改成 5min 要等下一次到点（最多 60 分钟）才起作用，
        用户会以为没改成。
        """
        cur = self._conn.execute(
            """
            UPDATE subscriptions
               SET interval_sec = ?, last_polled_at = NULL
             WHERE id = ? AND group_openid = ?
            """,
            (interval_sec, sub_id, group_openid),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def count_subscriptions(self, group_openid: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE group_openid = ?", (group_openid,)
        ).fetchone()
        return int(row["c"])

    # ---------- 主动消息配额 ----------

    def send_count(self, group_openid: str, day: str) -> int:
        row = self._conn.execute(
            "SELECT count FROM send_counters WHERE group_openid = ? AND day = ?",
            (group_openid, day),
        ).fetchone()
        return int(row["count"]) if row else 0

    def bump_send_count(self, group_openid: str, day: str, n: int = 1) -> int:
        self._conn.execute(
            """
            INSERT INTO send_counters (group_openid, day, count) VALUES (?, ?, ?)
            ON CONFLICT (group_openid, day) DO UPDATE SET count = count + excluded.count
            """,
            (group_openid, day, n),
        )
        self._conn.commit()
        return self.send_count(group_openid, day)

    def prune_send_counters(self, keep_days: int = 7) -> None:
        self._conn.execute(
            "DELETE FROM send_counters WHERE day < date('now', ?)",
            (f"-{int(keep_days)} day",),
        )
        self._conn.commit()

    # ---------- AI 摘要 ----------

    def set_subscription_ai(self, sub_id: int, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE subscriptions SET ai_enabled = ? WHERE id = ?",
            (1 if enabled else 0, sub_id),
        )
        self._conn.commit()

    def get_ai_cache(self, entry_key: str):
        """缓存命中返回 `(title, summary, translated)`，没有返回 None。"""
        row = self._conn.execute(
            "SELECT title, summary, translated FROM ai_cache WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()
        if row is None:
            return None
        return (row["title"], row["summary"], bool(row["translated"]))

    def put_ai_cache(
        self, entry_key: str, title: str, summary: str, translated: bool
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO ai_cache (entry_key, title, summary, translated, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (entry_key) DO UPDATE SET
                title = excluded.title,
                summary = excluded.summary,
                translated = excluded.translated
            """,
            (entry_key, title, summary, 1 if translated else 0, utcnow()),
        )
        self._conn.commit()

    def prune_ai_cache(self, keep_days: int = 30) -> None:
        """缓存会一直长。30 天足够覆盖「同一篇文章被两个群先后订到」。"""
        self._conn.execute(
            "DELETE FROM ai_cache WHERE created_at < datetime('now', ?)",
            (f"-{int(keep_days)} day",),
        )
        self._conn.commit()

    def ai_count(self, day: str) -> int:
        row = self._conn.execute(
            "SELECT count FROM ai_counters WHERE day = ?", (day,)
        ).fetchone()
        return int(row["count"]) if row else 0

    def bump_ai_count(self, day: str, n: int = 1) -> int:
        self._conn.execute(
            """
            INSERT INTO ai_counters (day, count) VALUES (?, ?)
            ON CONFLICT (day) DO UPDATE SET count = count + excluded.count
            """,
            (day, n),
        )
        self._conn.commit()
        return self.ai_count(day)

    def prune_ai_counters(self, keep_days: int = 7) -> None:
        self._conn.execute(
            "DELETE FROM ai_counters WHERE day < date('now', ?)",
            (f"-{int(keep_days)} day",),
        )
        self._conn.commit()

    # ---------- 抽票提醒（ASOBI TICKET）----------

    def ticket_alert_kinds(self, group_openid: str) -> dict[str, set[str]]:
        """这个群已经收到过的提醒：`{reception_id: {kind, …}}`。

        一次查完整个群，别在循环里逐条查 —— 31 条受付 × 若干 kind 会变成
        几十次查询。
        """
        rows = self._conn.execute(
            "SELECT reception_id, kind FROM ticket_alerts WHERE group_openid = ?",
            (group_openid,),
        ).fetchall()
        out: dict[str, set[str]] = {}
        for row in rows:
            out.setdefault(row["reception_id"], set()).add(row["kind"])
        return out

    def mark_ticket_alert(self, reception_id: str, group_openid: str, kind: str) -> None:
        """记下「这条提醒发过了」。重复调用无副作用。"""
        self._conn.execute(
            """
            INSERT INTO ticket_alerts (reception_id, group_openid, kind, sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (reception_id, group_openid, kind) DO NOTHING
            """,
            (reception_id, group_openid, kind, utcnow()),
        )
        self._conn.commit()

    # ---------- 月曜が近いよ（每周播报）----------

    def monday_posted(self, slot: str) -> set[str]:
        """这个计划时刻已经发过哪些群。`slot` 形如 `2026-10-18T22:00`。"""
        rows = self._conn.execute(
            "SELECT group_openid FROM monday_posts WHERE slot = ?", (slot,)
        ).fetchall()
        return {row["group_openid"] for row in rows}

    def mark_monday_posted(self, slot: str, group_openid: str, video: str) -> None:
        """记下「这个 slot 发给这个群了」。重复调用无副作用。"""
        self._conn.execute(
            """
            INSERT INTO monday_posts (slot, group_openid, video, sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (slot, group_openid) DO UPDATE SET
                video = excluded.video, sent_at = excluded.sent_at
            """,
            (slot, group_openid, video, utcnow()),
        )
        self._conn.commit()

    def monday_last_video(self) -> str | None:
        """上一次实际发出去的文件名 —— 用来避免连着两周发同一个。

        按 `sent_at` 排，**同时用 rowid 兜底**：同一秒内发的几条
        `sent_at` 完全一样，只按它排顺序是不确定的。
        """
        row = self._conn.execute(
            "SELECT video FROM monday_posts ORDER BY sent_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row["video"] if row else None

    # ---------- 每日早安语音 ----------

    def morning_posted(self, slot: str) -> set[str]:
        """这个计划时刻已经发过哪些群。`slot` 形如 `2026-10-20T07:30`。"""
        rows = self._conn.execute(
            "SELECT group_openid FROM morning_posts WHERE slot = ?", (slot,)
        ).fetchall()
        return {row["group_openid"] for row in rows}

    def mark_morning_posted(
        self, slot: str, group_openid: str, idol: str, line: str
    ) -> None:
        """记下「这个 slot 发给这个群了」。重复调用无副作用。"""
        self._conn.execute(
            """
            INSERT INTO morning_posts (slot, group_openid, idol, line, sent_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (slot, group_openid) DO UPDATE SET
                idol = excluded.idol,
                line = excluded.line,
                sent_at = excluded.sent_at
            """,
            (slot, group_openid, idol, line, utcnow()),
        )
        self._conn.commit()

    def morning_last_line(self) -> str | None:
        """上一次实际发出去的文件名 —— 用来避免连着两天发同一条。

        按 `sent_at` 排，**同时用 rowid 兜底**：同一秒内发的几条
        `sent_at` 完全一样，只按它排顺序是不确定的（同 `monday_last_video`）。
        """
        row = self._conn.execute(
            "SELECT line FROM morning_posts ORDER BY sent_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row["line"] if row else None

    def morning_recent_idols(self, days: int) -> set[str]:
        """最近 `days` 天发过的偶像 id，用来避开重复。

        ⚠️ 按 **`slot` 的字符串前缀**筛，**不用 `sent_at`** —— `sent_at` 是
        UTC、`slot` 是本地时间，跨零点会错一天。`slot` 是 ISO 形态
        （`2026-10-20T07:30`），所以字符串比较就等于时间比较。

        **不管 `days` 是几，都至少带上最近那一次的那位** —— 否则
        `MORNING_RECENT_DAYS=0` 时明天还会发同一个人，看起来像随机坏了。
        """
        days = max(days, 0)
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") + "T00:00"
        rows = self._conn.execute(
            "SELECT idol FROM morning_posts WHERE slot >= ?", (cutoff,)
        ).fetchall()
        out = {row["idol"] for row in rows}
        last = self._conn.execute(
            "SELECT idol FROM morning_posts ORDER BY sent_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if last:
            out.add(last["idol"])
        return out

    # ---------- 偶像生日推送 ----------

    def birthday_posted(self, day: str) -> set[str]:
        """这一天已经发过哪些群。`day` 是**被祝福的那天**，形如 `2026-10-19`。"""
        rows = self._conn.execute(
            "SELECT group_openid FROM birthday_posts WHERE day = ?", (day,)
        ).fetchall()
        return {row["group_openid"] for row in rows}

    def mark_birthday_posted(self, day: str, group_openid: str, tweet: str) -> None:
        """记下「这一天的生日推发给这个群了」。重复调用无副作用。

        `tweet` 传空串表示**这条是没配图发的**，不是漏填 —— 排查时能分清
        「当时没抓到图」和「当时压根没记」。
        """
        self._conn.execute(
            """
            INSERT INTO birthday_posts (day, group_openid, tweet, sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (day, group_openid) DO UPDATE SET
                tweet = excluded.tweet,
                sent_at = excluded.sent_at
            """,
            (day, group_openid, tweet, utcnow()),
        )
        self._conn.commit()

    def birthday_art_posted(self, day: str) -> set[str]:
        """这一天已经发过贺图的群。`day` 是**被庆祝的那天**（JST），形如 `2026-10-19`。

        ⚠️ 键是**被庆祝的那天**，不是推送时刻 —— 和 `birthday_posts` 同一个理由：
        重试 / 重启 / 跨零点都不会重复发。
        """
        rows = self._conn.execute(
            "SELECT group_openid FROM birthday_art_posts WHERE day = ?", (day,)
        ).fetchall()
        return {row["group_openid"] for row in rows}

    def clear_birthday_art(self, day: str, group_openid: str) -> bool:
        """删掉「这一天发给这个群」的记录，返回**原来有没有**。

        **只给探针重测用** —— 生产代码永远不该调它。记账的意义就是「同一天
        同一个群不重复发」，删了就等于允许重发。`scripts/probe_birthday_art.py`
        的 `--reset` 走这条路：改完挑选规则想拿同一天重测，不清账就会被
        `poll_once` 直接挡回来（返回 0，看起来像「什么都没发」）。
        """
        cur = self._conn.execute(
            "DELETE FROM birthday_art_posts WHERE day = ? AND group_openid = ?",
            (day, group_openid),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def mark_birthday_art(
        self, day: str, group_openid: str, sent: int, tweets: str
    ) -> None:
        """记下「这一天的贺图发给这个群了」。

        `sent` 是**真的发出去了几张**（中间某张挂掉时会小于总数），
        `tweets` 是那几张的推文 id —— 排查「今天为什么只有 3 张」时要靠它。
        """
        self._conn.execute(
            """
            INSERT INTO birthday_art_posts (day, group_openid, sent, tweets, sent_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (day, group_openid) DO UPDATE SET
                sent = excluded.sent,
                tweets = excluded.tweets,
                sent_at = excluded.sent_at
            """,
            (day, group_openid, sent, tweets, utcnow()),
        )
        self._conn.commit()

    # ---------- 出错时私聊谁 ----------

    def set_notify_target(self, group_openid: str, member_openid: str) -> None:
        if not member_openid:
            return
        self._conn.execute(
            """
            INSERT INTO notify_targets (group_openid, member_openid, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (group_openid, member_openid) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (group_openid, member_openid, utcnow()),
        )
        self._conn.commit()

    def notify_targets(self, group_openid: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT member_openid FROM notify_targets WHERE group_openid = ?"
            " ORDER BY updated_at DESC",
            (group_openid,),
        ).fetchall()
        return [r["member_openid"] for r in rows]

    def remove_notify_target(self, group_openid: str, member_openid: str) -> None:
        self._conn.execute(
            "DELETE FROM notify_targets WHERE group_openid = ? AND member_openid = ?",
            (group_openid, member_openid),
        )
        self._conn.commit()

    # ---------- 条目去重 ----------

    def seen_keys(self, sub_id: int, entry_keys: Sequence[str]) -> set[str]:
        """只查哪些 key 已经见过，**不写入**。

        与 filter_new_entries 的区别很重要：推送要在「标记已见」之前发生，
        否则推送失败时条目已被记为见过，就永远不会再推了（丢消息）。
        """
        if not entry_keys:
            return set()
        marks = ",".join("?" * len(entry_keys))
        rows = self._conn.execute(
            f"SELECT entry_key FROM seen_entries"
            f" WHERE subscription_id = ? AND entry_key IN ({marks})",
            (sub_id, *entry_keys),
        ).fetchall()
        return {r["entry_key"] for r in rows}

    def mark_seen(self, sub_id: int, entry_keys: Sequence[str]) -> None:
        """登记为已见。推送成功之后才该调用。"""
        if not entry_keys:
            return
        now = utcnow()
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen_entries (subscription_id, entry_key, seen_at)"
            " VALUES (?, ?, ?)",
            [(sub_id, key, now) for key in entry_keys],
        )
        self._conn.commit()

    def filter_new_entries(
        self, sub_id: int, entry_keys: Sequence[str]
    ) -> list[str]:
        """返回其中从未见过的 key，并**立即**把它们登记为已见。

        适用于「不打算推送、只想标记」的场景（首抓建 baseline、/check 手动抓取）。
        推送路径请改用 seen_keys + mark_seen，见上面 seen_keys 的说明。
        """
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
            "paused": self._conn.execute(
                "SELECT COUNT(*) c FROM subscriptions WHERE enabled = 0"
            ).fetchone()["c"],
        }

    # ---------- 按群的功能开关（见 features.py）----------

    def feature_groups(self, feature: str) -> tuple[str, ...]:
        """这个功能**开着**的群。

        ⚠️ `ORDER BY rowid` 是**故意**的，不是随手写的：写库时按顺序插入，
        rowid 就保住了那个顺序。`birthday_art._alert` 取的是 `[:1]`（第一个群），
        而 `C2CNotifier` 拿这个群去查「谁在这个群发过指令」——
        **换个群就是换了一批私聊收件人**。
        """
        rows = self._conn.execute(
            "SELECT group_openid FROM group_features "
            "WHERE feature = ? AND enabled = 1 ORDER BY rowid",
            (feature,),
        ).fetchall()
        return tuple(r["group_openid"] for r in rows)

    def set_group_feature(self, group_openid: str, feature: str, enabled: bool) -> None:
        """开 / 关某个群的一个功能。upsert —— 关掉写 `enabled=0`，保留 `updated_at`。

        （直接删行效果一样，但留着 0 能看出「这个群曾经开过」。）
        """
        self._conn.execute(
            """
            INSERT INTO group_features (group_openid, feature, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (group_openid, feature) DO UPDATE SET
                enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (group_openid, feature, 1 if enabled else 0, utcnow()),
        )
        self._conn.commit()

    def all_group_features(self) -> dict[str, dict[str, bool]]:
        """`{群: {功能: 开没开}}`。`/set` 列状态用，一次查完别逐群查。"""
        out: dict[str, dict[str, bool]] = {}
        for row in self._conn.execute(
            "SELECT group_openid, feature, enabled FROM group_features ORDER BY rowid"
        ):
            out.setdefault(row["group_openid"], {})[row["feature"]] = bool(
                row["enabled"]
            )
        return out
