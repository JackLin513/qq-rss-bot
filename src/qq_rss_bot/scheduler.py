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
import time
from functools import partial
from typing import Awaitable, Protocol

import httpx

from . import ai, idolmaster, multiimage
from .config import Config
from .db import Database, Subscription, utcnow
from .notify import Notifier, NullNotifier
from .quota import Pusher, QuotaExceeded, today
from .rss import Entry, fetch_feed

__all__ = [
    "Pusher",
    "QuotaExceeded",
    "FeedPoller",
    "format_entry",
    "prepare_digests",
]

_log = logging.getLogger("qq_rss_bot.scheduler")

# QQ 单条文本消息有长度上限，留足余量
_MAX_CONTENT = 1500
# 逐条连发时留一点间隔：一是避免撞上群维度 20 qpm 的频控，
# 二是让客户端收到的顺序稳定（并发发出时到达顺序不保证）
_PUSH_GAP_SEC = 0.4

# 被「不发」否掉的条目 key 保留多久。**不能只留一轮** —— 正式群有好几个，
# 它们的轮询是并发的，只要有一个这一轮抓取失败、下一轮才发，就已经过了本轮。
# 6 小时足够覆盖任何退避重试，又不会让集合无限长。
_VETO_KEEP_SEC = 6 * 3600

# AI 摘要的并发数。抓正文 ~2s + 调模型 ~3s，3 条并发大概 5 秒能备好一批；
# 再高意义不大（DeepSeek 那边也会限流），而且会把 RSSHub 那条链路挤到。
_AI_CONCURRENCY = 3


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_CONTENT else text[: _MAX_CONTENT - 1] + "…"


# 摘要进推送文案的长度上限，可用 `SUMMARY_CHARS` 覆盖。
#
# **这个数不是平台限制。** 2026-10-18 实测 `scripts/probe_textlen.py`：
# 中文文本 40000 字符 QQ 照样收（HTTP 200）。真正的约束是**可读性** ——
# 群里一条消息超过一屏就没人看了，那还发它干什么。
#
# **800 是量出来的，不是拍的**（2026-10-18 实测 4 个源的 `<description>`）：
#
#   idolmaster  中位 627 / p90 3545     ← 800 完整覆盖中位
#   少数派      中位 3914（就是全文）
#   Solidot     中位 485 / 最大 857     ← 800 全覆盖
#   36氪快讯    中位 205 / 最大 422     ← 800 全覆盖
#
# 只有「`<description>` 就是全文」那类（少数派）才该上 AI 摘要 ——
# 靠调大这个数解决不了，`rss.py` 入口处已经先截到 4000 了。
#
# ⚠️ **别往上调到 1200 以上**：`_MAX_CONTENT = 1500` 限的是**整条消息**，
# 标题最长 120 + 源名 25 + 标签 20 + 链接 50 = 215，1200 左右就会把
# 链接挤出去 —— 而链接是这条推送里最值钱的东西。
MAX_SUMMARY_CHARS = 800


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary_line(entry: Entry, limit: int = MAX_SUMMARY_CHARS) -> str:
    """摘要那一行。拿不出有信息量的内容就返回空串（宁可不加这一行）。"""
    text = (entry.summary or "").strip()
    if not text:
        return ""
    # 很多源把标题原样塞进 `<description>`，照抄会变成标题下面又一行同样的字。
    # 剥掉开头那次再判断。
    if text.startswith(entry.title):
        text = text[len(entry.title) :].lstrip(" -—–·:：|")
    if not text or text == entry.title:
        return ""
    return _clip(text, limit)


