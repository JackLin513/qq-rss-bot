"""主动消息的节流与配额。

**为什么必须在 Pusher 外面再包一层**：`scheduler` 用信号量并发跑最多 5 个源的
抓取循环，每个循环内部有自己的 `_PUSH_GAP_SEC` 间隔 —— 但那 5 个循环是**同时**
在跑的，间隔形同虚设。实测 5 个源同时有新内容时，25 条消息全部落在 1.6 秒内，
而群维度频控是 20 条/分钟。

两层保护，管的是两件不同的事：

* **令牌桶**（每分钟）—— 超了就**排队等**，消息一条不丢，只是慢一点。
  官方频控是滑动窗口，所以用桶而不是「每条 sleep N 秒」：
  桶允许一次突发用满 20 个令牌，之后按 20/分钟匀速回填。
* **每日配额**（落库）—— 超了就**拒发**并抛 `QuotaExceeded`，让调用方降级。
  配额放内存不行：进程一重启就清零，真实用量会被低估。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Protocol

from .db import Database

_log = logging.getLogger("qq_rss_bot.quota")

# 官方频控：群维度 20 条/分钟、每群每天 1000 条。
# 每日留一点余量 —— 回执、提示这类消息也要走同一个池子。
DEFAULT_PER_MINUTE = 20
DEFAULT_DAILY_LIMIT = 900


class Pusher(Protocol):
    """发送出口。RssBot 实现它，`GroupSender` 包装它，测试里换成假的。

    放在这里而不是 scheduler：scheduler 需要 `QuotaExceeded`，如果 quota 反过来
    从 scheduler 导入这个协议就成了循环导入。
    """

    async def send_text(self, group_openid: str, content: str) -> None: ...

    async def send_rich(self, group_openid: str, content: str, image_url: str) -> None: ...

    async def send_markdown(self, group_openid: str, content: str) -> None: ...

    async def upload_image(self, group_openid: str, image_url: str) -> bool: ...


class QuotaExceeded(RuntimeError):
    """今天的主动消息配额用完了。调用方应当降级，而不是重试。"""

    def __init__(self, group_openid: str, used: int, limit: int) -> None:
        super().__init__(f"群 {group_openid} 今日已发 {used}/{limit} 条")
        self.group_openid = group_openid
        self.used = used
        self.limit = limit


def today() -> str:
    """按**本地日期**算配额日 —— QQ 的配额在本地零点重置，不是 UTC 零点。"""
    return datetime.now().strftime("%Y-%m-%d")


class _Bucket:
    """令牌桶。容量 = 每分钟上限，按同一速率回填。"""

    def __init__(self, capacity: int, per_second: float) -> None:
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.rate = per_second
        self.updated = time.monotonic()

    async def take(self) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep((1 - self.tokens) / self.rate)


class GroupSender:
    """`Pusher` 的包装：群维度串行 + 令牌桶 + 每日配额。

    群维度一把锁，所以对同一个群来说，消息是**严格按调用顺序**发出的 ——
    顺带把「并发发送导致到达顺序错乱」也解决了。
    """

    def __init__(
        self,
        pusher: Pusher,
        db: Database,
        *,
        per_minute: int = DEFAULT_PER_MINUTE,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
    ) -> None:
        self.pusher = pusher
        self.db = db
        self.per_minute = per_minute
        self.daily_limit = daily_limit
        self._locks: dict[str, asyncio.Lock] = {}
        self._buckets: dict[str, _Bucket] = {}
        # 只用于观测/测试，不作为判断依据（判断一律回查数据库）
        self.sent_today: dict[str, int] = {}

    def _lock(self, group_openid: str) -> asyncio.Lock:
        lock = self._locks.get(group_openid)
        if lock is None:
            lock = self._locks[group_openid] = asyncio.Lock()
        return lock

    def _bucket(self, group_openid: str) -> _Bucket:
        bucket = self._buckets.get(group_openid)
        if bucket is None:
            bucket = self._buckets[group_openid] = _Bucket(
                self.per_minute, self.per_minute / 60.0
            )
        return bucket

    # ---------- 配额查询（给指令层用，只读）----------

    def used_today(self, group_openid: str) -> int:
        return self.db.send_count(group_openid, today())

    def remaining_today(self, group_openid: str) -> int:
        return max(0, self.daily_limit - self.used_today(group_openid))

    def exhausted(self, group_openid: str) -> bool:
        return self.remaining_today(group_openid) <= 0

    # ---------- 发送 ----------

    async def _send(self, group_openid: str, call) -> None:
        """拿锁 → 查配额 → 取令牌 → 真发。

        配额在锁内查，否则并发调用会各自读到同一个旧值、一起挤过上限。
        """
        async with self._lock(group_openid):
            used = self.used_today(group_openid)
            if used >= self.daily_limit:
                raise QuotaExceeded(group_openid, used, self.daily_limit)
            await self._bucket(group_openid).take()
            await call()
            total = self.db.bump_send_count(group_openid, today())
            self.sent_today[group_openid] = total
            if total == self.daily_limit:
                _log.warning("群 %s 今日主动消息配额已用尽（%d 条）", group_openid, total)

    async def send_text(self, group_openid: str, content: str) -> None:
        await self._send(group_openid, lambda: self.pusher.send_text(group_openid, content))

    async def send_rich(self, group_openid: str, content: str, image_url: str) -> None:
        await self._send(
            group_openid, lambda: self.pusher.send_rich(group_openid, content, image_url)
        )

    async def send_markdown(self, group_openid: str, content: str) -> None:
        await self._send(
            group_openid, lambda: self.pusher.send_markdown(group_openid, content)
        )

    async def upload_image(self, group_openid: str, image_url: str) -> bool:
        """只让 QQ 去抓图、拿 `file_info`，**不发消息**。

        所以它**不占配额、也不进令牌桶**（官方频控管的是消息，不是上传），
        当然也不该拿群锁 —— 上传之间没有顺序要求。

        多图那条路用它提前查一次「QQ 到底抓不抓得到」：markdown 的图裂了是
        **静默**的（发送接口照样 HTTP 200，没有任何回执），事后无从发现，
        只能事前查。
        """
        return await self.pusher.upload_image(group_openid, image_url)
