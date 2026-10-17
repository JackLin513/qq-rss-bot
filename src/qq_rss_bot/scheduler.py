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
from .notify import Notifier, NullNotifier
from .quota import Pusher, QuotaExceeded
from .rss import Entry, fetch_feed

__all__ = ["Pusher", "QuotaExceeded", "FeedPoller", "format_entry"]

_log = logging.getLogger("qq_rss_bot.scheduler")

# QQ 单条文本消息有长度上限，留足余量
_MAX_CONTENT = 1500
# 逐条连发时留一点间隔：一是避免撞上群维度 20 qpm 的频控，
# 二是让客户端收到的顺序稳定（并发发出时到达顺序不保证）
_PUSH_GAP_SEC = 0.4


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_CONTENT else text[: _MAX_CONTENT - 1] + "…"


def format_entry(feed_title: str | None, url: str, entry: Entry) -> str:
    """单条新闻的推送文案：源名 + 标题 + 链接。"""
    lines = [f"📰 {feed_title or url}", f"· {entry.title}"]
    if entry.link:
        lines.append(f"  {entry.link}")
    return _truncate("\n".join(lines))


class FeedPoller:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        http: httpx.AsyncClient,
        pusher: Pusher,
        notifier: Notifier | None = None,
        quota=None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.http = http
        self.pusher = pusher
        # 出错时私聊管理员。默认空实现，测试里不用管。
        self.notifier: Notifier = notifier or NullNotifier()
        # 有 .exhausted(group) 的对象（GroupSender）。可空 —— 不传就没有提前检查，
        # 靠 QuotaExceeded 兜底，行为一样只是会白抓一轮。
        self.quota = quota
        self._sem = asyncio.Semaphore(5)
        self._stopped = asyncio.Event()
        self._ticks = 0

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        _log.info("轮询启动，每 %ss 扫描一次", self.cfg.tick_sec)
        while not self._stopped.is_set():
            # 低频心跳。**这不是冗余日志**：出问题时第一个要回答的就是
            # 「进程还活着吗、事件循环还在转吗」，而在此之前 INFO 级别下
            # 这个问题的答案是「不知道」—— 实测踩到过：群里发消息没反应，
            # 日志静默 22 分钟，分不清是没收到消息还是整个进程卡死了。
            # 每 20 拍（默认 10 分钟）一条，一天 144 行，不构成噪音。
            self._ticks += 1
            if self._ticks % 20 == 1:
                _log.info("轮询心跳：第 %d 拍，进程正常", self._ticks)
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

        await self._check_global_outage(due, results)

    async def _check_global_outage(self, due: list[Subscription], results: list) -> None:
        """整轮**全军覆没**时提醒一次：多半是 RSSHub 或网络挂了。

        比单独探活更准 —— 测的就是真正依赖的东西，也不额外发请求。
        只要有**一个**源成功就不报警，免得单个源的 404 被误报成整体故障。

        这正是「Docker 一停、所有源一起失败、群里却一声不吭」那个场景。
        """
        if not due:
            return
        # 跳过的（配额用尽）既不算成功也不算失败，必须排除掉 ——
        # 否则配额一满就会误报「RSSHub 挂了」，把人引到完全错误的方向。
        attempted = [r for r in results if r is not None]
        if not attempted or any(r is True for r in attempted):
            return
        _log.error("本轮 %d 个订阅全部抓取失败，怀疑 RSSHub 或网络不可达", len(attempted))
        for group_openid in {sub.group_openid for sub in due}:
            await self.notifier.notify(
                group_openid,
                f"⚠️ 本轮 {len(due)} 个订阅**全部**抓取失败，"
                f"多半是 RSSHub 或网络挂了。\n"
                f"先检查 RSSHub 容器是否在跑；恢复后会自动继续，不用手动干预。",
                key="rsshub_down",
            )

    async def _poll_one(self, sub: Subscription) -> bool | None:
        """抓一个订阅。

        返回 `True` 抓取成功 / `False` 抓取失败 / **`None` 本轮跳过**（配额用尽）。
        三态是必要的：跳过不是失败，混在一起会让 `_check_global_outage`
        把「配额用尽」误报成「所有源都挂了」。
        """
        async with self._sem:
            # 配额已用尽就别抓了：抓了也发不出去，白占 RSSHub 和带宽
            if self.quota is not None and self.quota.exhausted(sub.group_openid):
                _log.info("群 %s 今日配额用尽，跳过抓取 #%s", sub.group_openid, sub.id)
                return None

            result = await fetch_feed(self.http, sub.url, self.cfg.fetch_timeout_sec)

            if not result.ok:
                # 失败的订阅也更新 last_polled_at，等于退避一个间隔再重试
                fails = self.db.mark_polled(sub.id, error=result.error)
                _log.warning(
                    "抓取失败 #%s %s: %s（连续 %d 次）", sub.id, sub.url, result.error, fails
                )
                if fails >= self.cfg.max_fail_before_pause:
                    self.db.set_enabled(sub.group_openid, sub.id, False)
                    _log.warning("订阅 #%s 连续失败 %d 次，已自动暂停", sub.id, fails)
                    await self.notifier.notify(
                        sub.group_openid,
                        f"⚠️ 订阅《{sub.title or sub.url}》连续 {fails} 次抓取失败，已自动暂停。\n"
                        f"最后一次错误：{result.error}\n"
                        f"修好后用 /resume <编号> 恢复（编号见 /list）。",
                        key=f"pause:{sub.id}",
                    )
                return False

            keys = [e.key for e in result.entries]

            if not sub.initialized:
                # 首抓只建立 baseline，绝不推送，否则一订阅就倒灌几十条旧文
                self.db.filter_new_entries(sub.id, keys)
                self.db.mark_polled(sub.id, title=result.title, mark_initialized=True)
                _log.info("订阅 #%s 建立 baseline，%d 条", sub.id, len(keys))
                return True

            # 先只查、不写。推送成功之后才标记已见 ——
            # 顺序反了的话，推送失败时条目已被记为见过，就永远不会再推（静默丢消息）
            already = self.db.seen_keys(sub.id, keys)
            fresh = [e for e in result.entries if e.key not in already]
            self.db.mark_polled(sub.id, title=result.title)
            self.db.prune_seen(sub.id)

            if not fresh:
                return True

            shown = fresh[: self.cfg.max_items_per_push]
            try:
                pushed = await self._push_entries(sub, result.title, shown)
            except QuotaExceeded as exc:
                # 配额用尽：剩下的逐条发也只会被拒，整批跳过。
                # 仍然标记已见 —— 否则明天配额恢复后会倒灌一整天的积压。
                self.db.mark_seen(sub.id, [e.key for e in fresh])
                _log.warning("群 %s 配额用尽，跳过 %d 条: %s", sub.group_openid, len(fresh), exc)
                await self.notifier.notify(
                    sub.group_openid,
                    f"⚠️ 今日主动消息配额已用尽（{exc.used}/{exc.limit} 条）。\n"
                    f"《{sub.title or sub.url}》等源的更新今天不再推送，明天零点自动恢复。\n"
                    f"想省着用可以调大间隔（/interval <编号> <分钟>）。",
                    key="quota",
                )
                return True

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
                except QuotaExceeded:
                    pass  # 只是提示，配额没了就不发
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
            return True

    async def _push_entries(
        self, sub: Subscription, feed_title: str | None, entries: list[Entry]
    ) -> list[Entry]:
        """逐条推送，返回**成功**的那些。

        单条失败不能中断整批（否则它后面的条目白丢），也不标记已见，
        留给下一轮重试。

        **`QuotaExceeded` 必须穿透**：它是 RuntimeError，会被下面的
        `except Exception` 当成「配图失败/发送失败」吞掉，于是整批每条都
        白试两遍（先配图失败、再纯文字失败），日志刷满却什么都没解决。
        配额是整批级别的状态，交给 `_poll_one` 统一处理。
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
                except QuotaExceeded:
                    raise
                except Exception as exc:
                    # 配图失败（QQ 拉不到图 / 图太大 / 格式不支持）不该让整条新闻丢失，
                    # 退回纯文字再发一次
                    _log.warning("配图推送失败，退回纯文字 (%s): %s", entry.image, exc)

            try:
                await self.pusher.send_text(sub.group_openid, text)
                pushed.append(entry)
            except QuotaExceeded:
                raise
            except Exception as exc:
                _log.error("推送条目失败，下轮重试: %s | %s", entry.title[:40], exc)

        return pushed