def format_entry(
    feed_title: str | None,
    url: str,
    entry: Entry,
    *,
    with_summary: bool = True,
    summary_chars: int = MAX_SUMMARY_CHARS,
    digest: "ai.Digest | None" = None,
    tags: str = "",
    with_link: bool = True,
) -> str:
    """单条新闻的推送文案：源名 + 标题 +（摘要）+ 链接。

    **摘要这一行是 2026-10-18 补的。** 在此之前 `Entry.summary` 从 RSS 里
    抓出来、洗完标签、存进对象，然后**一行都没用过** —— 群里看到的永远是
    光秃秃的「源名 / 标题 / 链接」，用户直接问「是不是 RSS 省略了」。
    不是省略，是我们没发。

    `with_summary=False` 是给 `PUSH_SUMMARY` 开关用的，不是给调用方随手传的。

    给了 `digest`（AI 处理过的）就用它的标题和摘要，**完全盖掉** RSS 那份。

    **不附原标题。** 曾经附过一行 `原题：…`（理由是翻译会丢人名和产品名），
    实测发出来太长 —— 一条消息里标题、原题、摘要、链接四段，群里一屏放不下，
    而原题的信息量最低。`Digest.translated` 现在只用来写日志。
    """
    lines = [f"📰 {feed_title or url}"]
    if tags:
        lines.append(tags)
    body = ""
    if digest is not None:
        lines.append(f"· {digest.title}")
        body = _clip(digest.summary, summary_chars) if digest.summary else ""
    else:
        lines.append(f"· {entry.title}")
        if with_summary:
            body = _summary_line(entry, summary_chars)
    if body:
        # **空行是必须的，不是装饰。** 推送走的是 msg_type=0/7 的**纯文本**
        # （不是 markdown），单换行在客户端里读起来就是「标题紧贴着正文」一坨。
        # 空行把它切成两段。
        #
        # **只在真有正文时才加** —— 标题和正文一样时 `_summary_line` 返回空串，
        # 这里就不会多出一个孤零零的空行。
        lines.append("")
        lines.append(f"  {body}")
    if with_link and entry.link:
        lines.append(f"  {entry.link}")
    return _truncate("\n".join(lines))


# 单条发送失败后的重试间隔。QQ 会用 `500` + `50035006 消息发送异常，请稍后重试`
# 拒掉连发里的某一条（实测多图时**第 3 张**撞上过）。它自己说可重试，那就隔
# 一下再试一次 —— **只重试一次**是有意的，再多就是把偶发失败换成刷屏。
IMAGE_RETRY_GAP = 1.0


async def send_with_retry(send, content: str, url: str) -> None:
    """发一条图文，失败就隔 `IMAGE_RETRY_GAP` 秒重试**一次**。

    `send` 是「已经绑好目标」的可调用对象：RSS 侧传
    `functools.partial(pusher.send_rich, group_openid)`，预览侧直接传
    `reply_rich` —— 两边签名不同，绑定交给调用方。

    `QuotaExceeded` 直接抛：配额用完是整批级别的状态，重试没有意义。
    """
    for attempt in (1, 2):
        try:
            await send(content, url)
            return
        except QuotaExceeded:
            raise
        except Exception:
            if attempt == 2:
                raise
            _log.info("发送失败，%.1f 秒后重试一次: %s", IMAGE_RETRY_GAP, url)
            await asyncio.sleep(IMAGE_RETRY_GAP)


# 配图失败私聊的冷却键前缀。**只带群号、不带条目** —— `C2CNotifier` 对同一个
# key 有 30 分钟冷却，所以一条资讯 4 张全挂不会变成 4 条私聊，RSSHub 抽风
# 批量失败也不会变成 DM 风暴。代价是冷却窗口内的后续失败只进日志。
# 定时推送和链接预览**共用同一个 key**：两个入口同时出问题时报一次就够了。
IMAGE_FAILURE_KEY_PREFIX = "imgfail:"


def image_failure_message(reason: str, headline: str, failed: list[str]) -> str:
    """配图失败私聊的正文。三个调用点（定时推送 / 链接预览 / `/new`）共用这一份 ——
    各写一份的话，「已重试一次」这种细节迟早只在一边更新。

    `reason` 由调用方给：**失败的形状不止一种**（部分图掉了 / 首图就挂导致整条
    退回纯文字），统一措辞会把两种情况说成一样。
    """
    sample = "\n".join(f"· {u}" for u in failed[:3])
    more = f"\n…还有 {len(failed) - 3} 张" if len(failed) > 3 else ""
    return f"⚠️ 配图发送失败：{reason}\n{headline}\n{sample}{more}"


