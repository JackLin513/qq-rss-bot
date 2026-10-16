"""出错时私聊管理员，而不是在群里报错。

为什么走私聊：群里报错对其他人是噪音，而且**要占主动消息配额** ——
配额本来就要省着用（一个源一次就能吃掉 5 条）。

收件人 openid 从群消息的 `author.member_openid` 拿，只有**发过指令**的人
才会有记录。**实测（2026-10-16）这条路是通的**：从群消息里抠出来的 openid
可以直接用来发**主动** C2C 消息（不带 msg_id），HTTP 200，不需要对方
先私聊过机器人。所以「在群里用过一次指令」就等于把私聊通道打通了。

**收件人有两种模式**（见 `C2CNotifier.targets`）：

* 配了 `NOTIFY_OPENIDS` → **白名单模式**，只发给名单上的人，完全不查数据库。
  这是「只发给我」的唯一可靠做法：自动登记收的是**群主 + 所有管理员**，
  群里一旦有别的管理员，他们也会收到你的出错通知。
* 没配 → 自动登记模式，收件人 = 在该群发过指令的**群主 + 管理员**（最多 3 人）。
  普通成员不会被登记，所以不会收到私聊。

即便如此，发送失败仍然只记日志、**绝不回落到群消息** ——
宁可少一条通知，也不能把错误刷到群里去。

两个防刷屏机制（RSSHub 一挂就是所有源一起挂，不做限制会变成 DM 风暴）：

* 同 `key` 的消息在 `cooldown_sec` 内只发一次；
* 不同消息之间也留一个最小间隔，避免瞬时打满用户维度的频控。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Protocol

from .db import Database

_log = logging.getLogger("qq_rss_bot.notify")

# 同一类错误 30 分钟只提醒一次
DEFAULT_COOLDOWN_SEC = 1800.0
# 不同通知之间的最小间隔（C2C 也有自己的频控）
DEFAULT_MIN_GAP_SEC = 1.0
# 一个群最多通知几个人，防止 notify_targets 膨胀后一次发一堆
MAX_TARGETS = 3


class C2CSender(Protocol):
    """发私聊消息的能力。RssBot 实现它；测试里换成假的。"""

    async def send_c2c_text(self, openid: str, content: str) -> None: ...


class Notifier(Protocol):
    async def notify(self, group_openid: str, text: str, *, key: str = "") -> bool: ...


class NullNotifier:
    """什么都不做的实现，给测试和「关掉了通知」用。"""

    async def notify(self, group_openid: str, text: str, *, key: str = "") -> bool:
        return False


class C2CNotifier:
    def __init__(
        self,
        sender: C2CSender,
        db: Database,
        *,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        min_gap_sec: float = DEFAULT_MIN_GAP_SEC,
        allowlist: Sequence[str] = (),
    ) -> None:
        self.sender = sender
        self.db = db
        self.cooldown_sec = cooldown_sec
        self.min_gap_sec = min_gap_sec
        # 非空 = 白名单模式：数据库里登记的收件人一律忽略
        self.allowlist: tuple[str, ...] = tuple(o for o in allowlist if o)
        if len(self.allowlist) > MAX_TARGETS:
            _log.warning(
                "NOTIFY_OPENIDS 配了 %d 个收件人，只取前 %d 个（被丢掉的：%s）",
                len(self.allowlist),
                MAX_TARGETS,
                [o[:8] for o in self.allowlist[MAX_TARGETS:]],
            )
            self.allowlist = self.allowlist[:MAX_TARGETS]
        self._last_by_key: dict[tuple[str, str], float] = {}
        self._last_any: float = 0.0
        self._lock = asyncio.Lock()

    def targets(self, group_openid: str) -> list[str]:
        """这条通知发给谁。

        白名单模式**完全不碰数据库** —— 收件人一眼能从 .env 看出来，
        也不会因为谁被撤了管理员而悄悄变化。
        """
        if self.allowlist:
            return list(self.allowlist)
        return self.db.notify_targets(group_openid)[:MAX_TARGETS]

    def is_target(self, group_openid: str, openid: str) -> bool:
        """某人是否会收到这个群的通知。私聊回执里用来告诉对方「你能不能收到」。"""
        if not openid:
            return False
        if self.allowlist:
            return openid in self.allowlist
        return openid in self.db.notify_targets(group_openid)

    def _cooled_down(self, group_openid: str, key: str, now: float) -> bool:
        if not key:
            return False
        last = self._last_by_key.get((group_openid, key))
        return last is not None and (now - last) < self.cooldown_sec

    async def notify(self, group_openid: str, text: str, *, key: str = "") -> bool:
        """私聊通知。返回是否**至少送达一人**。

        任何失败都只记日志 —— 通知发不出去不该影响主流程。
        """
        targets = self.targets(group_openid)
        if not targets:
            _log.info("群 %s 没有可通知的对象，跳过：%s", group_openid, text.splitlines()[0])
            return False

        async with self._lock:
            now = time.monotonic()
            if self._cooled_down(group_openid, key, now):
                _log.debug("同类通知在冷却期内，跳过 key=%s", key)
                return False
            wait = self.min_gap_sec - (now - self._last_any)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_any = time.monotonic()
            if key:
                self._last_by_key[(group_openid, key)] = self._last_any

        sent = 0
        for openid in targets:
            try:
                await self.sender.send_c2c_text(openid, text)
                sent += 1
            except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
                # 常见原因是「该用户还没和机器人建立会话」，只能记日志等人来私聊一次
                _log.warning("私聊通知失败 openid=%s: %r", openid[:8], exc)
        # **成功也留痕**。之前只有失败才打日志，于是「通知到底发没发出去」
        # 在群里和日志里都看不到任何迹象 —— 收件人说没收到，你无从判断是
        # 没发、发失败、还是他没看。这行是唯一的客观依据。
        head = text.splitlines()[0] if text else ""
        if sent:
            _log.info("私聊通知已送达 %d/%d 人：%s", sent, len(targets), head)
        else:
            _log.warning("私聊通知全部失败（%d 人）：%s", len(targets), head)
        return sent > 0
