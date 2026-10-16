"""定时轮询 + 增量推送。

调度策略很朴素：每 TICK_SEC 醒一次，问数据库「哪些订阅到点了」。
好处是间隔完全由数据决定，进程重启不需要重建任何定时任务。

推送形态：**每条新条目单独发一条消息**。
之所以不做「多条合并成一条摘要」，是因为一条图文消息只能带一张图
（官方 media 字段只接受单个对象，传数组会 400），合并摘要就带不了图。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Protocol

import httpx

from .config import Config
from .db import Database, Subscription, utcnow
from .rss import Entry, fetch_feed

_log = logging.getLogger("qq_rss_bot.scheduler")

# QQ 单条文本消息有长度上限，留足余量
_MAX_CONTENT = 1500
# 逐条连发时留一点间隔：一是避免撞上群维度 20 qpm 的频控，
# 二是让客户端收到的顺序稳定（并发发出时到达顺序不保证）
_PUSH_GAP_SEC = 0.4


class Pusher(Protocol):
    """推送出口。RssBot 实现它；测试里用假的实现，不碰 QQ。"""

    async def send_text(self, group_openid: str, content: str) -> None: ...

    async def send_rich(self, group_openid: str, content: str, image_url: str) -> None: ...


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_CONTENT else text[: _MAX_CONTENT - 1] + "…"


def format_entry(feed_title: str | None, url: str, entry: Entry) -> str:
    """单条新闻的推送文案：源名 + 标题 + 链接。"""
    lines = [f"📰 {feed_title or url}", f"· {entry.title}"]
    if entry.link:
        lines.append(f"  {entry.link}")
    return _truncate("\n".join(lines))


class FeedPoller:
    def __init__(self, cfg: Config, db: Database, http: httpx.AsyncClient, pusher: Pusher) -> None:
        self.cfg = cfg
        self.db = db
        self.http = http
        self.pusher = pusher
        self._sem = asyncio.Semaphore(5)
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        _log.info("轮询启动，每 %ss 扫描一次", self.cfg.tick_sec)
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("单轮轮询异常，跳过本轮")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.cfg.tick_sec)
            except asyncio.TimeoutError:
                pass
        _log.info("轮询已停止")

    async def poll_once(self) -> None:
        due = self.db.due_subscriptions(utcnow())
        if not due:
            return
        _log.debug("本轮待抓取 %d 个订阅", len(due))
        results = await asyncio.gather(
            *(self._poll_one(sub) for sub in due), return_exceptions=True
        )
        # 必须检查返回值：return_exceptions=True 会把异常收进结果里静默丢弃，
        # 不检查的话推送失败连一行日志都没有。
        for sub, result in zip(due, results):
            if isinstance(result, BaseException):
                _log.error(
                    "订阅 #%s (%s) 本轮处理失败", sub.id, sub.url, exc_info=result
                )

    async def _poll_one(self, sub: Subscription) -> None:
        async with self._sem:
            result = await fetch_feed(self.http, sub.url, self.cfg.fetch_timeout_sec)

            if not result.ok:
                # 失败的订阅也更新 last_polled_at，等于退避一个间隔再重试
                self.db.mark_polled(sub.id, error=result.error)
                _log.warning("抓取失败 #%s %s: %s", sub.id, sub.url, result.error)
                return

            keys = [e.key for e in result.entries]

            if not sub.initialized:
                # 首抓只建立 baseline，绝不推送，否则一订阅就倒灌几十条旧文
                self.db.filter_new_entries(sub.id, keys)
                self.db.mark_polled(sub.id, title=result.title, mark_initialized=True)
                _log.info("订阅 #%s 建立 baseline，%d 条", sub.id, len(keys))
                return

            # 先只查、不写。推送成功之后才标记已见 ——
            # 顺序反了的话，推送失败时条目已被记为见过，就永远不会再推（静默丢消息）
            already = self.db.seen_keys(sub.id, keys)
            fresh = [e for e in result.entries if e.key not in already]
            self.db.mark_polled(sub.id, title=result.title)
            self.db.prune_seen(sub.id)

            if not fresh:
                return

            shown = fresh[: self.cfg.max_items_per_push]
            pushed = await self._push_entries(sub, result.title, shown)

            # 超出上限的那些是「有意丢弃」，直接标记已见，免得下轮反复捞
            dropped = [e.key for e in fresh[self.cfg.max_items_per_push :]]
            if dropped:
                self.db.mark_seen(sub.id, dropped)
            # 只有真正推成功的才标记已见；失败的留到下轮重试
            self.db.mark_seen(sub.id, [e.key for e in pushed])

            overdue = len(fresh) - len(shown)
            if overdue > 0:
                try:
                    await self.pusher.send_text(
                        sub.group_openid, f"（另有 {overdue} 条更新未展示）"
                    )
                except Exception as exc:
                    # 只是提示，失败不影响正文，不该让它冒泡打断本轮
                    _log.warning("「另有 N 条」提示发送失败: %s", exc)

            failed = len(shown) - len(pushed)
            _log.info(
                "推送 #%s → %s，%d 条（配图 %d）%s",
                sub.id,
                sub.group_openid,
                len(pushed),
                sum(1 for e in pushed if e.image),
                f"，{failed} 条失败待下轮重试" if failed else "",
            )

    async def _push_entries(
        self, sub: Subscription, feed_title: str | None, entries: list[Entry]
    ) -> list[Entry]:
        """逐条推送，返回**成功**的那些。

        单条失败不能中断整批（否则它后面的条目白丢），也不标记已见，
        留给下一轮重试。
        """
        pushed: list[Entry] = []
        for index, entry in enumerate(entries):
            if index:
                await asyncio.sleep(_PUSH_GAP_SEC)
            text = format_entry(feed_title, sub.url, entry)

            if self.cfg.attach_images and entry.image:
                try:
                    await self.pusher.send_rich(sub.group_openid, text, entry.image)
                    pushed.append(entry)
                    continue
                except Exception as exc:
                    # 配图失败（QQ 拉不到图 / 图太大 / 格式不支持）不该让整条新闻丢失，
                    # 退回纯文字再发一次
                    _log.warning("配图推送失败，退回纯文字 (%s): %s", entry.image, exc)

            try:
                await self.pusher.send_text(sub.group_openid, text)
                pushed.append(entry)
            except Exception as exc:
                _log.error("推送条目失败，下轮重试: %s | %s", entry.title[:40], exc)

        return pushed