async def send_entry_images(
    pusher: Pusher,
    group_openid: str,
    text: str,
    images: "tuple[str, ...] | list[str]",
    *,
    http: "httpx.AsyncClient | None" = None,
    link: str = "",
    markdown: bool = False,
) -> list[str]:
    """把一条资讯连同它的**全部配图**发出去。

    **一个条目只发一条消息，永远不连发。**

    * 不止一张图、且允许 markdown → **一条 markdown 带全部图**（`msg_type=2`）。
      图由 QQ 的服务器去下载，语法和坑见 `multiimage.py` 的模块说明。
    * markdown 没走通 / 被开关关掉 → **只发第一张**（`msg_type=7` 富媒体），
      其余算没发出去，由调用方私聊告警。
      2026-10-22 之前这里是「退回连发 N 条」—— 那正好在 markdown 出问题的时候
      刷屏，是最不该做的选择。

    **失败语义是分开的，这点很重要**：

    * **第一条**失败会抛出去 —— 调用方据此退回纯文字（原有行为）；
    * **后续**某张失败只记日志、跳过。正文那时候已经发出去了，再抛一次
      会让调用方以为整条没发成，退回纯文字等于**重复刷屏**；
    * `QuotaExceeded` 一律穿透 —— 配额是整批级别的状态，不该被当成
      「这张图坏了」（这条踩过，见 CLAUDE.md 的限流那节）。

    返回**没发出去**的图片 URL（空列表 = 全成功）。调用方拿它去私聊告警 ——
    配图失败在群里是**看不见**的，读者只会觉得「这条怎么少了两张图」。
    """
    if markdown and http is not None and len(images) > 1:
        sent = await _send_entry_markdown(
            pusher, group_openid, text, images, http, link
        )
        if sent is not None:
            return sent
        _log.warning(
            "多图 markdown 没走通 → 只发第一张（**不连发**），其余 %d 张算失败",
            len(images) - 1,
        )

    # 单张：富媒体（`msg_type=7`）。第一条失败照旧抛出去，调用方退回纯文字。
    await send_with_retry(partial(pusher.send_rich, group_openid), text, images[0])
    return list(images[1:])


async def _send_entry_markdown(
    pusher: Pusher,
    group_openid: str,
    text: str,
    images: "tuple[str, ...] | list[str]",
    http: httpx.AsyncClient,
    link: str,
) -> "list[str] | None":
    """一条 markdown 带全部图。整条路不通返回 `None`，调用方**只发第一张**。

    返回 `[]` 表示**发成功了**，中间可能剔掉了几张 QQ 抓不到的图 —— 那些 URL
    一起返回，交给调用方走告警。

    **为什么要先逐张 `upload_image` 探一次**：markdown 的图裂了是**静默**的
    （发送接口照样 HTTP 200，没有任何回执），事后完全无从发现；而富媒体那条路
    有 `post_group_file` 的报错兜着。所以这里花 N 次「不发消息的上传」换回
    「哪张图 QQ 抓不到」这个信息 —— 它不占配额、也不进令牌桶。

    **尺寸必须逐张读**（`measure_all`）：尺寸提示在手机端是**必填**的，比例还得
    和原图一致，否则整条 markdown 不渲染、或者图被拉伸。读不到尺寸的那张直接
    剔掉，不猜。
    """
    items = await multiimage.measure_all(http, images)
    if not items:
        return None

    good: list[tuple[str, tuple[int, int]]] = []
    dropped: list[str] = []
    for url, wh in items:
        if await pusher.upload_image(group_openid, url):
            good.append((url, wh))
        else:
            dropped.append(url)
    if not good:
        return None

    md = multiimage.build(text, good, link)
    for attempt in (1, 2):
        try:
            await pusher.send_markdown(group_openid, md)
            break
        except QuotaExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                # 重试过还是不行 —— 交给调用方**只发第一张**。
                _log.warning("多图 markdown 发送失败，落回只发第一张: %s", exc)
                return None
            # QQ 会拿 `50035006 消息发送异常，请稍后重试` 把**整条**拒掉，它自己
            # 说可重试，那就重试一次。**不重试的代价是其余几张全丢**（落回只发
            # 第一张），2026-10-18 一天撞了 4 次。
            _log.info(
                "多图 markdown 发送失败，%.1f 秒后重试一次: %s", IMAGE_RETRY_GAP, exc
            )
            await asyncio.sleep(IMAGE_RETRY_GAP)
    return dropped


async def prepare_digests(
    db: Database,
    cfg: Config,
    http: httpx.AsyncClient,
    ai_http: httpx.AsyncClient | None,
    sub: Subscription,
    entries: list[Entry],
) -> "dict[str, ai.Digest]":
    """给这一批条目并发备好 AI 摘要。返回 `{entry.key: Digest}`。

    **为什么必须预取**：抓正文 ~2 秒 + 调模型 ~3 秒，逐条现算的话 5 条
    就是半分钟，而这半分钟里群里的其他推送全在排队。并发备好之后，
    推送循环本身的节奏一点没变。

    **为什么失败是静默的**：这条路任何一环挂掉（没配 key、抓不到正文、
    模型超时、JSON 烂掉）都只是「这条没有 AI 摘要」，退回 RSS 原本的文案。
    绝不能让一条推送因为摘要失败而消失。
    """
    if not (sub.ai_enabled and cfg.ai_api_key):
        return {}
    out: dict[str, ai.Digest] = {}
    sem = asyncio.Semaphore(_AI_CONCURRENCY)

    async def one(entry: Entry) -> None:
        async with sem:
            got = await digest_for(db, cfg, http, ai_http, entry)
        if got is not None:
            out[entry.key] = got

    await asyncio.gather(*(one(e) for e in entries))
    return out

async def digest_for(
    db: Database,
    cfg: Config,
    http: httpx.AsyncClient,
    ai_http: httpx.AsyncClient | None,
    entry: Entry,
) -> "ai.Digest | None":
    """单条：查缓存 → 抓正文 → 调模型。任何一步失败都返回 None。"""
    cached = db.get_ai_cache(entry.key)
    if cached is not None:
        title, summary, translated = cached
        return ai.Digest(title=title, summary=summary, translated=translated)

    day = today()
    if db.ai_count(day) >= cfg.ai_daily_limit:
        _log.warning(
            "ai：今日调用已达上限 %d，这条退回 RSS 原文案（改 AI_DAILY_LIMIT 可放宽）",
            cfg.ai_daily_limit,
        )
        return None

    text = ""
    # **默认不抓文章页**（`AI_FETCH_ARTICLE=false`）。很多源的 `<description>`
    # 本身就是全文 —— 实测少数派 3914 字、Solidot 368 —— 再抓一遍纯属浪费
    # 一次请求和几秒延迟。只有 V2EX / Hacker News 那种「只给标题」的源才值得开。
    if cfg.ai_fetch_article and entry.link:
        text = await ai.fetch_article(
            http,
            entry.link,
            timeout=cfg.ai_fetch_timeout_sec,
            proxy_http=ai_http,
        )
    # 抓不到正文不算失败 —— RSS 的导语照样能让它翻译标题、写出摘要，
    # 只是信息量少一些。真要一句都没有才放弃。
    if not text:
        text = entry.summary or ""
    if not text and not entry.title:
        return None

    # **先计数再调用**：失败也要计。模型那边已经收到请求了，
    # 而且失败通常意味着我们的代码有问题，不计数的话会一直重试烧钱。
    db.bump_ai_count(day)

    got = await ai.digest(
        http,
        api_key=cfg.ai_api_key,
        title=entry.title,
        text=text,
        base_url=cfg.ai_base_url,
        model=cfg.ai_model,
        timeout=cfg.ai_timeout_sec,
        disable_thinking=cfg.ai_disable_thinking,
    )
    if got is not None:
        db.put_ai_cache(entry.key, got.title, got.summary, got.translated)
        _log.info(
            "ai：%s → %s（输入 %d 字，%s）",
            entry.title[:30],
            "已翻译" if got.translated else "原文",
            len(text),
            "文章页" if cfg.ai_fetch_article else "RSS 导语",
        )
    return got


class FeedPoller:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        http: httpx.AsyncClient,
        pusher: Pusher,
        notifier: Notifier | None = None,
        quota=None,
        ai_http: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.http = http
        self.pusher = pusher
        # 抓文章正文用的**备用**客户端（走代理）。直连失败才会用到 ——
        # 国内的源走代理又慢又可能撞 MITM 证书问题，不能一上来就代理。
        self.ai_http = ai_http
        # 出错时私聊管理员。默认空实现，测试里不用管。
        self.notifier: Notifier = notifier or NullNotifier()
        # 有 .exhausted(group) 的对象（GroupSender）。可空 —— 不传就没有提前检查，
        # 靠 QuotaExceeded 兜底，行为一样只是会白抓一轮。
        self.quota = quota
        self._sem = asyncio.Semaphore(5)
        self._stopped = asyncio.Event()
        self._ticks = 0
        # ---- markdown 多图的人工预览（见 Config.preview_group 那段注释）----
        # 「不发」喊停时置位。**只在预览窗口内有效**。
        self._veto = asyncio.Event()
        self._preview_open = False
        # 被人工否掉的条目 key → 时间戳，见 `_VETO_KEEP_SEC`。
        self._vetoed: dict[str, float] = {}
        # 预览那轮真正发出去的 key，用来在喊停时**精确**挡住同一批，
        # 而不是把这一轮所有条目都一起毙掉。
        self._last_pushed: dict[int, list[str]] = {}

    def stop(self) -> None:
        self._stopped.set()

    def veto_current(self) -> bool:
        """人工喊「不发」。返回 True 表示**确实挡下了**。

        **只有预览窗口开着才生效。** 窗口外返回 False —— 否则你随手打一句
        「不发」，下一条正式推送会莫名其妙被吞掉，而且完全看不出原因。
        """
        if not self._preview_open:
            return False
        self._veto.set()
        return True

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
        collected = await self._poll_staged(due)
        results = [collected.get(sub.id) for sub in due]
        await self._check_global_outage(due, results)

    async def _gather_poll(self, subs: list[Subscription]) -> dict[int, object]:
        """并发抓一批订阅，返回 `{订阅 id: 结果}`。

        `return_exceptions=True` 会把异常收进结果里静默丢弃，所以必须逐个检查 ——
        不检查的话推送失败连一行日志都没有。
        """
        results = await asyncio.gather(
            *(self._poll_one(sub) for sub in subs), return_exceptions=True
        )
        for sub, result in zip(subs, results):
            if isinstance(result, BaseException):
                _log.error("订阅 #%s (%s) 本轮处理失败", sub.id, sub.url, exc_info=result)
        return {sub.id: r for sub, r in zip(subs, results)}

    def _stage_subs(self, group_openid: str, urls: set[str]) -> list[Subscription]:
        """预热群 / 预览群自己的订阅，**绕开 `due` 直接取**。

        **为什么不能靠 `due` 对齐**：实测这三个订阅虽然都是 900 秒，但轮询时刻
        相差 90 秒（07:25:37 / 07:26:08 / 07:27:38）—— 它们**永远不会落进同一个
        `due` 批次**。靠 `due` 判定的写法会一直退化成普通并发，三阶段一次都不
        触发，正式群就一直冷发 —— 症状和没做这个功能一模一样。

        只取和正式群**同一个源**的订阅：拿预热群去预热别的源毫无意义。
        """
        if not group_openid or self.db is None:
            return []
        return [
            s for s in self.db.list_subscriptions(group_openid)
            if s.enabled and s.url in urls
        ]

    async def _poll_staged(self, due: list[Subscription]) -> dict[int, object]:
        """预热① → 预热②（=预览）→ 等喊停 → 正式群。

        **三个阶段必须串行，这正是整个机制的意义** —— 一把 gather 发下去，
        预热和正式群同时在飞，预热就白做了。

        预热群 / 预览群**只在「有正式群要发」的这一轮被拉起来**（见
        `_stage_subs`），不按它们自己的间隔单独跑 —— 让它们先自己发一遍的话，
        等正式群到点时它们已经「没有新条目」了，预热不出任何东西。

        两个群都留空 = 完全退回改动前的行为（一把 gather）。
        """
        cfg = self.cfg
        special = {cfg.preview_group} if cfg.preview_group else set()
        if not special:
            return await self._gather_poll(due)

        real = [s for s in due if s.group_openid not in special]
        if not real:
            # 只有预热群/预览群到点。**什么都不做** —— 它们由正式群那一轮带着跑。
            return {}

        now = time.monotonic()
        self._vetoed = {k: t for k, t in self._vetoed.items() if now - t < _VETO_KEEP_SEC}
        self._last_pushed = {}
        collected: dict[int, object] = {}
        urls = {s.url for s in real}

        # ② 预览群 + 人工否决窗口
        prev = self._stage_subs(cfg.preview_group, urls)
        if prev:
            self._veto.clear()
            self._preview_open = True
            try:
                collected.update(await self._gather_poll(prev))
                keys = [k for s in prev for k in self._last_pushed.get(s.id, [])]
                if keys:
                    _log.info(
                        "多图预览已发（%d 条），等 %d 秒看你喊不喊停",
                        len(keys), cfg.preview_veto_sec,
                    )
                    try:
                        await asyncio.wait_for(
                            self._veto.wait(), timeout=cfg.preview_veto_sec
                        )
                    except asyncio.TimeoutError:
                        pass
                    else:
                        for k in keys:
                            self._vetoed[k] = time.monotonic()
                        _log.info("收到「不发」：这 %d 条不进正式群", len(keys))
            finally:
                self._preview_open = False
                self._veto.clear()

        # ③ 正式群
        collected.update(await self._gather_poll(real))
        return collected

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
                self._last_pushed[sub.id] = [e.key for e in pushed]
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

    async def _report_image_failures(
        self,
        group_openid: str,
        reason: str,
        feed_title: str | None,
        entry: Entry,
        failed: list[str],
    ) -> None:
        """配图没发出去 → 私聊告警。

        为什么要报：**群里看不出来**。读者只会觉得「这条怎么少了两张图」，
        而我们会一直以为发成功了 —— 第 3 张一直发不出去这件事就是这么发现的
        （翻 NAS 日志才看见）。

        冷却键见 `IMAGE_FAILURE_KEY_PREFIX`。
        """
        await self.notifier.notify(
            group_openid,
            image_failure_message(
                reason, f"源：{feed_title or '-'}\n条目：{entry.title}", failed
            ),
            key=f"{IMAGE_FAILURE_KEY_PREFIX}{group_openid}",
        )

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
        digests = await prepare_digests(
            self.db, self.cfg, self.http, self.ai_http, sub, entries
        )
        # 偶像大师官网 NEWS 的企划/分类标签。非那个源直接返回 {}，
        # 所以这里不用先判订阅。
        tags = (
            await idolmaster.tags_for(self.http, sub.url, entries)
            if self.cfg.imas_tags
            else {}
        )
        for index, entry in enumerate(entries):
            if index:
                await asyncio.sleep(_PUSH_GAP_SEC)
            if entry.key in self._vetoed:
                # 人工在预览里否掉的条目。**当成「已推」返回** —— 调用方会把它
                # 标成已见。不当成已推的话下一轮又捞出来，你得每轮否一次。
                _log.info("被「不发」挡下，跳过: %s", entry.title[:40])
                pushed.append(entry)
                continue
            # `entry.images` 可能是 None（源里没图），先归一成 list 再判断
            images = list(entry.images or ())
            if not self.cfg.rss_multi_image:
                images = images[:1]
            # 多图合并成一条 markdown 时，链接要排在图片**后面**，
            # 所以这里先别把它排进正文（下面单独补 `[打开原文]`）。
            use_md = (
                self.cfg.multi_image_markdown
                and self.cfg.attach_images
                and len(images) > 1
            )
            text = format_entry(
                feed_title,
                sub.url,
                entry,
                with_summary=self.cfg.push_summary,
                summary_chars=self.cfg.summary_chars,
                digest=digests.get(entry.key),
                tags=tags.get(entry.key, ""),
                with_link=not use_md,
            )

            if self.cfg.attach_images and images:
                try:
                    failed = await send_entry_images(
                        self.pusher,
                        sub.group_openid,
                        text,
                        images,
                        http=self.http,
                        link=entry.link,
                        markdown=use_md,
                    )
                    pushed.append(entry)
                    if failed:
                        await self._report_image_failures(
                            sub.group_openid,
                            f"{len(failed)}/{len(images)} 张没发出去（每张已重试一次）",
                            feed_title,
                            entry,
                            failed,
                        )
                    continue
                except QuotaExceeded:
                    raise
                except Exception as exc:
                    # 配图失败（QQ 拉不到图 / 图太大 / 格式不支持）不该让整条新闻丢失，
                    # 退回纯文字再发一次
                    _log.warning("配图推送失败，退回纯文字 (%s): %s", images[0], exc)
                    # **这条路更要报。** 部分图掉了只是「少一张」，而首图就挂
                    # 意味着这个源/这类图**一张都发不出去** —— 症状是全文字，
                    # 看起来和「这条本来没图」一模一样。
                    # 只报首图：后面几张根本没试过，算进去就是编数字。
                    await self._report_image_failures(
                        sub.group_openid,
                        f"首图就发不出去，整条已退回纯文字（共 {len(images)} 张）",
                        feed_title,
                        entry,
                        [images[0]],
                    )

            try:
                await self.pusher.send_text(sub.group_openid, text)
                pushed.append(entry)
            except QuotaExceeded:
                raise
            except Exception as exc:
                _log.error("推送条目失败，下轮重试: %s | %s", entry.title[:40], exc)

        return pushed
